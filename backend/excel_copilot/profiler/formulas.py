"""Formula analysis: normalise filled-down formulas and extract what they reference.

Excel stores each cell's formula with absolute row numbers (=F4*G4 on row 4,
=F5*G5 on row 5). To recognise a *column* formula we rewrite every relative
row reference as an offset from the formula's own row: =F{r}*G{r}. Operands are
found with openpyxl's formula tokenizer, so string literals and function names
are never mistaken for references.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from openpyxl.formula.tokenizer import Token, Tokenizer

_CELL = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)(\d+)")
_VLOOKUP = re.compile(
    r"^=.*?VLOOKUP\(\s*\$?([A-Z]{1,3})\{r\}\s*,\s*('?)([^'!]+)\2!\$?([A-Z]{1,3})\$?\d*:\$?[A-Z]{1,3}\$?\d*",
    re.I)
_XLOOKUP = re.compile(
    r"^=.*?XLOOKUP\(\s*\$?([A-Z]{1,3})\{r\}\s*,\s*('?)([^'!]+)\2!\$?([A-Z]{1,3})\$?\d*(?::\$?[A-Z]{1,3}\$?\d*)?",
    re.I)
_CONSTANT = re.compile(r"^=\s*(TRUE|FALSE)(\(\))?\s*$|^=\s*-?[\d.]+\s*$|^=\s*\"[^\"]*\"\s*$", re.I)
ERROR_LITERALS = {"#DIV/0!", "#N/A", "#REF!", "#VALUE!", "#NAME?", "#NUM!", "#NULL!",
                  "#SPILL!", "#CALC!", "#GETTING_DATA"}


def _rewrite_ref(ref: str, row: int) -> str:
    """Rewrite one range operand (maybe sheet-qualified) relative to `row`."""
    if "!" in ref:
        sheet, _, rng = ref.rpartition("!")
        prefix = sheet + "!"
    else:
        prefix, rng = "", ref

    def sub(m: re.Match) -> str:
        col_abs, col, row_abs, r = m.groups()
        if row_abs:
            return f"{col_abs}{col.upper()}${r}"
        off = int(r) - row
        tok = "{r}" if off == 0 else f"{{r{off:+d}}}"
        return f"{col_abs}{col.upper()}{tok}"

    return prefix + _CELL.sub(sub, rng)


def normalise(formula: str, row: int) -> tuple[str, list[str]]:
    """Return (row-relative formula, list of range operands as written)."""
    try:
        tok = Tokenizer(formula)
    except Exception:  # unparsable formula: keep it verbatim
        return formula, []
    out, refs = ["="], []
    for t in tok.items:
        if t.type == Token.OPERAND and t.subtype == Token.RANGE:
            refs.append(t.value)
            out.append(_rewrite_ref(t.value, row))
        else:
            out.append(t.value)
    return "".join(out), refs


@dataclass
class ColumnFormulas:
    formula: str | None = None
    coverage: float = 0.0
    cross_sheet_refs: list[str] = field(default_factory=list)
    same_sheet_cols: list[str] = field(default_factory=list)   # letters referenced on {r}
    lookup: tuple[str, str, str] | None = None                # (key letter, sheet, key column letter)
    n_formula_cells: int = 0


def analyse_column(formulas: list[tuple[int, str | None]], n_rows: int) -> ColumnFormulas:
    """`formulas` is [(sheet_row, formula or None)] for the column's data cells."""
    counts: Counter[str] = Counter()
    refs_for: dict[str, list[str]] = {}
    n = 0
    for row, f in formulas:
        if not (isinstance(f, str) and f.startswith("=")) or _CONSTANT.match(f):
            continue
        n += 1
        norm, refs = normalise(f, row)
        counts[norm] += 1
        refs_for.setdefault(norm, refs)
    res = ColumnFormulas(n_formula_cells=n)
    if not counts:
        return res
    norm, c = counts.most_common(1)[0]
    res.formula = norm
    res.coverage = c / max(n_rows, 1)
    for ref in refs_for[norm]:
        if "!" in ref:
            sheet, _, rng = ref.rpartition("!")
            res.cross_sheet_refs.append(f"{sheet.strip(chr(39))}!{rng}")
    res.same_sheet_cols = sorted({m.group(1).upper() for m in
                                  re.finditer(r"(?<![A-Za-z$])\$?([A-Z]{1,3})\{r\}", _strip_sheet_refs(norm))})
    m = _VLOOKUP.match(norm) or _XLOOKUP.match(norm)
    if m:
        res.lookup = (m.group(1).upper(), m.group(3), m.group(4).upper())
    return res


def _strip_sheet_refs(norm: str) -> str:
    """Remove sheet-qualified operands so only same-sheet references remain."""
    return re.sub(r"('[^']+'|[A-Za-z0-9_.]+)!\$?[A-Z]{1,3}[^,)*/+\-&^ ]*", "", norm)


def readable(norm: str, letter_to_name: dict[str, str]) -> str:
    """=F{r}*G{r} -> =[Units]*[Unit Price] (same-row, same-sheet references only)."""
    def sub(m: re.Match) -> str:
        name = letter_to_name.get(m.group(1).upper())
        return f"[{name}]" if name else m.group(0)
    parts = re.split(r"(('[^']+'|[A-Za-z0-9_.]+)!\$?[A-Z]{1,3}[^,)*/+\-&^ ]*)", norm)
    out = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        if i % 3 == 0:
            out.append(re.sub(r"(?<![A-Za-z$])\$?([A-Z]{1,3})\{r\}", sub, seg))
        elif i % 3 == 1:
            out.append(seg)
        i += 1
    return "".join(out)
