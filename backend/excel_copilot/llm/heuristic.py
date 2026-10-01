"""Offline backend: a deterministic, rule-based planner/coder.

It exists so the whole system (API, streaming, sandbox, artifacts, UI) runs and
is testable without an API key, and as a fallback. It understands a useful but
narrow family of questions:

    total/average/median/max/min <measure> [by <dimension|month|quarter|year>]
    [in <year>|Q<n>|<month>] [for <category value>...] [top|bottom N]
    how many <entity> [by ...]

It emits exactly the same Plan and Program objects as the Claude backend, so
everything downstream (validation, compilation, sandboxing) is identical.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ir.models import (Aggregate, Aggregation, Chart, Derive, Filter, Join, Limit, Load, Plan,
                         Program, Sort, SortKey)
from ..profiler.models import ColumnProfile, TableProfile, WorkbookProfile
from .base import LLMError, Turn, Usage

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_SYNONYMS = {"sales": ["revenue", "sales", "amount", "total"], "turnover": ["revenue", "sales"],
             "income": ["revenue", "profit"], "spend": ["cost", "amount"], "price": ["price"],
             "quantity": ["units", "qty", "quantity"], "volume": ["units", "quantity"],
             "margin": ["margin"], "earnings": ["revenue", "profit"]}
_AGG_WORDS = [("median", "median"), ("average", "mean"), ("avg", "mean"), ("mean", "mean"),
              ("maximum", "max"), ("max", "max"), ("minimum", "min"), ("min", "min"),
              ("std", "std"), ("deviation", "std")]
_GRAINS = {"month": "month", "monthly": "month", "quarter": "quarter", "quarterly": "quarter",
           "year": "year", "yearly": "year", "annual": "year", "annually": "year"}


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9%]+", s.lower())


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _col_score(col: ColumnProfile, qwords: set[str]) -> float:
    toks = [_stem(t) for t in _words(col.name) if t not in ("of", "the")]
    if not toks:
        return 0.0
    hit = sum(1 for t in toks if t in qwords)
    return hit / len(toks) + 0.05 * hit if hit else 0.0


@dataclass
class Analysis:
    base: TableProfile
    measure: ColumnProfile | None
    func: str
    count_entity: ColumnProfile | None = None
    group: tuple[TableProfile, ColumnProfile] | None = None
    grain: str | None = None
    filters: list[tuple[TableProfile, str, str]] = field(default_factory=list)   # (table, column, condition)
    top_n: int | None = None
    descending: bool = True
    notes: list[str] = field(default_factory=list)


class HeuristicBackend:
    name = "offline"

    # ------------------------------------------------------------------ analysis
    def analyse(self, question: str, p: WorkbookProfile) -> Analysis:
        q = question.lower()
        words = [_stem(w) for w in _words(q)]
        qset = set(words)
        for w in list(qset):
            for syn in _SYNONYMS.get(w, []):
                qset.add(syn)
        if not p.tables:
            raise LLMError("the workbook has no tables")

        # measure: best-matching measure column anywhere
        best: tuple[float, TableProfile, ColumnProfile] | None = None
        for t in p.tables:
            if t.layout == "wide":
                continue
            for c in t.columns:
                if c.role != "measure":
                    continue
                s = _col_score(c, qset) + (0.1 if c.formula else 0) + 0.01 * t.n_rows / max(1, max(x.n_rows for x in p.tables))
                if _col_score(c, qset) and (best is None or s > best[0]):
                    best = (s, t, c)

        counting = bool(re.search(r"\bhow many\b|\bnumber of\b|\bcount\b", q))
        func = "sum"
        for word, f in _AGG_WORDS:
            if re.search(rf"\b{word}\b", q):
                func = f
                break

        top = re.search(r"\b(top|bottom|best|worst|largest|smallest)\s+(\d{1,4})\b", q)
        top_n = int(top.group(2)) if top else None
        descending = not (top and top.group(1) in ("bottom", "worst", "smallest"))
        if top_n is None and re.search(r"\b(highest|most|largest|biggest|best|top)\b", q) and re.search(r"\bwhich\b|\bwhat\b", q):
            top_n = 1
        if top_n is None and re.search(r"\b(lowest|least|smallest|worst)\b", q) and re.search(r"\bwhich\b|\bwhat\b", q):
            top_n, descending = 1, False

        # base table
        if best and not counting:
            base, measure = best[1], best[2]
        else:
            measure = best[2] if best and not counting else None
            named = [t for t in p.tables if _stem(t.id.split("_")[0]) in qset or _stem(t.name.lower()) in qset]
            base = (best[1] if best else None) or (named[0] if named else max(p.tables, key=lambda t: t.n_rows))
            if counting:
                # "how many customers" over orders -> distinct Customer ID; over customers -> rows
                base = max([t for t in p.tables if t.layout == "long"], key=lambda t: t.n_rows)
                func = "count"
        a = Analysis(base=base, measure=measure, func=func, top_n=top_n, descending=descending)
        if counting:
            for c in base.columns:
                if c.role == "identifier" and not c.unique and _stem(_words(c.name)[0]) in qset:
                    a.count_entity = c
                    a.func = "nunique"
                    break
            else:
                ent = next((t for t in p.tables if _stem(t.id) in qset and t.id != base.id), None)
                if ent:
                    a.base = ent
        if a.measure is None and not counting:
            mentions = any(_col_score(c, qset) for t in p.tables for c in t.columns) or any(
                _stem(t.id) in qset for t in p.tables)
            if not mentions:
                measures = sorted({c.name for t in p.tables if t.layout == "long" for c in t.columns if c.role == "measure"})
                raise LLMError("the question does not mention anything in this workbook; "
                               f"measures available: {', '.join(measures[:12])}")
            # fall back to the most "money-like" measure of the biggest table
            cands = [c for c in base.columns if c.role == "measure"]
            cands.sort(key=lambda c: (c.type == "currency", bool(c.formula), c.name.lower() in ("revenue", "sales", "amount")), reverse=True)
            if not cands:
                raise LLMError("could not find a numeric column to aggregate; name one in the question")
            a.measure = cands[0]
            a.notes.append(f"no measure named in the question; using {base.id}.{cands[0].name}")

        # group by: phrase after by/per/for each/across, or "which <col>"
        m = re.search(r"\b(?:by|per|for each|across|which|what)\s+([a-z0-9 %]+?)(?=\s+(?:in|for|during|where|with|had|has|have|is|was|were|did|does|sold|generated|made|of)\b|[?.,]|$)", q)
        grain = next((g for w, g in _GRAINS.items() if re.search(rf"\b{w}\b", q)), None)
        topx = re.search(r"\b(?:top|bottom|best|worst)\s+\d{1,4}\s+([a-z0-9 %]+?)\s+(?:by|in|for|with)\b", q)
        if topx:
            phrase = set(_stem(w) for w in _words(topx.group(1)))
            cand = self._find_column(p, a.base, phrase, roles=("dimension", "identifier", "text", "temporal", "flag"))
            if cand:
                a.group = cand
        if m and a.group is None:
            phrase = set(_stem(w) for w in _words(m.group(1)))
            if grain and phrase & set(_GRAINS):
                a.grain = grain
            else:
                cand = self._find_column(p, a.base, phrase, roles=("dimension", "identifier", "text", "temporal", "flag"))
                if cand:
                    a.group = cand
        if a.grain is None and grain and re.search(r"\b(monthly|quarterly|yearly|annual|each month|each quarter|each year|per month|per quarter|per year)\b", q):
            a.grain = grain

        # filters
        temporal = next((c for c in a.base.columns if c.role == "temporal"), None)
        years = re.findall(r"\b(19\d{2}|20\d{2})\b", q)
        if years and temporal:
            ys = sorted(set(int(y) for y in years))
            cond = f"year(`{temporal.name}`) == {ys[0]}" if len(ys) == 1 else f"between(year(`{temporal.name}`), {ys[0]}, {ys[-1]})"
            a.filters.append((a.base, temporal.name, cond))
        qs = re.findall(r"\bq([1-4])\b", q)
        if qs and temporal:
            a.filters.append((a.base, temporal.name, f"quarter(`{temporal.name}`) == {int(qs[0])}"))
        for i, mname in enumerate(_MONTHS):
            if re.search(rf"\b(in|during)\s+{mname}\b", q) and temporal:
                a.filters.append((a.base, temporal.name, f"month(`{temporal.name}`) == {i + 1}"))
        group_col = a.group[1] if a.group else None
        candidates = [a.base] + [t for t in p.tables if t.id != a.base.id and self._link(p, a.base, t)]
        filtered_names: set[str] = set()
        for t in candidates:
            for c in t.columns:
                if c.role not in ("dimension", "flag") or c is group_col or not c.top_values \
                        or c.name in filtered_names or (group_col is not None and c.name == group_col.name):
                    continue
                hits = [v for v, _ in c.top_values if v and re.search(rf"\b{re.escape(v.lower())}\b", q)]
                if hits and not (c.role == "flag"):
                    neg = re.search(rf"\b(excluding|except|not|without|non)[- ]+{re.escape(hits[0].lower())}", q)
                    if len(hits) == 1:
                        op = "!=" if neg else "=="
                        cond = f"`{c.name}` {op} {hits[0]!r}"
                    else:
                        cond = f"`{c.name}` in {hits!r}"
                    a.filters.append((t, c.name, cond))
                    filtered_names.add(c.name)
        return a

    def _link(self, p: WorkbookProfile, a: TableProfile, b: TableProfile):
        for r in p.relationships:
            if r.from_table == a.id and r.to_table == b.id:
                return (r.from_column, r.to_column)
            if r.to_table == a.id and r.from_table == b.id:
                return (r.to_column, r.from_column)
        return None

    def _find_column(self, p: WorkbookProfile, base: TableProfile, phrase: set[str], roles) -> tuple[TableProfile, ColumnProfile] | None:
        best = None
        for t in [base] + [t for t in p.tables if t.id != base.id and self._link(p, base, t)]:
            for c in t.columns:
                if c.role not in roles or c.pii:
                    continue
                s = _col_score(c, phrase) - (0.01 if t.id != base.id else 0)
                if s > 0 and (best is None or s > best[0]):
                    best = (s, t, c)
        return (best[1], best[2]) if best else None

    # ------------------------------------------------------------------ backend API
    def plan(self, question: str, context: str, profile: WorkbookProfile) -> tuple[Plan, Usage]:
        try:
            a = self.analyse(question, profile)
        except LLMError as e:
            return Plan(answerable=False, reason=str(e), tables=[], columns=[], steps=[], assumptions=[]), Usage(model="offline-heuristic")
        tables = sorted({a.base.id} | {t.id for t, _, _ in a.filters} | ({a.group[0].id} if a.group else set()))
        cols = []
        if a.measure:
            cols.append(f"{a.base.id}.{a.measure.name}")
        if a.group:
            cols.append(f"{a.group[0].id}.{a.group[1].name}")
        steps = [f"load {a.base.id}"]
        steps += [f"filter {t.id}: {cond}" for t, _, cond in a.filters]
        if a.group and a.group[0].id != a.base.id:
            steps.append(f"join {a.base.id} to {a.group[0].id}")
        what = (f"count distinct {a.count_entity.name}" if a.count_entity else
                "count rows" if a.func == "count" else f"{a.func} of {a.measure.name}")  # type: ignore[union-attr]
        by = f" by {a.grain}" if a.grain else (f" by {a.group[1].name}" if a.group else "")
        steps.append(what + by)
        if a.top_n:
            steps.append(f"keep the {'top' if a.descending else 'bottom'} {a.top_n}")
        return Plan(answerable=True, reason="", tables=tables, columns=cols, steps=steps,
                    assumptions=a.notes + ["offline rule-based planner; set ANTHROPIC_API_KEY for Claude"]), Usage(model="offline-heuristic")

    def code(self, question: str, plan: Plan, context: str, profile: WorkbookProfile,
             history: list[Turn]) -> tuple[Program, Usage]:
        if history:
            detail = history[-1].content.strip().splitlines()
            raise LLMError("the offline planner cannot repair its own program: "
                           + (detail[1].lstrip("- ") if len(detail) > 1 else "validation failed"))
        a = self.analyse(question, profile)
        steps: list = [Load(op="load", out="base", table=a.base.id)]
        cur = "base"
        k = 0

        def nxt(prefix: str) -> str:
            nonlocal k
            k += 1
            return f"{prefix}_{k}"

        joined: dict[str, str] = {}
        def ensure_join(t: TableProfile) -> None:
            nonlocal cur
            if t.id == a.base.id or t.id in joined:
                return
            link = self._link(profile, a.base, t)
            if not link:
                raise LLMError(f"no relationship between {a.base.id} and {t.id}")
            name = f"{t.id}_t"
            steps.append(Load(op="load", out=name, table=t.id))
            out = nxt("joined")
            steps.append(Join(op="join", out=out, left=cur, right=name, left_on=[link[0]], right_on=[link[1]], how="left"))
            joined[t.id] = name
            cur = out

        for t, _, _ in a.filters:
            ensure_join(t)
        if a.group:
            ensure_join(a.group[0])
        for t, _, cond in a.filters:
            out = nxt("filtered")
            steps.append(Filter(op="filter", out=out, input=cur, condition=cond))
            cur = out

        group_by: list[str] = []
        if a.grain:
            temporal = next((c for c in a.base.columns if c.role == "temporal"), None)
            if temporal is None:
                raise LLMError(f"{a.base.id} has no date column to group by {a.grain}")
            label = {"month": "Month", "quarter": "Quarter", "year": "Year"}[a.grain]
            out = nxt("with_period")
            steps.append(Derive(op="derive", out=out, input=cur, column=label, expression=f"{a.grain}(`{temporal.name}`)"))
            cur = out
            group_by = [label]
        elif a.group:
            name = a.group[1].name
            if a.group[0].id != a.base.id and name in {c.name for c in a.base.columns}:
                name = f"{name}_right"
            group_by = [name]

        if a.count_entity:
            agg = Aggregation(column=a.count_entity.name, func="nunique", as_name=f"Distinct {a.count_entity.name}")
        elif a.func == "count":
            agg = Aggregation(column="*", func="count", as_name="Count")
        else:
            assert a.measure is not None
            label = a.measure.name if a.func == "sum" else f"{a.func.title()} {a.measure.name}"
            agg = Aggregation(column=a.measure.name, func=a.func, as_name=label)  # type: ignore[arg-type]
        out = nxt("agg")
        steps.append(Aggregate(op="aggregate", out=out, input=cur, group_by=group_by, aggregations=[agg]))
        cur = out
        if group_by:
            out = nxt("sorted")
            by = [SortKey(column=group_by[0], descending=False)] if a.grain and not a.top_n else \
                 [SortKey(column=agg.as_name, descending=a.descending)]
            steps.append(Sort(op="sort", out=out, input=cur, by=by))
            cur = out
        if a.top_n and group_by:
            out = nxt("top")
            steps.append(Limit(op="limit", out=out, input=cur, n=a.top_n))
            cur = out
        chart = Chart(kind=("line" if a.grain else "bar") if group_by else "none",
                      x=group_by[0] if group_by else "", y=agg.as_name if group_by else "")
        title = f"{agg.as_name}" + (f" by {group_by[0]}" if group_by else "")
        return Program(steps=steps, output=cur, title=title, chart=chart), Usage(model="offline-heuristic")
