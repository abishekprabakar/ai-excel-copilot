"""Claude backend: planner and coder calls with JSON-schema constrained outputs.

Uses `client.messages.parse(output_format=<pydantic model>)`, which sends the
model's JSON schema as `output_config.format` so decoding is constrained to the
schema, then validates the reply back into the pydantic type. The workbook
context goes in a cached system block, so asking several questions about the
same workbook re-reads it from the prompt cache.

The coder emits the flat wire format (ir/wire.py), not the typed IR union: the
union's compiled grammar is larger than the API accepts. If the API still
rejects a schema as too large (limits can differ by model), the backend falls
back to unconstrained JSON for that schema, validated the same way, and
remembers the fallback for the rest of the process.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..ir.models import Plan, Program
from ..ir.wire import WireError, WireProgram, to_program
from . import prompts
from .base import Backend, LLMError, Turn, Usage

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-sonnet-5"
WIRE_RETRIES = 2


def _grammar_too_large(e: Exception) -> bool:
    msg = str(getattr(e, "message", "") or e).lower()
    return "grammar" in msg and ("too large" in msg or "too complex" in msg)


def _extract_json(text: str) -> str:
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the response")
    return text[start:end + 1]


@dataclass
class ClaudeBackend(Backend):
    model: str = field(default_factory=lambda: os.environ.get("EXCEL_COPILOT_MODEL", DEFAULT_MODEL))
    coder_model: str | None = None
    max_tokens: int = 16000
    client: Any = None
    name: str = "claude"
    unconstrained: set[str] = field(default_factory=set)   # schema names that fell back

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic(max_retries=3)
        self.coder_model = self.coder_model or os.environ.get("EXCEL_COPILOT_CODER_MODEL") or self.model

    def _system(self, system: str, context: str) -> list[dict]:
        return [{"type": "text", "text": system},
                {"type": "text", "text": f"<workbook>\n{context}\n</workbook>", "cache_control": {"type": "ephemeral"}}]

    @staticmethod
    def _usage(model: str, u: Any, t0: float) -> Usage:
        return Usage(model=model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                     cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                     cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                     latency_ms=round((time.perf_counter() - t0) * 1000, 1))

    @staticmethod
    def _check_stop(resp: Any) -> None:
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("the model ran out of output tokens before finishing the JSON")

    def _call(self, *, model: str, system: str, context: str, turns: list[Turn], schema: type[T]) -> tuple[T, Usage]:
        import anthropic
        if schema.__name__ in self.unconstrained:
            return self._call_json(model=model, system=system, context=context, turns=turns, schema=schema)
        t0 = time.perf_counter()
        try:
            resp = self.client.messages.parse(
                model=model, max_tokens=self.max_tokens, system=self._system(system, context),
                messages=[{"role": t.role, "content": t.content} for t in turns], output_format=schema)
        except anthropic.APIStatusError as e:
            if e.status_code == 400 and _grammar_too_large(e):
                self.unconstrained.add(schema.__name__)
                return self._call_json(model=model, system=system, context=context, turns=turns, schema=schema)
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"could not reach the Claude API: {e}") from e
        except Exception as e:  # validation of the parsed output, etc.
            raise LLMError(f"Claude response could not be parsed as {schema.__name__}: {e}") from e
        self._check_stop(resp)
        if resp.parsed_output is None:
            raise LLMError("the model returned no structured output")
        return resp.parsed_output, self._usage(model, resp.usage, t0)

    def _call_json(self, *, model: str, system: str, context: str, turns: list[Turn],
                   schema: type[T]) -> tuple[T, Usage]:
        """Unconstrained fallback: ask for JSON matching the schema, validate, retry once on bad JSON."""
        import anthropic
        instr = (f"{system}\n\nRespond with a single JSON object and nothing else. It must validate "
                 f"against this JSON schema:\n{json.dumps(schema.model_json_schema(), separators=(',', ':'))}")
        msgs = [{"role": t.role, "content": t.content} for t in turns]
        total = Usage(model=model)
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                resp = self.client.messages.create(model=model, max_tokens=self.max_tokens,
                                                   system=self._system(instr, context), messages=msgs)
            except anthropic.APIStatusError as e:
                raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"could not reach the Claude API: {e}") from e
            self._check_stop(resp)
            u = self._usage(model, resp.usage, t0)
            total.input_tokens += u.input_tokens
            total.output_tokens += u.output_tokens
            total.cache_read_tokens += u.cache_read_tokens
            total.cache_write_tokens += u.cache_write_tokens
            total.latency_ms += u.latency_ms
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            try:
                return schema.model_validate_json(_extract_json(text)), total
            except (ValueError, ValidationError) as e:
                if attempt == 1:
                    raise LLMError(f"Claude response could not be parsed as {schema.__name__}: {e}") from e
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That was not valid JSON for the schema: {str(e)[:800]}. "
                                                     "Return only the corrected JSON object."}]
        raise LLMError("unreachable")

    def plan(self, question: str, context: str, profile) -> tuple[Plan, Usage]:
        return self._call(model=self.model, system=prompts.PLANNER_SYSTEM, context=context,
                          turns=[Turn("user", f"<question>{question}</question>")], schema=Plan)

    def code(self, question: str, plan: Plan, context: str, profile,
             history: list[Turn]) -> tuple[Program, Usage]:
        first = Turn("user", f"<question>{question}</question>\n\n<plan>\n{plan.model_dump_json(indent=1)}\n</plan>\n\n"
                             "Write the IR program.")
        turns = [first, *history]
        total = Usage(model=self.coder_model or self.model)
        for attempt in range(WIRE_RETRIES + 1):
            wire, u = self._call(model=self.coder_model or self.model, system=prompts.CODER_SYSTEM,
                                 context=context, turns=turns, schema=WireProgram)
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "latency_ms"):
                setattr(total, k, getattr(total, k) + getattr(u, k))
            try:
                return to_program(wire), total
            except WireError as e:
                if attempt == WIRE_RETRIES:
                    raise LLMError(f"coder output was not a valid program: {e}") from e
                turns = turns + [Turn("assistant", wire.model_dump_json()),
                                 Turn("user", prompts.repair_user(e.problems))]
        raise LLMError("unreachable")
