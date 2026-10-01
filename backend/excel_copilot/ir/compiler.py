"""Validate an IR program against the workbook profile and compile it to restricted Python.

One pass does both: each step is checked against the typed schema of its input
frame (tracked step by step), and, if valid, emitted as one line of pandas.
Errors are structured (step, field, message, hint) so they can be fed straight
back to the coder model for repair.
"""
from __future__ import annotations

import difflib
import keyword
import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from ..profiler.models import WorkbookProfile
from .expr import COLUMN_TYPE_MAP, Checker, EType, ExprError
from .models import (Aggregate, Derive, Filter, Join, Limit, Load, Pivot, Program, Select, Sort,
                     Unpivot)

_FRAME = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
RESERVED = {"tables", "xl", "pd", "np", "run", "result", "df"} | set(keyword.kwlist)
NUMERIC_AGGS = {"sum", "mean", "median", "std"}
MAX_STEPS = 30


class Diagnostic(BaseModel):
    step: int | None            # index into program.steps, None for program-level
    field: str
    message: str
    hint: str | None = None
    severity: str = "error"     # "error" | "warning"

    def render(self) -> str:
        where = f"step {self.step} ({self.field})" if self.step is not None else self.field
        return f"[{self.severity}] {where}: {self.message}" + (f" — hint: {self.hint}" if self.hint else "")


@dataclass
class Compiled:
    ok: bool
    diagnostics: list[Diagnostic]
    source: str = ""
    schemas: dict[str, dict[str, EType]] = field(default_factory=dict)
    tables_used: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


class _Fail(Exception):
    def __init__(self, field: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.field, self.message, self.hint = field, message, hint


def _suggest(name: str, options: list[str]) -> str | None:
    close = difflib.get_close_matches(name, options, n=3, cutoff=0.5)
    if close:
        return "did you mean " + ", ".join(map(repr, close)) + "?"
    return "available: " + ", ".join(map(repr, options[:25]))


def compile_program(program: Program, profile: WorkbookProfile) -> Compiled:
    diags: list[Diagnostic] = []
    schemas: dict[str, dict[str, EType]] = {}
    roles: dict[str, dict[str, str]] = {}          # frame -> column -> profile role (for semantic checks)
    opaque: set[str] = set()                        # frames from pivot: columns unknown until runtime
    lines = ["def run(tables):"]
    tables_used: list[str] = []
    table_ids = [t.id for t in profile.tables]

    if not program.steps:
        diags.append(Diagnostic(step=None, field="steps", message="program has no steps"))
    if len(program.steps) > MAX_STEPS:
        diags.append(Diagnostic(step=None, field="steps", message=f"more than {MAX_STEPS} steps"))

    def frame(name: str, fld: str) -> dict[str, EType]:
        if name not in schemas:
            raise _Fail(fld, f"frame {name!r} is not defined before this step",
                        _suggest(name, list(schemas)) if schemas else "start with a load step")
        return schemas[name]

    def cols(schema: dict[str, EType], names: list[str], fld: str, src: str) -> None:
        if src in opaque:
            return
        for c in names:
            if c not in schema:
                raise _Fail(fld, f"column {c!r} does not exist in frame {src!r}", _suggest(c, list(schema)))

    for i, step in enumerate(program.steps):
        try:
            out = step.out
            if not _FRAME.match(out) or out in RESERVED:
                raise _Fail("out", f"invalid frame name {out!r}", "use a short snake_case name like 'q3_orders'")
            if out in schemas:
                raise _Fail("out", f"frame {out!r} is already defined", "every step needs a new output name")

            if isinstance(step, Load):
                if step.table not in table_ids:
                    raise _Fail("table", f"unknown table {step.table!r}", _suggest(step.table, table_ids))
                t = profile.table(step.table)
                schema = {c.name: COLUMN_TYPE_MAP[c.type] for c in t.columns}
                roles[out] = {c.name: c.role for c in t.columns}
                tables_used.append(step.table)
                code = f"tables[{step.table!r}].copy()"

            elif isinstance(step, Filter):
                src = frame(step.input, "input")
                if step.input in opaque:
                    raise _Fail("input", "cannot filter a pivoted frame; filter before pivoting")
                chk = Checker(src, step.input)
                try:
                    cond = chk.check(step.condition)
                except ExprError as e:
                    raise _Fail("condition", str(e), e.hint) from None
                if cond.type != "bool":
                    raise _Fail("condition", f"condition must be boolean, got {cond.type}",
                                "compare the value, e.g. Units > 0")
                schema, roles[out] = dict(src), dict(roles[step.input])
                code = f"{step.input}.loc[xl.mask({cond.code})]"

            elif isinstance(step, Derive):
                src = frame(step.input, "input")
                if step.input in opaque:
                    raise _Fail("input", "cannot derive on a pivoted frame")
                if not step.column.strip() or len(step.column) > 80:
                    raise _Fail("column", "derived column needs a name (max 80 chars)")
                chk = Checker(src, step.input)
                try:
                    val = chk.check(step.expression)
                except ExprError as e:
                    raise _Fail("expression", str(e), e.hint) from None
                schema = dict(src)
                schema[step.column] = val.type
                roles[out] = dict(roles[step.input])
                roles[out][step.column] = "measure" if val.type == "num" else "dimension"
                code = f"xl.with_column({step.input}, {step.column!r}, {val.code})"

            elif isinstance(step, Select):
                src = frame(step.input, "input")
                if not step.columns:
                    raise _Fail("columns", "select needs at least one column")
                cols(src, step.columns, "columns", step.input)
                if len(set(step.columns)) != len(step.columns):
                    raise _Fail("columns", "duplicate column in select")
                schema = {c: src[c] for c in step.columns}
                roles[out] = {c: roles[step.input].get(c, "") for c in step.columns}
                code = f"{step.input}[{list(step.columns)!r}]"

            elif isinstance(step, Aggregate):
                src = frame(step.input, "input")
                if step.input in opaque:
                    raise _Fail("input", "cannot aggregate a pivoted frame")
                cols(src, step.group_by, "group_by", step.input)
                if not step.aggregations:
                    raise _Fail("aggregations", "aggregate needs at least one aggregation")
                schema = {g: src[g] for g in step.group_by}
                names = set(step.group_by)
                aggs = []
                for j, a in enumerate(step.aggregations):
                    fld = f"aggregations[{j}]"
                    if a.column == "*":
                        if a.func != "count":
                            raise _Fail(fld, "'*' can only be used with count")
                    else:
                        cols(src, [a.column], fld, step.input)
                        t = src[a.column]
                        if a.func in NUMERIC_AGGS and t != "num":
                            raise _Fail(fld, f"{a.func} needs a numeric column; {a.column!r} is {t}",
                                        "use count or nunique for text columns")
                        if a.func in ("min", "max") and t not in ("num", "date", "str"):
                            raise _Fail(fld, f"{a.func} is not defined for {t}")
                        if a.func in NUMERIC_AGGS and roles[step.input].get(a.column) == "identifier":
                            raise _Fail(fld, f"{a.column!r} is an identifier; {a.func} of IDs is meaningless",
                                        "use count or nunique to count entities")
                    if not a.as_name.strip() or a.as_name in names:
                        raise _Fail(fld, f"output name {a.as_name!r} is empty or clashes with another column")
                    names.add(a.as_name)
                    schema[a.as_name] = ("num" if a.func in NUMERIC_AGGS | {"count", "nunique"}
                                         else src[a.column])
                    aggs.append((a.column, a.func, a.as_name))
                roles[out] = {g: roles[step.input].get(g, "") for g in step.group_by} | {
                    a.as_name: "measure" for a in step.aggregations}
                code = f"xl.aggregate({step.input}, {list(step.group_by)!r}, {aggs!r})"

            elif isinstance(step, Join):
                left, right = frame(step.left, "left"), frame(step.right, "right")
                if step.left == step.right:
                    raise _Fail("right", "self-joins are not supported")
                if not step.left_on or len(step.left_on) != len(step.right_on):
                    raise _Fail("left_on", "left_on and right_on must be non-empty and the same length")
                cols(left, step.left_on, "left_on", step.left)
                cols(right, step.right_on, "right_on", step.right)
                for lk, rk in zip(step.left_on, step.right_on):
                    if left[lk] != right[rk]:
                        raise _Fail("right_on", f"join key types differ: {lk!r} is {left[lk]}, {rk!r} is {right[rk]}")
                schema = dict(left)
                roles[out] = dict(roles[step.left])
                for c, t in right.items():
                    if c in step.right_on and step.right_on.index(c) < len(step.left_on) \
                            and step.left_on[step.right_on.index(c)] == c:
                        continue                    # same-named key: kept once
                    name = c if c not in schema else f"{c}_right"
                    schema[name] = t
                    roles[out][name] = roles[step.right].get(c, "")
                code = (f"xl.join({step.left}, {step.right}, {list(step.left_on)!r}, "
                        f"{list(step.right_on)!r}, {step.how!r})")

            elif isinstance(step, Sort):
                src = frame(step.input, "input")
                if not step.by:
                    raise _Fail("by", "sort needs at least one key")
                cols(src, [k.column for k in step.by], "by", step.input)
                schema, roles[out] = dict(src), dict(roles[step.input])
                code = (f"{step.input}.sort_values(by={[k.column for k in step.by]!r}, "
                        f"ascending={[not k.descending for k in step.by]!r}, na_position='last', kind='stable')")

            elif isinstance(step, Limit):
                src = frame(step.input, "input")
                if not 1 <= step.n <= 100_000:
                    raise _Fail("n", "limit must be between 1 and 100000")
                schema, roles[out] = dict(src), dict(roles[step.input])
                code = f"{step.input}.head({int(step.n)})"

            elif isinstance(step, Unpivot):
                src = frame(step.input, "input")
                cols(src, step.id_columns + step.value_columns, "value_columns", step.input)
                if not step.value_columns:
                    raise _Fail("value_columns", "unpivot needs value columns")
                vt = {src[c] for c in step.value_columns}
                if len(vt) != 1:
                    raise _Fail("value_columns", f"value columns have mixed types {sorted(vt)}")
                for nm in (step.var_name, step.value_name):
                    if not nm.strip() or nm in step.id_columns:
                        raise _Fail("var_name", f"name {nm!r} is empty or clashes with an id column")
                schema = {c: src[c] for c in step.id_columns} | {step.var_name: "str", step.value_name: vt.pop()}
                roles[out] = {c: roles[step.input].get(c, "") for c in step.id_columns} | {
                    step.var_name: "dimension", step.value_name: "measure"}
                code = (f"{step.input}.melt(id_vars={list(step.id_columns)!r}, "
                        f"value_vars={list(step.value_columns)!r}, var_name={step.var_name!r}, "
                        f"value_name={step.value_name!r})")

            elif isinstance(step, Pivot):
                src = frame(step.input, "input")
                cols(src, step.index + [step.columns, step.values], "index", step.input)
                if step.func in NUMERIC_AGGS and src[step.values] != "num":
                    raise _Fail("values", f"{step.func} needs numeric values")
                schema = {c: src[c] for c in step.index}
                roles[out] = {c: roles[step.input].get(c, "") for c in step.index}
                opaque.add(out)
                code = (f"xl.pivot({step.input}, {list(step.index)!r}, {step.columns!r}, "
                        f"{step.values!r}, {step.func!r})")
            else:  # pragma: no cover - exhaustive over Step
                raise _Fail("op", f"unknown op {step!r}")

            schemas[out] = schema
            lines.append(f"    {out} = {code}")
        except _Fail as f:
            diags.append(Diagnostic(step=i, field=f.field, message=f.message, hint=f.hint))
            # keep going with an empty schema so later steps report their own errors
            if getattr(step, "out", None) and step.out not in schemas:
                schemas[step.out] = {}
                opaque.add(step.out)
                roles[step.out] = {}

    if program.output not in schemas:
        diags.append(Diagnostic(step=None, field="output", message=f"output frame {program.output!r} is not defined",
                                hint=_suggest(program.output, list(schemas)) if schemas else None))
    else:
        used = set()
        for s in program.steps:
            for attr in ("input", "left", "right"):
                if hasattr(s, attr):
                    used.add(getattr(s, attr))
        dead = [s.out for s in program.steps if s.out not in used and s.out != program.output]
        if dead:
            diags.append(Diagnostic(step=None, field="steps", severity="warning",
                                    message=f"frames never used: {', '.join(dead)}"))
        ch = program.chart
        if ch.kind != "none" and program.output not in opaque:
            out_schema = schemas[program.output]
            for axis, c in (("chart.x", ch.x), ("chart.y", ch.y)):
                if c not in out_schema:
                    diags.append(Diagnostic(step=None, field=axis, message=f"chart column {c!r} not in output",
                                            hint=_suggest(c, list(out_schema))))
            if ch.y in out_schema and out_schema[ch.y] != "num":
                diags.append(Diagnostic(step=None, field="chart.y", message="chart y must be numeric"))

    ok = not any(d.severity == "error" for d in diags)
    if ok:
        lines.append(f"    return {program.output}")
    return Compiled(ok=ok, diagnostics=diags, source="\n".join(lines) + "\n" if ok else "",
                    schemas=schemas, tables_used=sorted(set(tables_used)))
