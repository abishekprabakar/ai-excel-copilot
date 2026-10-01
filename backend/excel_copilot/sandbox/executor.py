"""Run verified code in a separate, resource-limited Python process.

Layers of containment, outermost first:

1. Separate process started with `python -I` (isolated: no user site, no
   PYTHONPATH, no cwd on sys.path) and a scrubbed environment: no API keys, no
   AWS credentials reach it.
2. rlimits: address space, CPU seconds, no new processes, no file writes.
3. A PEP 578 audit hook installed after pandas is warmed up: blocks sockets,
   subprocesses, os.system/exec/fork, ctypes, file writes and further
   compile/exec.
4. The program is exec'd with a near-empty `__builtins__`, and only `tables`,
   `xl` and `pd` in scope (and it has already passed the AST verifier).
5. Results come back as JSON on stdout (never pickle), size-capped.

This is not a VM boundary. On Lambda, the function itself is the outer
boundary (Firecracker microVM, no inbound network, IAM-scoped role). For
untrusted multi-tenant use, put this behind gVisor/Firecracker as well.
"""
from __future__ import annotations

import io
import json
import os
import pickle
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .verify import verify

RUNTIME_SRC = (Path(__file__).with_name("runtime.py")).read_text()

# The child bootstrap. Read as a string; runs with `python -I -c`.
_CHILD = r'''
import sys, os, io, json, pickle, resource, time
payload = pickle.loads(sys.stdin.buffer.read())     # from the trusted parent only
lim = payload["limits"]
import numpy as np
import pandas as pd

xl = type(sys)("xl")
exec(compile(payload["runtime"], "xl_runtime", "exec"), xl.__dict__)

# warm up lazily-imported pandas paths before locking down
_w = pd.DataFrame({"a": [1, 2], "b": ["x", "y"], "d": pd.to_datetime(["2024-01-01", "2024-02-01"])})
xl.aggregate(_w, ["b"], [("a", "sum", "s"), ("*", "count", "n"), ("a", "median", "m"), ("a", "std", "sd"), ("a", "nunique", "u")])
xl.join(_w, _w, ["b"], ["b"], "left"); xl.pivot(_w, ["b"], "b", "a", "sum")
_w.melt(id_vars=["b"], value_vars=["a"]); _w["d"].dt.year; _w["b"].str.contains("x", case=False, regex=False)
_w.sort_values(by=["a"], ascending=[False], na_position="last", kind="stable").to_json(orient="table", date_format="iso")
xl.if_else(_w, _w["a"] > 1, _w["a"], 0); xl.div(_w["a"], _w["a"] - 1)

def _limit(name, soft, hard):
    # Some limits are unsupported on some platforms (macOS rejects RLIMIT_AS and
    # can refuse RLIMIT_NPROC=0). Apply what the OS allows; never die here.
    try:
        resource.setrlimit(getattr(resource, name), (soft, hard))
        return True
    except (ValueError, OSError, AttributeError):
        return False
_applied = {
    "memory": _limit("RLIMIT_AS", lim["memory_bytes"], lim["memory_bytes"]),
    "cpu": _limit("RLIMIT_CPU", lim["cpu_seconds"], lim["cpu_seconds"] + 1),
    "fsize": _limit("RLIMIT_FSIZE", 0, 0),
    "nproc": _limit("RLIMIT_NPROC", 0, 0),
}
if not _applied["memory"]:
    # fall back to the data-segment limit, which macOS does honour for malloc'd memory
    _applied["memory"] = _limit("RLIMIT_DATA", lim["memory_bytes"], lim["memory_bytes"])

# reads are allowed for interpreter/library files only (stdlib, site-packages, .dylib/.so)
_CODE_EXT = (".py", ".pyc", ".so", ".dylib", ".pyd", ".json")
_ROOTS = tuple(sorted({os.path.realpath(p) + os.sep for p in
                       (sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(os.__file__),
                        os.path.dirname(os.path.dirname(np.__file__)),
                        os.path.dirname(os.path.dirname(pd.__file__)))}))
_state = {"exec_allowed": 1}
_BLOCK_PREFIX = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                 "os.fork", "os.forkpty", "os.kill", "os.remove", "os.rename", "os.rmdir", "os.mkdir",
                 "os.chmod", "os.chown", "os.truncate", "os.symlink", "os.link", "os.putenv", "os.unsetenv",
                 "shutil.", "ctypes.", "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.",
                 "sys._getframe", "sys.settrace", "sys.setprofile", "code.", "pickle.find_class",
                 "marshal.loads", "import")
def _hook(event, args):
    if event in ("exec", "compile"):
        if _state["exec_allowed"] > 0:
            if event == "exec":
                _state["exec_allowed"] -= 1
            return
        raise RuntimeError(f"sandbox: {event} is blocked")
    if event == "open":
        path, mode = args[0], args[1] if len(args) > 1 else "r"
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            raise RuntimeError("sandbox: file writes are blocked")
        if isinstance(path, (str, bytes)):
            p = os.fsdecode(path)
            if not p.endswith(_CODE_EXT) and not os.path.realpath(p).startswith(_ROOTS):
                raise RuntimeError(f"sandbox: opening {p!r} is blocked")
        return
    if event.startswith(_BLOCK_PREFIX):
        raise RuntimeError(f"sandbox: {event} is blocked")
sys.addaudithook(_hook)

SAFE_BUILTINS = {"len": len, "range": range, "min": min, "max": max, "abs": abs, "round": round,
                 "sum": sum, "list": list, "dict": dict, "str": str, "int": int, "float": float,
                 "bool": bool, "True": True, "False": False, "None": None}
env = {"__builtins__": SAFE_BUILTINS, "pd": pd, "xl": xl}
t0 = time.perf_counter()
out = {"ok": False}
try:
    exec(compile(payload["code"], "<program>", "exec"), env)
    res = env["run"](payload["tables"])
    if not isinstance(res, pd.DataFrame):
        raise TypeError(f"program returned {type(res).__name__}, expected a DataFrame")
    n = len(res)
    out = {"ok": True, "rows": n, "truncated": n > lim["max_rows"],
           "result": json.loads(res.head(lim["max_rows"]).to_json(orient="table", date_format="iso", index=False))}
except BaseException as e:  # report everything, including MemoryError and RecursionError
    out = {"ok": False, "error": f"{type(e).__name__}: {e}"[:2000]}
out["exec_ms"] = round((time.perf_counter() - t0) * 1000, 1)
sys.stdout.write(json.dumps(out, default=str))
'''


@dataclass
class Limits:
    timeout_s: float = 20.0
    cpu_seconds: int = 15
    memory_bytes: int = 2 * 1024**3
    max_rows: int = 50_000
    max_output_bytes: int = 64 * 1024**2


@dataclass
class ExecResult:
    ok: bool
    frame: pd.DataFrame | None
    rows: int
    truncated: bool
    error: str | None
    exec_ms: float
    wall_ms: float
    stderr: str


def _child_env() -> dict[str, str]:
    keep = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
            "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
            "TZ": "UTC"}
    return keep          # deliberately no HOME, no AWS_*, no ANTHROPIC_*


def execute(source: str, tables: dict[str, pd.DataFrame], limits: Limits | None = None) -> ExecResult:
    limits = limits or Limits()
    verify(source)                       # never run anything that fails verification
    payload = pickle.dumps({"code": source, "runtime": RUNTIME_SRC, "tables": tables,
                            "limits": {"memory_bytes": limits.memory_bytes, "cpu_seconds": limits.cpu_seconds,
                                       "max_rows": limits.max_rows}})
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", _CHILD], input=payload, capture_output=True,
                              timeout=limits.timeout_s, env=_child_env(), cwd="/")
    except subprocess.TimeoutExpired:
        return ExecResult(False, None, 0, False, f"timed out after {limits.timeout_s:.0f}s", 0.0,
                          (time.perf_counter() - t0) * 1000, "")
    wall = (time.perf_counter() - t0) * 1000
    stderr = proc.stderr.decode(errors="replace")[-4000:]
    if len(proc.stdout) > limits.max_output_bytes:
        return ExecResult(False, None, 0, False, "result too large", 0.0, wall, stderr)
    try:
        out = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError:
        out = {}
    if not out:
        sig = -proc.returncode if proc.returncode < 0 else None
        why = {9: "killed (CPU or memory limit)", 24: "CPU time limit exceeded"}.get(sig or 0, f"exit code {proc.returncode}")
        last = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        if last:
            why += f" ({last[-1][:300]})"
        return ExecResult(False, None, 0, False, f"sandbox process failed: {why}", 0.0, wall, stderr)
    if not out.get("ok"):
        return ExecResult(False, None, 0, False, out.get("error", "unknown error"), out.get("exec_ms", 0.0), wall, stderr)
    frame = pd.read_json(io.StringIO(json.dumps(out["result"])), orient="table")
    return ExecResult(True, frame, out["rows"], out["truncated"], None, out["exec_ms"], wall, stderr)
