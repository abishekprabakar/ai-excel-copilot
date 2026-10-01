"""The coder's wire format: one flat step shape instead of a 10-way union.

Constrained decoding compiles the output schema into a grammar. A list of a
10-member `anyOf` union, each member with its own nested objects, compiled to a
grammar the API rejects as too large. The wire format keeps the grammar small:
every step is the same object with every field present (unused fields are
"" / [] / 0), and only `op` is an enum. `to_program` then converts it into the
strict, typed `Program` union, reporting problems per step so they can be fed
back to the model.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .models import (Aggregate, Chart, Derive, Filter, Join, Limit, Load, Pivot, Program, Select,
                     Sort, Unpivot)

OPS = ("load", "filter", "derive", "select", "aggregate", "join", "sort", "limit", "unpivot", "pivot")

# fields each op uses (everything else must be left empty)
OP_FIELDS: dict[str, tuple[str, ...]] = {
    "load": ("table",),
    "filter": ("input", "condition"),
    "derive": ("input", "column", "expression"),
    "select": ("input", "columns"),
    "aggregate": ("input", "group_by", "aggregations"),
    "join": ("left", "right", "left_on", "right_on", "how"),
    "sort": ("input", "by"),
    "limit": ("input", "n"),
    "unpivot": ("input", "id_columns", "value_columns", "var_name", "value_name"),
    "pivot": ("input", "index", "pivot_columns", "values", "func"),
}


class WireAgg(BaseModel):
    column: str
    func: str = Field(description="sum mean median min max count nunique std")
    as_name: str


class WireSortKey(BaseModel):
    column: str
    descending: bool


class WireStep(BaseModel):
    op: Literal["load", "filter", "derive", "select", "aggregate", "join", "sort", "limit", "unpivot", "pivot"]
    out: str
    input: str
    table: str
    condition: str
    column: str
    expression: str
    columns: list[str]
    group_by: list[str]
    aggregations: list[WireAgg]
    left: str
    right: str
    left_on: list[str]
    right_on: list[str]
    how: str
    by: list[WireSortKey]
    n: int
    id_columns: list[str]
    value_columns: list[str]
    var_name: str
    value_name: str
    index: list[str]
    pivot_columns: str
    values: str
    func: str


class WireProgram(BaseModel):
    steps: list[WireStep]
    output: str
    title: str
    chart_kind: str = Field(description="none | bar | line")
    chart_x: str
    chart_y: str


class WireError(ValueError):
    """Conversion problems, already formatted for the model."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


_STEP_CLS = {"load": Load, "filter": Filter, "derive": Derive, "select": Select, "aggregate": Aggregate,
             "join": Join, "sort": Sort, "limit": Limit, "unpivot": Unpivot, "pivot": Pivot}


def _fmt(prefix: str, e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        field = ".".join(str(x) for x in err["loc"])
        out.append(f"{prefix} {field}: {err['msg']} (got {err.get('input')!r})")
    return out


def to_program(w: WireProgram) -> Program:
    steps, problems = [], []
    for i, s in enumerate(w.steps):
        d = {"op": s.op, "out": s.out}
        for f in OP_FIELDS[s.op]:
            v = getattr(s, f)
            if f == "pivot_columns":
                d["columns"] = v
            elif f in ("aggregations", "by"):
                d[f] = [x.model_dump() for x in v]
            else:
                d[f] = v
        try:
            steps.append(_STEP_CLS[s.op].model_validate(d))    # validate against the op's own model
        except ValidationError as e:
            problems += _fmt(f"step {i} ({s.op})", e)
    try:
        chart = Chart.model_validate({"kind": (w.chart_kind or "none").strip().lower(), "x": w.chart_x, "y": w.chart_y})
    except ValidationError as e:
        problems += _fmt("chart", e)
        chart = None
    if problems:
        raise WireError(problems[:10])
    return Program(steps=steps, output=w.output, title=w.title, chart=chart)


def from_program(p: Program) -> WireProgram:
    """Inverse of to_program (used for repair turns and tests)."""
    blank = {f: "" for f in WireStep.model_fields}
    for f in ("columns", "group_by", "aggregations", "left_on", "right_on", "by", "id_columns", "value_columns", "index"):
        blank[f] = []
    blank["n"] = 0
    steps = []
    for s in p.steps:
        d = dict(blank)
        dumped = s.model_dump()
        for k, v in dumped.items():
            if s.op == "pivot" and k == "columns":
                d["pivot_columns"] = v
            else:
                d[k] = v
        steps.append(WireStep.model_validate(d))
    return WireProgram(steps=steps, output=p.output, title=p.title, chart_kind=p.chart.kind,
                       chart_x=p.chart.x, chart_y=p.chart.y)

