import type { Diagnostic, Plan, ResultData, RunEvent, Usage } from "./types";

export interface Attempt {
  attempt: number | string;
  program: unknown;
  usage?: Usage;
  diagnostics?: { ok: boolean; items: Diagnostic[] };
}

export interface RunState {
  key: string;
  question: string;
  status: "running" | "ok" | "failed" | "unanswerable";
  runId?: string;
  backend?: string;
  stage?: string;
  context?: { tokens: number; text: string };
  plan?: Plan;
  attempts: Attempt[];
  code?: string;
  logs: string[];
  result?: ResultData;
  artifacts: { name: string; bytes: number }[];
  error?: { stage: string; message: string; items?: Diagnostic[] };
  reason?: string;
  elapsedMs?: number;
  tokens?: { input: number; output: number };
  startedAt: number;
}

export function newRun(question: string): RunState {
  return { key: crypto.randomUUID(), question, status: "running", attempts: [], logs: [], artifacts: [], startedAt: Date.now() };
}

/** Pure reducer: fold one server event into the run state. */
export function applyEvent(s: RunState, ev: RunEvent): RunState {
  switch (ev.type) {
    case "stage":
      return {
        ...s,
        stage: ev.data.stage,
        runId: ev.data.run_id ?? s.runId,
        backend: ev.data.backend ?? s.backend,
      };
    case "context":
      return { ...s, context: { tokens: ev.data.tokens, text: ev.data.text } };
    case "plan":
      return { ...s, plan: ev.data.plan };
    case "ir":
      return { ...s, attempts: [...s.attempts, { attempt: ev.data.attempt, program: ev.data.program, usage: ev.data.usage }] };
    case "diagnostics": {
      const attempts = s.attempts.slice();
      const i = attempts.findIndex((a) => a.attempt === ev.data.attempt);
      if (i >= 0) attempts[i] = { ...attempts[i], diagnostics: { ok: ev.data.ok, items: ev.data.items } };
      return { ...s, attempts };
    }
    case "code":
      return { ...s, code: ev.data.source };
    case "log":
      return { ...s, logs: [...s.logs, ev.data.message] };
    case "result":
      return { ...s, result: ev.data };
    case "artifact":
      return { ...s, artifacts: [...s.artifacts, { name: ev.data.name, bytes: ev.data.bytes }] };
    case "error":
      return { ...s, error: ev.data };
    case "done":
      return {
        ...s,
        stage: "done",
        status: ev.data.ok ? "ok" : ev.data.answerable === false ? "unanswerable" : "failed",
        runId: ev.data.run_id ?? s.runId,
        reason: ev.data.reason,
        elapsedMs: ev.data.elapsed_ms,
        tokens: ev.data.tokens,
      };
    default:
      return s;
  }
}
