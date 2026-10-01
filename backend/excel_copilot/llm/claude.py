"""Claude backend: planner and coder calls with JSON-schema constrained outputs.

Uses `client.messages.parse(output_format=<pydantic model>)`, which sends the
model's JSON schema as `output_config.format` so decoding is constrained to the
schema, then validates the reply back into the pydantic type. The workbook
context goes in a cached system block, so asking several questions about the
same workbook re-reads it from the prompt cache.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from ..ir.models import Plan, Program
from . import prompts
from .base import Backend, LLMError, Turn, Usage

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-sonnet-5"


@dataclass
class ClaudeBackend(Backend):
    model: str = field(default_factory=lambda: os.environ.get("EXCEL_COPILOT_MODEL", DEFAULT_MODEL))
    coder_model: str | None = None
    max_tokens: int = 16000
    client: Any = None
    name: str = "claude"

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic(max_retries=3)
        self.coder_model = self.coder_model or os.environ.get("EXCEL_COPILOT_CODER_MODEL") or self.model

    def _call(self, *, model: str, system: str, context: str, turns: list[Turn], schema: type[T]) -> tuple[T, Usage]:
        import anthropic
        t0 = time.perf_counter()
        try:
            resp = self.client.messages.parse(
                model=model,
                max_tokens=self.max_tokens,
                system=[
                    {"type": "text", "text": system},
                    {"type": "text", "text": f"<workbook>\n{context}\n</workbook>",
                     "cache_control": {"type": "ephemeral"}},
                ],
                messages=[{"role": t.role, "content": t.content} for t in turns],
                output_format=schema,
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"could not reach the Claude API: {e}") from e
        except Exception as e:  # validation of the parsed output, etc.
            raise LLMError(f"Claude response could not be parsed as {schema.__name__}: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("the model declined this request")
        if resp.stop_reason == "max_tokens":
            raise LLMError("the model ran out of output tokens before finishing the JSON")
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError("the model returned no structured output")
        u = resp.usage
        usage = Usage(model=model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                      latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return parsed, usage

    def plan(self, question: str, context: str, profile) -> tuple[Plan, Usage]:
        return self._call(model=self.model, system=prompts.PLANNER_SYSTEM, context=context,
                          turns=[Turn("user", f"<question>{question}</question>")], schema=Plan)

    def code(self, question: str, plan: Plan, context: str, profile,
             history: list[Turn]) -> tuple[Program, Usage]:
        first = Turn("user", f"<question>{question}</question>\n\n<plan>\n{plan.model_dump_json(indent=1)}\n</plan>\n\n"
                             "Write the IR program.")
        return self._call(model=self.coder_model or self.model, system=prompts.CODER_SYSTEM, context=context,
                          turns=[first, *history], schema=Program)
