"""The Claude backend through the real Anthropic SDK, with the HTTP layer mocked.

This checks the exact request we send (model, cached system context, JSON-schema
output_config, repair turns) and how responses and failures are handled. It does
not call the live API.
"""
import json

import anthropic
import httpx2
import pytest

from excel_copilot.llm.base import LLMError, Turn
from excel_copilot.llm.claude import ClaudeBackend
from excel_copilot.pipeline import run_question

from .conftest import plan as mkplan

PLAN = {"answerable": True, "reason": "", "tables": ["orders"], "columns": ["orders.Revenue", "orders.Region"],
        "steps": ["load orders", "sum Revenue by Region"], "assumptions": ["all statuses count"]}
PROGRAM = {"steps": [{"op": "load", "out": "o", "table": "orders"},
                     {"op": "aggregate", "out": "g", "input": "o", "group_by": ["Region"],
                      "aggregations": [{"column": "Revenue", "func": "sum", "as_name": "Revenue"}]}],
           "output": "g", "title": "Revenue by region", "chart": {"kind": "bar", "x": "Region", "y": "Revenue"}}


def reply(payload, stop="end_turn", status=200):
    def make(body):
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        return httpx2.Response(200, json={
            "id": "msg_x", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(payload)}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 120, "cache_read_input_tokens": 700,
                      "cache_creation_input_tokens": 0}})
    return make


def backend(*responders):
    seen = []
    queue = list(responders)

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return queue.pop(0)(body)
    client = anthropic.Anthropic(api_key="test-key", max_retries=0,
                                 http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    return ClaudeBackend(model="claude-sonnet-5", client=client), seen


def test_request_shape_and_parsing():
    be, seen = backend(reply(PLAN))
    plan, usage = be.plan("revenue by region", "TABLE orders ...", None)
    assert plan.steps == PLAN["steps"] and usage.cache_read_tokens == 700
    body = seen[0]
    assert body["model"] == "claude-sonnet-5"
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert "TABLE orders" in body["system"][1]["text"]
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema" and set(fmt["schema"]["required"]) >= {"answerable", "tables", "steps"}
    assert "discriminator" not in json.dumps(fmt["schema"])        # unsupported by constrained decoding


def test_program_schema_and_repair_turns():
    be, seen = backend(reply(PROGRAM))
    hist = [Turn("assistant", '{"bad": 1}'), Turn("user", "fix step 1")]
    prog, _ = be.code("q", mkplan(), "ctx", None, hist)
    assert prog.output == "g"
    msgs = seen[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"] and msgs[2]["content"] == "fix step 1"
    schema = json.dumps(seen[0]["output_config"]["format"]["schema"])
    for op in ("load", "filter", "derive", "select", "aggregate", "join", "sort", "limit", "unpivot", "pivot"):
        assert f'"{op}"' in schema


@pytest.mark.parametrize("responder,needle", [
    (reply(PLAN, stop="refusal"), "declined"),
    (reply(PLAN, stop="max_tokens"), "ran out of output tokens"),
    (reply(PLAN, status=529), "529"),
    (reply({"answerable": "maybe"}), "could not be parsed"),
])
def test_failures_raise_llm_error(responder, needle):
    be, _ = backend(responder)
    with pytest.raises(LLMError, match=needle):
        be.plan("q", "ctx", None)


def test_full_pipeline_with_mocked_claude(demo):
    be, seen = backend(reply(PLAN), reply(PROGRAM))
    evs = list(run_question("revenue by region", demo.profile, demo.frames, be))
    assert evs[-1].data["ok"] and evs[-1].data["tokens"] == {"input": 1800, "output": 240}
    # the coder sees a restricted, detailed view of only the planned tables
    coder_ctx = seen[1]["system"][1]["text"]
    assert "TABLE orders" in coder_ctx and "TABLE customers" not in coder_ctx
