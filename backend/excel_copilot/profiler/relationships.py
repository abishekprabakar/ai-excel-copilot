"""Infer how tables connect: value-containment foreign keys and formula lookups."""
from __future__ import annotations

import re

import pandas as pd

from .models import Relationship, TableProfile

_KEY_TYPES = {"integer", "string"}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _singular(s: str) -> str:
    return s[:-1] if s.endswith("s") and not s.endswith("ss") else s


def name_score(from_col: str, to_table: TableProfile, to_col: str) -> float:
    """1.0 for identical names, 0.8 for '<table>_id' -> 'id', 0.6 for containment, else 0."""
    a, b = _norm(from_col), _norm(to_col)
    tname = _singular(_norm(to_table.id))
    if a == b:
        return 1.0
    if b in ("id", "key", "code") and a in (tname + b, tname):
        return 0.8
    if len(b) >= 3 and (a.endswith(b) or b.endswith(a)) and len(a) >= 3:
        return 0.6
    return 0.0


def infer_relationships(tables: list[TableProfile], frames: dict[str, pd.DataFrame],
                        lookups: list[tuple[str, list[str], str, tuple[str, str, str]]]) -> list[Relationship]:
    rels: list[Relationship] = []
    seen: set[tuple[str, str, str, str]] = set()

    # 1) formula lookups: VLOOKUP(D{r}, Products!$A..) => orders.SKU -> products.<col A>
    by_sheet = {}
    for t in tables:
        by_sheet.setdefault(t.sheet.lower(), []).append(t)
    for tid, letters, _derived, (key_letter, sheet, target_letter) in lookups:
        src = next(t for t in tables if t.id == tid)
        key_col = next((c for c in src.columns if c.letter == key_letter), None)
        for tgt in by_sheet.get(sheet.lower(), []):
            to_col = next((c for c in tgt.columns if c.letter == target_letter), None)
            if key_col and to_col:
                cont = _containment(frames[tid][key_col.name], frames[tgt.id][to_col.name])
                key = (tid, key_col.name, tgt.id, to_col.name)
                if key not in seen:
                    seen.add(key)
                    rels.append(Relationship(from_table=tid, from_column=key_col.name, to_table=tgt.id,
                                             to_column=to_col.name, kind="formula_lookup",
                                             containment=round(cont, 4), confidence=0.99))

    # 2) value containment: from-column values found in a unique to-column
    for src in tables:
        for fc in src.columns:
            if fc.type not in _KEY_TYPES or fc.non_null == 0 or fc.role not in ("identifier", "dimension"):
                continue
            for tgt in tables:
                if tgt.id == src.id:
                    continue
                for tc in tgt.columns:
                    if tc.type != fc.type or not tc.unique or tc.non_null < 2:
                        continue
                    ns = name_score(fc.name, tgt, tc.name)
                    if ns == 0 or (src.id, fc.name, tgt.id, tc.name) in seen:
                        continue
                    if fc.unique and tc.unique and src.n_rows >= tgt.n_rows and ns < 1:
                        continue
                    cont = _containment(frames[src.id][fc.name], frames[tgt.id][tc.name])
                    if cont < 0.8:
                        continue
                    seen.add((src.id, fc.name, tgt.id, tc.name))
                    rels.append(Relationship(from_table=src.id, from_column=fc.name, to_table=tgt.id,
                                             to_column=tc.name, kind="foreign_key",
                                             containment=round(cont, 4), confidence=round(cont * ns, 4)))
    rels.sort(key=lambda r: -r.confidence)
    return rels


def _containment(a: pd.Series, b: pd.Series) -> float:
    av = set(a.dropna().astype(str))
    bv = set(b.dropna().astype(str))
    return len(av & bv) / len(av) if av else 0.0
