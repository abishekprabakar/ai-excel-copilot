import { describe, expect, it } from "vitest";
import { applyEvent, newRun } from "./run";
import type { RunEvent } from "./types";

const events: RunEvent[] = [
  { type: "stage", data: { stage: "start", run_id: "r1", backend: "claude" } },
  { type: "context", data: { tokens: 420, levels: {}, text: "WORKBOOK x" } },
  { type: "plan", data: { plan: { answerable: true, reason: "", tables: ["orders"], columns: [], steps: ["a"], assumptions: [] }, usage: { model: "m", input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, latency_ms: 1 } } },
  { type: "ir", data: { attempt: 1, program: { steps: [] }, usage: { model: "m", input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, latency_ms: 1 } } },
  { type: "diagnostics", data: { attempt: 1, ok: false, items: [{ step: 1, field: "columns", message: "bad", severity: "error" }] } },
  { type: "ir", data: { attempt: 2, program: { steps: [] }, usage: { model: "m", input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, latency_ms: 1 } } },
  { type: "diagnostics", data: { attempt: 2, ok: true, items: [] } },
  { type: "code", data: { source: "def run(tables):\n    return x\n", tables: ["orders"] } },
  { type: "result", data: { title: "T", row_count: 1, truncated: false, chart: { kind: "none", x: "", y: "" }, columns: [{ name: "a", dtype: "int64" }], rows: [[1]] } },
  { type: "artifact", data: { name: "result.xlsx", key: "runs/r1/result.xlsx", bytes: 10 } },
  { type: "done", data: { ok: true, run_id: "r1", elapsed_ms: 900, tokens: { input: 3, output: 3 } } },
];

describe("applyEvent", () => {
  it("folds a full successful run", () => {
    const s = events.reduce(applyEvent, newRun("q"));
    expect(s.status).toBe("ok");
    expect(s.runId).toBe("r1");
    expect(s.backend).toBe("claude");
    expect(s.attempts.map((a) => a.diagnostics?.ok)).toEqual([false, true]);
    expect(s.result?.rows).toEqual([[1]]);
    expect(s.artifacts.map((a) => a.name)).toEqual(["result.xlsx"]);
  });

  it("marks unanswerable questions distinctly from failures", () => {
    const s = applyEvent(newRun("q"), { type: "done", data: { ok: false, answerable: false, reason: "no data" } });
    expect(s.status).toBe("unanswerable");
    expect(s.reason).toBe("no data");
    expect(applyEvent(newRun("q"), { type: "done", data: { ok: false } }).status).toBe("failed");
  });
});
