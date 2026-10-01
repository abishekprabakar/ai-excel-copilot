"""FastAPI layer.

    POST /v1/workbooks                       upload + profile an .xlsx/.xlsm
    GET  /v1/workbooks/{id}/profile          full profile JSON
    GET  /v1/workbooks/{id}/graph            context graph (nodes/edges)
    GET  /v1/workbooks/{id}/context          compact LLM context (?question=&budget=)
    POST /v1/workbooks/{id}/ask              run the pipeline, streamed as server-sent events
    GET  /v1/runs/{run_id}/artifacts/{name}  short-lived download URL (S3 presigned or signed local)
    GET  /v1/files/{key}                     signed local downloads (local storage only)
    GET  /healthz

The same app runs under uvicorn locally and on AWS Lambda behind the Lambda
Web Adapter in response-streaming mode, so the SSE stream reaches the browser
incrementally in both places.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Iterator

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ..graph import build_graph, render_context
from ..llm.base import Backend, get_backend
from ..pipeline import Event, Options, run_question
from ..profiler import ProfileResult, UnsafeWorkbook, WorkbookProfile, profile_workbook
from ..storage import LocalStorage, Storage, StorageError, storage_from_env

log = logging.getLogger("excel_copilot")
ID_RE = re.compile(r"^[a-f0-9]{32}$")
ARTIFACTS = {"result.xlsx", "result.csv", "program.py", "run.json"}


class Settings(BaseModel):
    api_keys: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    max_upload_bytes: int = 25 * 1024 * 1024
    max_question_chars: int = 1000
    url_ttl_s: int = 900
    cache_size: int = 16

    @classmethod
    def from_env(cls) -> "Settings":
        def lst(name: str, default: str = "") -> list[str]:
            return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]
        return cls(api_keys=lst("API_KEYS"), cors_origins=lst("CORS_ORIGINS", "http://localhost:5173"),
                   max_upload_bytes=int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024)


class AskRequest(BaseModel):
    question: str
    context_budget: int = Field(default=1500, ge=200, le=8000)


class _Cache:
    """Tiny LRU of profiled workbooks so repeated questions skip re-parsing."""

    def __init__(self, size: int):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, k: str) -> ProfileResult | None:
        with self.lock:
            if k in self.data:
                self.data.move_to_end(k)
                return self.data[k]
            return None

    def put(self, k: str, v: ProfileResult) -> None:
        with self.lock:
            self.data[k] = v
            self.data.move_to_end(k)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


def create_app(settings: Settings | None = None, storage: Storage | None = None,
               backend: Backend | None = None, options: Options | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    storage = storage or storage_from_env()
    backend = backend or get_backend()
    options = options or Options()
    cache = _Cache(settings.cache_size)

    app = FastAPI(title="Excel Copilot", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "X-API-Key"], max_age=600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
        resp.headers["X-Request-Id"] = rid
        log.info(json.dumps({"rid": rid, "method": request.method, "path": request.url.path,
                             "status": resp.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1)}))
        return resp

    def auth(x_api_key: str | None = Header(default=None)) -> None:
        if not settings.api_keys:
            return
        if not x_api_key or not any(hmac.compare_digest(x_api_key, k) for k in settings.api_keys):
            raise HTTPException(401, "missing or invalid X-API-Key")

    def load(workbook_id: str) -> ProfileResult:
        if not ID_RE.match(workbook_id):
            raise HTTPException(404, "workbook not found")
        hit = cache.get(workbook_id)
        if hit:
            return hit
        try:
            meta = json.loads(storage.get(f"workbooks/{workbook_id}/meta.json"))
            data = storage.get(f"workbooks/{workbook_id}/source.xlsx")
        except StorageError:
            raise HTTPException(404, "workbook not found") from None
        res = profile_workbook(data, meta["file_name"])     # deterministic: same profile as at upload
        cache.put(workbook_id, res)
        return res

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "llm": backend.name, "storage": storage.kind}

    @app.post("/v1/workbooks", status_code=201, dependencies=[Depends(auth)])
    async def upload(file: UploadFile = File(...)):
        name = os.path.basename(file.filename or "workbook.xlsx")
        if not name.lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(415, "upload an .xlsx or .xlsm file")
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f"file larger than {settings.max_upload_bytes // (1024 * 1024)} MB")
        from starlette.concurrency import run_in_threadpool
        try:
            res = await run_in_threadpool(profile_workbook, data, name)
        except UnsafeWorkbook as e:
            raise HTTPException(422, str(e)) from None
        except Exception as e:  # openpyxl raises many types on malformed files
            log.exception("profiling failed")
            raise HTTPException(422, f"could not read workbook: {type(e).__name__}") from None
        wid = uuid.uuid4().hex
        storage.put(f"workbooks/{wid}/source.xlsx", data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        storage.put(f"workbooks/{wid}/meta.json", json.dumps({"file_name": name}).encode(), "application/json")
        storage.put(f"workbooks/{wid}/profile.json", res.profile.model_dump_json().encode(), "application/json")
        cache.put(wid, res)
        return {"workbook_id": wid, "profile": json.loads(res.profile.model_dump_json()),
                "suggested_questions": suggest_questions(res.profile)}

    @app.get("/v1/workbooks/{workbook_id}/profile", dependencies=[Depends(auth)])
    def get_profile(workbook_id: str):
        return json.loads(load(workbook_id).profile.model_dump_json())

    @app.get("/v1/workbooks/{workbook_id}/graph", dependencies=[Depends(auth)])
    def get_graph(workbook_id: str):
        return build_graph(load(workbook_id).profile).model_dump()

    @app.get("/v1/workbooks/{workbook_id}/context", dependencies=[Depends(auth)])
    def get_context(workbook_id: str, question: str = Query(default="", max_length=1000),
                    budget: int = Query(default=1500, ge=100, le=8000)):
        ctx = render_context(load(workbook_id).profile, question or None, budget)
        return {"text": ctx.text, "tokens": ctx.tokens, "levels": ctx.levels}

    @app.post("/v1/workbooks/{workbook_id}/ask", dependencies=[Depends(auth)])
    def ask(workbook_id: str, req: AskRequest):
        q = req.question.strip()
        if not q or len(q) > settings.max_question_chars:
            raise HTTPException(422, f"question must be 1-{settings.max_question_chars} characters")
        res = load(workbook_id)
        opts = Options(context_budget=req.context_budget, coder_budget=options.coder_budget,
                       max_repairs=options.max_repairs, preview_rows=options.preview_rows, limits=options.limits)

        def stream() -> Iterator[str]:
            yield ": stream open\n\n"           # flush headers through proxies immediately
            try:
                for ev in run_question(q, res.profile, res.frames, backend, storage,
                                       workbook_id=workbook_id, options=opts):
                    yield ev.sse()
            except Exception as e:             # never leave the client hanging
                log.exception("pipeline crashed")
                yield Event("error", {"stage": "internal", "message": f"{type(e).__name__}: {e}"}).sse()
                yield Event("done", {"ok": False}).sse()

        # A sync generator: Starlette iterates it in a worker thread, so blocking LLM
        # calls and the sandbox subprocess never stall the event loop.
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/v1/runs/{run_id}/artifacts/{name}", dependencies=[Depends(auth)])
    def artifact_url(run_id: str, name: str):
        if not ID_RE.match(run_id) or name not in ARTIFACTS:
            raise HTTPException(404, "artifact not found")
        key = f"runs/{run_id}/{name}"
        if not storage.exists(key):
            raise HTTPException(404, "artifact not found")
        return {"url": storage.url(key, f"{run_id[:8]}-{name}", settings.url_ttl_s), "expires_in": settings.url_ttl_s}

    @app.get("/v1/files/{key:path}")
    def local_file(key: str, exp: int, name: str, sig: str):
        if not isinstance(storage, LocalStorage) or not storage.verify(key, exp, name, sig):
            raise HTTPException(403, "link expired or invalid")
        try:
            data = storage.get(key)
        except StorageError:
            raise HTTPException(404, "not found") from None
        safe = re.sub(r'[^A-Za-z0-9_.\-]', "_", name)
        ctype = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv",
                 "json": "application/json", "py": "text/plain"}.get(safe.rsplit(".", 1)[-1], "application/octet-stream")
        return Response(data, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{safe}"'})

    @app.exception_handler(StorageError)
    async def _storage_error(_: Request, exc: StorageError):
        return JSONResponse({"detail": "not found"}, status_code=404)

    return app


def suggest_questions(p: WorkbookProfile, n: int = 4) -> list[str]:
    """Starter questions built from the profile's measures, dimensions and dates."""
    out: list[str] = []
    for t in sorted(p.tables, key=lambda t: -t.n_rows):
        if t.layout == "wide":
            continue
        measures = [c for c in t.columns if c.role == "measure"]
        measures.sort(key=lambda c: (c.type == "currency", bool(c.formula)), reverse=True)
        dims = [c for c in t.columns if c.role == "dimension"]
        time_col = next((c for c in t.columns if c.role == "temporal"), None)
        if measures and dims:
            out.append(f"Total {measures[0].name} by {dims[0].name}")
        if measures and time_col:
            out.append(f"Monthly {measures[0].name}")
        for r in p.relationships:
            if r.from_table == t.id and measures:
                other = p.table(r.to_table)
                od = next((c for c in other.columns if c.role == "dimension"), None)
                if od:
                    out.append(f"Top 5 {od.name} by {measures[0].name}")
                    break
        if len(out) >= n:
            break
    return list(dict.fromkeys(out))[:n]


