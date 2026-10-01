"""Column-level profiling: type inference, statistics, roles, PII and data-quality issues."""
from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import Counter
from typing import Any

from .formulas import ERROR_LITERALS
from .models import ColumnProfile, Issue, NumericStats

_NUMERIC_TEXT = re.compile(r"^\s*[-+]?(\$|€|£)?\s*\d{1,3}(,?\d{3})*(\.\d+)?\s*%?\s*$|^\s*[-+]?\d*\.\d+\s*$")
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_PHONE = re.compile(r"^\+?\d[\d\s().-]{7,}\d$")
_SSN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_ID_NAME = re.compile(r"(^|[\s_])(id|key|code|sku|number|no|#)$", re.I)
_CURRENCY_FMT = re.compile(r"[$€£¥]|\[\$")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def classify(value: Any, number_format: str | None) -> str:
    """Classify one non-null cell value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dt.datetime):
        return "date" if (value.hour, value.minute, value.second) == (0, 0, 0) else "datetime"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, (int, float)):
        fmt = number_format or ""
        if "%" in fmt:
            return "percent"
        if _CURRENCY_FMT.search(fmt):
            return "currency"
        if isinstance(value, int) or float(value).is_integer():
            return "integer"
        return "number"
    if isinstance(value, str):
        s = value.strip()
        if s in ERROR_LITERALS:
            return "error"
        if _NUMERIC_TEXT.match(s):
            return "numeric_text"
        return "string"
    return "string"


def to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "")
        pct = s.endswith("%")
        try:
            v = float(s.rstrip("%"))
        except ValueError:
            return None
        return v / 100 if pct else v
    return None


def _merge_type(counts: Counter[str]) -> tuple[str, int]:
    """Pick the column type and how many values conform to it."""
    numeric_kinds = ("integer", "number", "currency", "percent", "numeric_text")
    n_numeric = sum(counts[k] for k in numeric_kinds)
    total = sum(v for k, v in counts.items() if k != "error")
    if total == 0:
        return "empty", 0
    if n_numeric >= max(counts["string"], counts["date"] + counts["datetime"], counts["boolean"]):
        if counts["currency"]:
            kind = "currency"
        elif counts["percent"]:
            kind = "percent"
        elif counts["number"]:
            kind = "number"
        else:
            kind = "integer"
        return kind, n_numeric
    if counts["date"] + counts["datetime"] >= max(counts["string"], counts["boolean"]):
        return ("datetime" if counts["datetime"] else "date"), counts["date"] + counts["datetime"]
    if counts["boolean"] >= counts["string"]:
        return "boolean", counts["boolean"]
    return "string", counts["string"]


def _redact(value: str, kind: str | None) -> str:
    if kind == "email" and "@" in value:
        user, _, domain = value.partition("@")
        return f"{user[:1]}***@{domain}"
    if kind in ("phone", "ssn", "card"):
        return re.sub(r"\d", "•", value[:-2]) + value[-2:]
    return value


def _fmt(v: Any) -> str:
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, float):
        return f"{v:.6g}"
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "..."


def profile_column(*, name: str, header: str | None, letter: str, index: int, sheet: str,
                   rows: list[int], values: list[Any], formats: list[str | None]) -> ColumnProfile:
    """Profile one column. `rows` are 1-based sheet rows aligned with `values`."""
    counts: Counter[str] = Counter()
    kinds: list[str | None] = []
    issues: list[Issue] = []
    errors: list[str] = []
    for v, f, r in zip(values, formats, rows):
        if v is None or (isinstance(v, str) and not v.strip()):
            kinds.append(None)
            continue
        k = classify(v, f)
        kinds.append(k)
        counts[k] += 1
        if k == "error":
            errors.append(f"{sheet}!{letter}{r}")
    ctype, conforming = _merge_type(counts)
    non_null = sum(1 for k in kinds if k not in (None, "error"))
    nulls = len(values) - non_null
    numeric_kind = ctype in ("integer", "number", "currency", "percent")

    if errors:
        issues.append(Issue(code="formula_error", severity="error", count=len(errors),
                            location=errors[0],
                            message=f"{len(errors)} error value(s) such as "
                                    f"{values[kinds.index('error')]} (first at {errors[0]})"))
    if numeric_kind and counts["numeric_text"]:
        where = [f"{sheet}!{letter}{r}" for r, k in zip(rows, kinds) if k == "numeric_text"]
        issues.append(Issue(code="number_stored_as_text", severity="warning", count=len(where),
                            location=where[0],
                            message=f"{len(where)} number(s) stored as text (first at {where[0]})"))
    if ctype != "empty" and non_null and conforming / non_null < 0.98 and ctype != "string":
        issues.append(Issue(code="mixed_types", severity="warning", count=non_null - conforming,
                            message=f"{non_null - conforming} value(s) do not match type {ctype}: "
                                    + ", ".join(f"{k}={v}" for k, v in counts.items())))
    if len(values) and nulls / len(values) > 0.5 and ctype != "empty":
        issues.append(Issue(code="mostly_empty", severity="info", count=nulls,
                            message=f"{nulls}/{len(values)} cells are empty"))

    present = [v for v, k in zip(values, kinds) if k not in (None, "error")]
    keyed = [_fmt(v).strip() if isinstance(v, str) else _fmt(v) for v in present]
    distinct_counter = Counter(keyed)
    distinct = len(distinct_counter)
    unique = non_null > 0 and distinct == non_null

    numeric = None
    if numeric_kind:
        nums = [x for x in (to_number(v) for v in present) if x is not None]
        if nums:
            numeric = NumericStats(min=min(nums), max=max(nums), mean=statistics.fmean(nums),
                                   sum=float(sum(nums)), p50=float(statistics.median(nums)))
    date_min = date_max = None
    if ctype in ("date", "datetime"):
        ds = [v for v in present if isinstance(v, (dt.date, dt.datetime))]
        if ds:
            date_min, date_max = _fmt(min(ds)), _fmt(max(ds))

    pii = None
    if ctype == "string" and present:
        strs = [str(v).strip() for v in present]
        for kind, test in (("email", _EMAIL.match), ("ssn", _SSN.match), ("phone", _PHONE.match),
                           ("card", lambda s: (d := re.sub(r"[\s-]", "", s)).isdigit()
                            and 13 <= len(d) <= 19 and _luhn(d))):
            if sum(1 for s in strs if test(s)) >= 0.6 * len(strs):
                pii = kind
                issues.append(Issue(code="pii", severity="info", message=f"looks like {kind} addresses; "
                                    "sample values are redacted in LLM context"))
                break

    is_id_name = bool(_ID_NAME.search(name))
    if ctype == "empty":
        role = "empty"
    elif ctype in ("date", "datetime"):
        role = "temporal"
    elif ctype == "boolean":
        role = "flag"
    elif ctype in ("integer", "string") and is_id_name:
        role = "identifier"     # keys and foreign keys: never summed
    elif numeric_kind:
        role = "measure"
    elif distinct <= max(12, int(0.2 * non_null)):
        role = "dimension"
    else:
        role = "text"
    if role == "dimension" and pii:
        role = "text"

    top_values = []
    if role in ("dimension", "flag") and distinct_counter:
        top_values = [(_redact(k, pii), c) for k, c in distinct_counter.most_common(12)]
    seen, samples = set(), []
    for k in keyed:
        if k not in seen:
            seen.add(k)
            samples.append(_redact(k, pii))
        if len(samples) == 3:
            break

    fmt = Counter(f for f, k in zip(formats, kinds) if k is not None and f and f != "General")
    return ColumnProfile(
        name=name, header=header, letter=letter, index=index, type=ctype,  # type: ignore[arg-type]
        type_confidence=round(conforming / non_null, 4) if non_null else 0.0, role=role,  # type: ignore[arg-type]
        non_null=non_null, nulls=nulls, distinct=distinct, unique=unique, numeric=numeric,
        date_min=date_min, date_max=date_max, top_values=top_values, samples=samples,
        number_format=fmt.most_common(1)[0][0] if fmt else None, pii=pii, issues=issues)
