"""Context graph: the profile as nodes/edges, plus a compact, token-budgeted rendering.

The full profile JSON for a modest workbook is tens of thousands of tokens; an
LLM needs a few hundred. `render_context` produces a dense text view that
(1) ranks tables by relevance to the question, (2) degrades detail per table in
steps (full stats -> type+role -> names only -> table line -> dropped) starting
with the least relevant table until the estimate fits the budget, (3) collapses
wide period columns (Jan..Dec) into one line and (4) never shows raw PII.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel

from .profiler.models import ColumnProfile, TableProfile, WorkbookProfile
from .profiler.tables import is_period_label

NodeKind = Literal["workbook", "sheet", "table", "column", "named_range"]
EdgeKind = Literal["contains", "foreign_key", "formula_lookup", "derived_from", "references"]


class Node(BaseModel):
    id: str
    kind: NodeKind
    label: str
    attrs: dict[str, str | int | float | bool | None] = {}


class Edge(BaseModel):
    source: str
    target: str
    kind: EdgeKind
    weight: float = 1.0


class ContextGraph(BaseModel):
    nodes: list[Node]
    edges: list[Edge]


def build_graph(p: WorkbookProfile) -> ContextGraph:
    nodes = [Node(id="wb", kind="workbook", label=p.file_name, attrs={"sheets": len(p.sheets)})]
    edges: list[Edge] = []
    for s in p.sheets:
        sid = f"sheet:{s.name}"
        nodes.append(Node(id=sid, kind="sheet", label=s.name, attrs={"state": s.state}))
        edges.append(Edge(source="wb", target=sid, kind="contains"))
    for t in p.tables:
        tid = f"table:{t.id}"
        nodes.append(Node(id=tid, kind="table", label=t.id,
                          attrs={"range": f"{t.sheet}!{t.range}", "rows": t.n_rows, "layout": t.layout}))
        edges.append(Edge(source=f"sheet:{t.sheet}", target=tid, kind="contains"))
        by_letter = {c.letter: c.name for c in t.columns}
        for c in t.columns:
            cid = f"col:{t.id}.{c.name}"
            nodes.append(Node(id=cid, kind="column", label=c.name,
                              attrs={"type": c.type, "role": c.role, "pii": c.pii, "formula": c.formula_readable}))
            edges.append(Edge(source=tid, target=cid, kind="contains"))
            if c.formula:
                for letter in sorted(set(re.findall(r"(?<![A-Za-z$!])\$?([A-Z]{1,3})\{r\}", c.formula))):
                    if letter in by_letter and by_letter[letter] != c.name:
                        edges.append(Edge(source=cid, target=f"col:{t.id}.{by_letter[letter]}", kind="derived_from"))
    for r in p.relationships:
        edges.append(Edge(source=f"col:{r.from_table}.{r.from_column}", target=f"col:{r.to_table}.{r.to_column}",
                          kind=r.kind, weight=r.confidence))
    for n in p.named_ranges:
        nid = f"name:{n.name}"
        nodes.append(Node(id=nid, kind="named_range", label=n.name, attrs={"refers_to": n.refers_to, "value": n.value}))
        sheet = n.refers_to.split("!")[0].strip("'=")
        if any(s.name == sheet for s in p.sheets):
            edges.append(Edge(source=nid, target=f"sheet:{sheet}", kind="references"))
    return ContextGraph(nodes=nodes, edges=edges)


# ---------------------------------------------------------------- compact rendering
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "by", "in", "for", "and", "or", "to", "per", "what", "which", "how",
         "is", "are", "was", "were", "show", "me", "list", "each", "with", "from", "on", "top", "total",
         "all", "do", "does", "did", "we", "our", "my", "much", "many", "give", "find"}


def estimate_tokens(text: str) -> int:
    """~3.5 characters per token for this dense, symbol-heavy format (conservative)."""
    return int(len(text) / 3.5) + 1


def _terms(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        out.add(w)
        if w.endswith("ies") and len(w) > 4:
            out.add(w[:-3] + "y")
        elif w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
    return out


def relevance(t: TableProfile, q_terms: set[str]) -> float:
    if not q_terms:
        return 0.0
    score = 0.0
    if q_terms & _terms(t.id + " " + t.name + " " + t.sheet):
        score += 3
    for c in t.columns:
        if q_terms & _terms(c.name):
            score += 2 if c.role in ("measure", "temporal") else 1.5
        for v, _ in c.top_values[:12]:
            if q_terms & _terms(v):
                score += 1
                break
    return score


def _num(x: float) -> str:
    if abs(x) >= 1e6:
        return f"{x / 1e6:.3g}M"
    if abs(x) >= 1e4:
        return f"{x / 1e3:.3g}k"
    return f"{x:.4g}"


def _rels_for(p: WorkbookProfile, t: TableProfile) -> dict[str, str]:
    out = {}
    for r in p.relationships:
        if r.from_table == t.id:
            arrow = "~>" if r.kind == "formula_lookup" else "->"
            out[r.from_column] = f"{arrow} {r.to_table}.{r.to_column}" + (
                f" ({r.containment:.0%} match)" if r.containment < 0.999 else "")
    return out


def render_column(c: ColumnProfile, level: int, rel: str | None) -> str:
    role = {"identifier": "id", "dimension": "dim", "measure": "measure", "temporal": "time",
            "text": "text", "flag": "flag", "empty": "empty"}[c.role]
    s = f"  {c.name}: {c.type} {role}"
    if c.unique and c.role == "identifier":
        s += " unique"
    if rel:
        s += f" {rel.split(' (')[0] if level == 1 else rel}"
    if level == 1:
        return s
    if c.numeric and c.role == "measure" and (level >= 3 or not c.formula_readable):
        s += f" {_num(c.numeric.min)}..{_num(c.numeric.max)}"
        if level >= 3:
            s += f" avg {_num(c.numeric.mean)}"
    if c.formula_readable:
        s += f" {c.formula_readable}"
    elif c.date_min:
        s += f" {c.date_min}..{c.date_max}"
    if c.top_values and c.role in ("dimension", "flag"):
        k = 12 if level >= 3 else 5
        vals = ", ".join(v for v, _ in c.top_values[:k])
        more = "" if c.distinct <= k else f", +{c.distinct - k}"
        s += f" {{{vals}{more}}}"
    elif level >= 3 and c.samples and c.role in ("text", "identifier") and not c.unique:
        s += " e.g. " + "; ".join(c.samples[:2])
    elif level >= 3 and c.pii:
        s += f" [PII:{c.pii}, e.g. {c.samples[0] if c.samples else ''}]"
    if c.nulls and c.non_null:
        s += f" nulls={c.nulls}"
    if level >= 3:
        for i in c.issues:
            if i.code not in ("pii",):
                s += f" !{i.code}({i.count})"
    return s


def render_table(p: WorkbookProfile, t: TableProfile, level: int) -> str:
    head = f"TABLE {t.id} [{t.sheet}!{t.range}] {t.n_rows} rows"
    if t.title:
        head += f' "{t.title}"'
    if t.layout == "wide":
        head += " WIDE"
    if level == 0:
        return head + f" cols: {', '.join(c.name for c in t.columns[:12])}" + ("..." if len(t.columns) > 12 else "")
    rels = _rels_for(p, t)
    lines = [head]
    period_run: list[ColumnProfile] = []

    def flush():
        if len(period_run) >= 4:
            a, b = period_run[0], period_run[-1]
            lines.append(f"  {a.name}..{b.name} ({len(period_run)} period cols): {a.type} measure")
        else:
            lines.extend(render_column(c, level, rels.get(c.name)) for c in period_run)
        period_run.clear()

    for c in t.columns:
        if is_period_label(c.header or c.name) and c.role == "measure":
            period_run.append(c)
            continue
        flush()
        lines.append(render_column(c, level, rels.get(c.name)))
    flush()
    return "\n".join(lines)


@dataclass
class RenderedContext:
    text: str
    tokens: int
    levels: dict[str, int]          # table id -> detail level used (-1 = dropped)


def render_context(p: WorkbookProfile, question: str | None = None, budget_tokens: int = 1200,
                   only_tables: list[str] | None = None, min_level: int = 0,
                   counter: Callable[[str], int] = estimate_tokens) -> RenderedContext:
    """Render the workbook for an LLM within `budget_tokens`.

    `only_tables` restricts output (used for the coder stage). `min_level` stops
    degradation at that level (the coder needs at least names+types: level 1).
    """
    tables = [t for t in p.tables if only_tables is None or t.id in only_tables]
    q = _terms(question or "")
    direct = {t.id: relevance(t, q) for t in tables}
    scores = dict(direct)
    for r in p.relationships:   # neighbours of relevant tables matter too (join paths)
        if direct.get(r.from_table, 0) > 0 and r.to_table in scores:
            scores[r.to_table] += 0.5
        if direct.get(r.to_table, 0) > 0 and r.from_table in scores:
            scores[r.from_table] += 0.5
    order = sorted(tables, key=lambda t: (scores[t.id], t.n_rows), reverse=True)
    levels = {t.id: 3 for t in tables}
    restricted = only_tables is not None

    def tier(tid: str) -> int:
        if restricted or direct[tid] > 0:
            return 2          # relevant: keep as much detail as possible
        return 1 if scores[tid] > 0 else 0   # 1 = join neighbour, 0 = unrelated

    def next_move() -> str | None:
        """Degradation schedule: unrelated tables to one line, then neighbours, then
        relevant tables evenly (highest level first, least relevant first), then
        drop unrelated/neighbour tables entirely."""
        for want_tier in (0, 1):
            cands = [t for t in reversed(order) if tier(t.id) == want_tier and levels[t.id] > 0]
            if cands:
                return cands[0].id
        def relevant_down_to(floor: int) -> str | None:
            cands = [t for t in reversed(order) if tier(t.id) == 2 and levels[t.id] > floor]
            if not cands:
                return None
            top = max(levels[t.id] for t in cands)
            return next(t.id for t in cands if levels[t.id] == top)
        move = relevant_down_to(max(min_level, 1))
        if move:
            return move
        for want_tier in (0, 1):
            cands = [t for t in reversed(order) if tier(t.id) == want_tier and levels[t.id] > -1]
            if cands:
                return cands[0].id
        return relevant_down_to(min_level)
        return None

    def compose() -> str:
        hidden = [s.name for s in p.sheets if s.state != "visible"]
        head = f"WORKBOOK {p.file_name}: {len(p.sheets)} sheets, {len(p.tables)} tables"
        if hidden:
            head += f"; hidden sheets: {', '.join(hidden)}"
        parts = [head]
        for t in order:
            if levels[t.id] >= 0:
                parts.append(render_table(p, t, levels[t.id]))
        if only_tables is None:
            for n in p.named_ranges[:10]:
                parts.append(f"NAME {n.name} = {n.refers_to}" + (f" ({n.value})" if n.value else ""))
            for s in p.sheets:
                if s.loose_cells:
                    cells = ", ".join(f"{a}={v}" for a, v in s.loose_cells[:6])
                    parts.append(f"NOTE {s.name}: {cells}")
        if any(lv < 3 for lv in levels.values()):
            parts.append("(detail reduced to fit; ask for a table to see it in full)")
        return "\n".join(parts)

    text = compose()
    while counter(text) > budget_tokens:
        tid = next_move()
        if tid is None:
            break
        levels[tid] -= 1
        text = compose()
    return RenderedContext(text=text, tokens=counter(text), levels=levels)
