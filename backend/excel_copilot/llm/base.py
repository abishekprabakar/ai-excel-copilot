"""Backend interface for the planner and coder stages."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..ir.models import Plan, Program
from ..profiler.models import WorkbookProfile


class LLMError(RuntimeError):
    pass


@dataclass
class Turn:
    role: str            # "user" | "assistant"
    content: str


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: float = 0.0


class Backend(Protocol):
    name: str

    def plan(self, question: str, context: str, profile: WorkbookProfile) -> tuple[Plan, Usage]: ...

    def code(self, question: str, plan: Plan, context: str, profile: WorkbookProfile,
             history: list[Turn]) -> tuple[Program, Usage]:
        """`history` holds prior (assistant program, user diagnostics) turns for repair."""
        ...


def get_backend(name: str | None = None) -> Backend:
    """'claude' (needs ANTHROPIC_API_KEY), 'offline', or None for auto."""
    import os
    name = (name or os.environ.get("EXCEL_COPILOT_LLM") or "auto").lower()
    if name == "auto":
        name = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "offline"
    if name == "claude":
        from .claude import ClaudeBackend
        return ClaudeBackend()
    if name == "offline":
        from .heuristic import HeuristicBackend
        return HeuristicBackend()
    raise ValueError(f"unknown LLM backend {name!r}")
