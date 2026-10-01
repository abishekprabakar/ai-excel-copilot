import io
import json
import zipfile

import boto3
import pytest
import requests
from fastapi.testclient import TestClient
from moto import mock_aws

from excel_copilot.api.app import Settings, create_app
from excel_copilot.llm.heuristic import HeuristicBackend
from excel_copilot.storage import LocalStorage, S3Storage


def sse(resp) -> list[tuple[str, dict]]:
    out, ev = [], None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            out.append((ev, json.loads(line[6:])))
    return out


@pytest.fixture
def client(tmp_path):
    st = LocalStorage(tmp_path, "http://testserver", b"s" * 32)
    return TestClient(create_app(Settings(), st, HeuristicBackend()))


@pytest.fixture
def uploaded(client, demo_bytes):
    r = client.post("/v1/workbooks", files={"file": ("sales_ops.xlsx", demo_bytes)})
    assert r.status_code == 201, r.text
    return r.json()


def test_upload_returns_profile_and_suggestions(uploaded):
    assert len(uploaded["workbook_id"]) == 32
    assert {t["id"] for t in uploaded["profile"]["tables"]} == {"orders", "products", "customers", "targets", "lookups"}
    assert "Total Revenue by Region" in uploaded["suggested_questions"]


def test_profile_graph_context(client, uploaded):
    wid = uploaded["workbook_id"]
    assert client.get(f"/v1/workbooks/{wid}/profile").json()["sha256"] == uploaded["profile"]["sha256"]
    g = client.get(f"/v1/workbooks/{wid}/graph").json()
    assert any(e["kind"] == "foreign_key" for e in g["edges"])
    ctx = client.get(f"/v1/workbooks/{wid}/context", params={"question": "segment", "budget": 300}).json()
    assert ctx["tokens"] <= 300 and "Segment" in ctx["text"]


def test_ask_streams_events_and_artifacts_download(client, uploaded):
    wid = uploaded["workbook_id"]
    with client.stream("POST", f"/v1/workbooks/{wid}/ask", json={"question": "total revenue by region"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        evs = sse(r)
    kinds = [k for k, _ in evs]
    assert kinds[0] == "stage" and kinds[-1] == "done" and "result" in kinds
    assert kinds.index("plan") < kinds.index("ir") < kinds.index("code") < kinds.index("result")
    done = evs[-1][1]
    assert done["ok"] and set(done["artifacts"]) == {"result.xlsx", "result.csv", "program.py", "run.json"}
    link = client.get(f"/v1/runs/{done['run_id']}/artifacts/result.csv").json()
    path = link["url"].replace("http://testserver", "")
    got = client.get(path)
    assert got.status_code == 200 and got.text.splitlines()[0] == "Region,Revenue"
    assert "attachment" in got.headers["content-disposition"]
    assert client.get(path.replace("result.csv", "run.json")).status_code == 403   # signature bound to key
    assert client.get(f"/v1/runs/{done['run_id']}/artifacts/../../etc").status_code == 404


def test_auth_required_when_keys_configured(tmp_path, demo_bytes):
    st = LocalStorage(tmp_path, "http://testserver", b"s" * 32)
    c = TestClient(create_app(Settings(api_keys=["k1"]), st, HeuristicBackend()))
    assert c.post("/v1/workbooks", files={"file": ("a.xlsx", demo_bytes)}).status_code == 401
    assert c.post("/v1/workbooks", files={"file": ("a.xlsx", demo_bytes)}, headers={"X-API-Key": "nope"}).status_code == 401
    assert c.post("/v1/workbooks", files={"file": ("a.xlsx", demo_bytes)}, headers={"X-API-Key": "k1"}).status_code == 201
    assert c.get("/healthz").status_code == 200


def test_rejections(client, tmp_path, demo_bytes, uploaded):
    assert client.post("/v1/workbooks", files={"file": ("a.csv", b"a,b")}).status_code == 415
    assert client.post("/v1/workbooks", files={"file": ("a.xlsx", b"not a zip")}).status_code == 422
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<broken")
    assert client.post("/v1/workbooks", files={"file": ("a.xlsx", buf.getvalue())}).status_code == 422
    small = TestClient(create_app(Settings(max_upload_bytes=1000), LocalStorage(tmp_path / "s", "http://t", b"k"),
                                  HeuristicBackend()))
    assert small.post("/v1/workbooks", files={"file": ("a.xlsx", demo_bytes)}).status_code == 413
    assert client.get("/v1/workbooks/nothex/profile").status_code == 404
    assert client.get("/v1/workbooks/" + "0" * 32 + "/profile").status_code == 404
    wid = uploaded["workbook_id"]
    assert client.post(f"/v1/workbooks/{wid}/ask", json={"question": "x" * 1001}).status_code == 422
    assert client.post(f"/v1/workbooks/{wid}/ask", json={"question": "  "}).status_code == 422


def test_cache_miss_reloads_from_storage(tmp_path, demo_bytes):
    st = LocalStorage(tmp_path, "http://testserver", b"s" * 32)
    a = TestClient(create_app(Settings(), st, HeuristicBackend()))
    wid = a.post("/v1/workbooks", files={"file": ("sales_ops.xlsx", demo_bytes)}).json()["workbook_id"]
    b = TestClient(create_app(Settings(), st, HeuristicBackend()))      # fresh process, empty cache
    assert b.get(f"/v1/workbooks/{wid}/profile").json()["file_name"] == "sales_ops.xlsx"


@mock_aws
def test_s3_storage_end_to_end(demo_bytes):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="art")
    st = S3Storage(bucket="art", prefix="dev", region="us-east-1")
    c = TestClient(create_app(Settings(), st, HeuristicBackend()))
    wid = c.post("/v1/workbooks", files={"file": ("sales_ops.xlsx", demo_bytes)}).json()["workbook_id"]
    keys = {o["Key"] for o in boto3.client("s3", region_name="us-east-1").list_objects_v2(Bucket="art")["Contents"]}
    assert {f"dev/workbooks/{wid}/source.xlsx", f"dev/workbooks/{wid}/profile.json"} <= keys
    head = boto3.client("s3", region_name="us-east-1").head_object(Bucket="art", Key=f"dev/workbooks/{wid}/source.xlsx")
    assert head["ServerSideEncryption"] == "AES256"
    with c.stream("POST", f"/v1/workbooks/{wid}/ask", json={"question": "average discount by segment"}) as r:
        done = sse(r)[-1][1]
    assert done["ok"]
    url = c.get(f"/v1/runs/{done['run_id']}/artifacts/result.xlsx").json()["url"]
    assert "X-Amz-Signature=" in url and "response-content-disposition=attachment" in url
    got = requests.get(url, timeout=10)               # served by moto's S3 mock via the presigned URL
    assert got.status_code == 200 and got.content[:2] == b"PK"
