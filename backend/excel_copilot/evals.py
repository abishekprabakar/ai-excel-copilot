"""Answer-accuracy evaluation.

Each case has a question and a hand-written *reference IR program*. The
reference is compiled and executed by the same trusted machinery, which gives
the expected table; the backend under test answers the question end to end.
An answer passes when every reference column is matched by some answer column
with the same values (numbers to 1e-6 relative), and row counts agree (and row
order too, for cases marked `ordered`). Extra answer columns are allowed, column
names are not compared, so a correct answer phrased differently still passes.

    excel-copilot eval evals/sales_ops.json --llm offline
    excel-copilot eval evals/sales_ops.json --llm claude   # needs ANTHROPIC_API_KEY
"""
from __future__ import annotations

import json
import math
import numbers
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .ir.compiler import compile_program
from .ir.models import Chart, Program
from .llm.base import Backend
from .pipeline import run_question
from .profiler import profile_workbook
from .sandbox.executor import execute


# labels people (and models) use for a missing group key; compared as missing
NULL_LABELS = {"", "unknown", "unmatched", "(missing)", "missing", "n/a", "na", "none", "null", "(blank)", "blank",
               "unassigned", "no match", "(none)"}


def _norm(v):
    if isinstance(v, str) and v.strip().lower() in NULL_LABELS:
        return None
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, numbers.Number) and not isinstance(v, (bool, np.bool_)):
        return float(v)            # covers numpy scalars too
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return str(v).strip().lower()


def _same(a: list, b: list) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, float) and isinstance(y, float):
            if not math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-9):
                return False
        elif x != y:
            return False
    return True


def _key(v):
    return (0, v) if isinstance(v, float) else (1, "" if v is None else str(v))


def matches(answer: pd.DataFrame, expected: pd.DataFrame, ordered: bool = False) -> tuple[bool, str]:
    if len(answer) != len(expected):
        return False, f"{len(answer)} rows, expected {len(expected)}"
    ans_cols = {c: [_norm(v) for v in answer[c].tolist()] for c in answer.columns}
    mapping: dict = {}
    for ec in expected.columns:
        exp = [_norm(v) for v in expected[ec].tolist()]
        hit = None
        for ac, vals in ans_cols.items():
            if ac in mapping.values():
                continue
            if ordered or len(expected) <= 1:
                ok = _same(vals, exp)
            else:
                ok = _same(sorted(vals, key=_key), sorted(exp, key=_key))
            if ok:
                hit = ac
                break
        if hit is None:
            return False, f"no answer column matches expected column {ec!r}"
        mapping[ec] = hit
    if not ordered and len(expected.columns) > 1 and len(expected) > 1:
        # values matched column-wise; also require the rows to line up as tuples
        cols = list(expected.columns)
        a_rows = sorted(zip(*[ans_cols[mapping[ec]] for ec in cols]), key=lambda t: [_key(x) for x in t])
        e_rows = sorted(zip(*[[_norm(v) for v in expected[ec].tolist()] for ec in cols]), key=lambda t: [_key(x) for x in t])
        if not all(_same(list(x), list(y)) for x, y in zip(a_rows, e_rows)):
            return False, "columns match individually but rows do not line up"
    return True, "ok"


@dataclass
class CaseResult:
    id: str
    question: str
    passed: bool
    detail: str
    seconds: float
    tokens_in: int
    tokens_out: int
    repairs: int
    trace: dict | None = None     # plan, program, answer and expected previews (for the report)


def _preview(df: pd.DataFrame | None, n: int = 15) -> dict | None:
    if df is None:
        return None
    return {"columns": [str(c) for c in df.columns],
            "rows": json.loads(df.head(n).to_json(orient="values", date_format="iso")), "n_rows": len(df)}


def run_eval(path: str | Path, backend: Backend) -> list[CaseResult]:
    spec = json.loads(Path(path).read_text())
    wb_path = (Path(path).parent / spec["workbook"]).resolve()
    res = profile_workbook(wb_path.read_bytes(), wb_path.name)
    out = []
    for case in spec["cases"]:
        # A case may list alternative references for questions with more than one
        # defensible reading; an answer passes if it matches any of them.
        refs = [("primary", case["reference"])] + [(a["label"], a["reference"]) for a in case.get("alternatives", [])]
        expected_frames = []
        for label, steps in refs:
            ref = Program(steps=steps, output=steps[-1]["out"], title="ref", chart=Chart(kind="none", x="", y=""))
            comp = compile_program(ref, res.profile)
            if not comp.ok:
                raise ValueError(f"reference {label!r} for {case['id']} is invalid: {[d.render() for d in comp.errors]}")
            exp = execute(comp.source, {t: res.frames[t] for t in comp.tables_used})
            if not exp.ok:
                raise ValueError(f"reference {label!r} for {case['id']} failed: {exp.error}")
            expected_frames.append((label, exp.frame))
        t0 = time.perf_counter()
        answer = None
        detail = "no result"
        tin = tout = repairs = 0
        plan = program = None
        try:
            for ev in run_question(case["question"], res.profile, res.frames, backend):
                if ev.type == "result":
                    answer = pd.DataFrame(ev.data["rows"], columns=[c["name"] for c in ev.data["columns"]])
                elif ev.type == "plan":
                    plan = ev.data["plan"]
                elif ev.type == "ir":
                    program = ev.data["program"]
                elif ev.type == "diagnostics" and not ev.data["ok"]:
                    repairs += 1
                elif ev.type == "error":
                    detail = f"{ev.data['stage']}: {ev.data['message']}"
                elif ev.type == "done":
                    tin = ev.data.get("tokens", {}).get("input", 0)
                    tout = ev.data.get("tokens", {}).get("output", 0)
                    if ev.data.get("answerable") is False:
                        detail = f"declined: {ev.data.get('reason')}"
        except Exception as e:  # one broken case must not abort the whole eval
            detail = f"crashed: {type(e).__name__}: {e}"
            answer = None
        passed, matched = False, None
        if answer is not None:
            for label, frame in expected_frames:
                ok, why = matches(answer, frame, bool(case.get("ordered")))
                if ok:
                    passed, matched, detail = True, label, "ok" if label == "primary" else f"ok ({label})"
                    break
                if label == "primary":
                    detail = why
        out.append(CaseResult(case["id"], case["question"], passed, detail, round(time.perf_counter() - t0, 2),
                              tin, tout, repairs,
                              {"plan": plan, "program": program, "answer": _preview(answer),
                               "matched": matched,
                               "expected": {label: _preview(f) for label, f in expected_frames}}))
    return out
