"""Command line: profile, context, ask, serve.

    excel-copilot profile examples/sales_ops.xlsx            # summary (or --json for the full profile)
    excel-copilot context examples/sales_ops.xlsx -q "revenue by segment" --budget 400
    excel-copilot ask examples/sales_ops.xlsx "Top 5 SKUs by revenue in Q3" [--llm claude|offline] [--out runs/]
    excel-copilot eval evals/sales_ops.json --llm offline
    excel-copilot serve --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: str):
    from .profiler import profile_workbook
    p = Path(path)
    return profile_workbook(p.read_bytes(), p.name)


def cmd_profile(a) -> int:
    res = _load(a.file)
    p = res.profile
    if a.json:
        print(p.model_dump_json(indent=2))
        return 0
    print(f"{p.file_name}: {len(p.sheets)} sheets, {len(p.tables)} tables, profiled in {p.profiled_ms:.0f} ms")
    for t in p.tables:
        print(f"\n  {t.id:<12} {t.sheet}!{t.range:<10} {t.n_rows:>5} rows  {t.source}{'  WIDE' if t.layout == 'wide' else ''}")
        for c in t.columns:
            flags = " ".join(f"!{i.code}" for i in c.issues)
            extra = c.formula_readable or (f"[{c.pii}]" if c.pii else "")
            print(f"    {c.name:<16} {c.type:<9} {c.role:<10} {extra} {flags}".rstrip())
    if p.relationships:
        print("\n  relationships")
        for r in p.relationships:
            print(f"    {r.from_table}.{r.from_column} -> {r.to_table}.{r.to_column}  "
                  f"{r.kind} containment={r.containment:.1%}")
    if p.issues:
        print("\n  workbook issues: " + "; ".join(i.message for i in p.issues))
    return 0


def cmd_context(a) -> int:
    from .graph import render_context
    ctx = render_context(_load(a.file).profile, a.question, a.budget)
    print(ctx.text)
    print(f"\n[{ctx.tokens} tokens est., budget {a.budget}; detail levels {ctx.levels}]", file=sys.stderr)
    return 0


def cmd_ask(a) -> int:
    from .llm.base import get_backend
    from .pipeline import run_question
    from .storage import LocalStorage
    res = _load(a.file)
    backend = get_backend(a.llm)
    storage = LocalStorage(Path(a.out), "file://" + str(Path(a.out).resolve()), b"cli") if a.out else None
    ok = False
    for ev in run_question(a.question, res.profile, res.frames, backend, storage):
        d = ev.data
        if a.events:
            print(json.dumps({"type": ev.type, **d}, default=str))
            ok = ok or (ev.type == "done" and d.get("ok"))
            continue
        if ev.type == "context":
            print(f"· context: {d['tokens']} tokens")
        elif ev.type == "plan":
            print("· plan:")
            for s in d["plan"]["steps"]:
                print(f"    - {s}")
            for s in d["plan"]["assumptions"]:
                print(f"    assumption: {s}")
        elif ev.type == "diagnostics":
            for item in d["items"]:
                print(f"· {item['severity']}: step {item['step']} {item['field']}: {item['message']}")
        elif ev.type == "code":
            print("· compiled program:\n" + "\n".join("    " + ln for ln in d["source"].splitlines()))
        elif ev.type == "log":
            print(f"· {d['message']}")
        elif ev.type == "result":
            import pandas as pd
            df = pd.DataFrame(d["rows"], columns=[c["name"] for c in d["columns"]])
            print(f"\n{d['title']}  ({d['row_count']} rows)")
            with pd.option_context("display.width", 120, "display.max_columns", 20, "display.float_format",
                                   lambda v: f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"):
                print(df.to_string(index=False, na_rep="(missing)"))
        elif ev.type == "artifact":
            print(f"· artifact {d['name']} ({d['bytes']:,} bytes) -> {Path(a.out) / d['key']}")
        elif ev.type == "error":
            print(f"✗ {d['stage']}: {d['message']}")
        elif ev.type == "done":
            ok = bool(d.get("ok"))
            if not ok and d.get("reason"):
                print(f"✗ not answerable: {d['reason']}")
    return 0 if ok else 1


def cmd_eval(a) -> int:
    from .evals import run_eval
    from .llm.base import get_backend
    results = run_eval(a.spec, get_backend(a.llm))
    width = max(len(r.id) for r in results)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        tok = f"{r.tokens_in:>6} in {r.tokens_out:>5} out" if r.tokens_in else ""
        print(f"{mark}  {r.id:<{width}}  {r.seconds:>5.2f}s  {tok}  {r.detail if (not r.passed or r.detail != 'ok') else ''}")
    n = sum(r.passed for r in results)
    print(f"\n{n}/{len(results)} passed ({n / len(results):.0%})")
    if a.report:
        import dataclasses
        Path(a.report).write_text(json.dumps([dataclasses.asdict(r) for r in results], indent=1, default=str))
        print(f"details written to {a.report}")
    return 0


def cmd_serve(a) -> int:
    import uvicorn
    uvicorn.run("excel_copilot.api.main:app", host=a.host, port=a.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="excel-copilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("profile"); p.add_argument("file"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_profile)
    p = sub.add_parser("context"); p.add_argument("file"); p.add_argument("-q", "--question")
    p.add_argument("--budget", type=int, default=1500); p.set_defaults(fn=cmd_context)
    p = sub.add_parser("ask"); p.add_argument("file"); p.add_argument("question")
    p.add_argument("--llm", choices=["auto", "claude", "offline"], default="auto")
    p.add_argument("--out", help="directory for artifacts"); p.add_argument("--events", action="store_true",
                                                                           help="print raw events as JSON lines")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("eval"); p.add_argument("spec")
    p.add_argument("--report", default="eval_report.json", help="where to write per-case details ('' to skip)")
    p.add_argument("--llm", choices=["auto", "claude", "offline"], default="auto"); p.set_defaults(fn=cmd_eval)
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
