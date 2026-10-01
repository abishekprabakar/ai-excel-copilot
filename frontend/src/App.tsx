import { useCallback, useEffect, useRef, useState } from "react";
import { ask, getHealth, uploadWorkbook } from "./api";
import { RunView } from "./components/RunView";
import { WorkbookView } from "./components/WorkbookView";
import { applyEvent, newRun, type RunState } from "./run";
import type { UploadResponse } from "./types";

export default function App() {
  const [wb, setWb] = useState<UploadResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [runs, setRuns] = useState<RunState[]>([]);
  const [health, setHealth] = useState<{ llm: string; storage: string } | null>(null);
  const [drag, setDrag] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  const upload = useCallback(async (file: File) => {
    setBusy(true);
    setErr(null);
    try {
      const res = await uploadWorkbook(file);
      setWb(res);
      setRuns([]);
      setQuestion(res.suggested_questions[0] ?? "");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  async function trySample() {
    const blob = await (await fetch("/sales_ops.xlsx")).blob();
    await upload(new File([blob], "sales_ops.xlsx", { type: blob.type }));
  }

  async function submit(q: string) {
    if (!wb || !q.trim()) return;
    abort.current?.abort();
    const ctrl = new AbortController();
    abort.current = ctrl;
    let run = newRun(q.trim());
    setRuns((rs) => [run, ...rs]);
    const update = (r: RunState) => setRuns((rs) => rs.map((x) => (x.key === r.key ? r : x)));
    try {
      for await (const ev of ask(wb.workbook_id, q.trim(), ctrl.signal)) {
        run = applyEvent(run, ev);
        update(run);
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        run = { ...run, status: "failed", error: { stage: "network", message: (e as Error).message } };
        update(run);
      }
    }
    if (run.status === "running") update({ ...run, status: "failed", error: { stage: "network", message: "stream ended early" } });
  }

  const running = runs.some((r) => r.status === "running");

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" aria-hidden>
            ▦
          </span>
          Excel Copilot
        </div>
        <div className="muted small">
          {health ? (
            <>
              <span className={`pill ${health.llm}`}>{health.llm === "claude" ? "Claude" : "offline planner"}</span>
              <span className="pill">{health.storage === "s3" ? "S3" : "local storage"}</span>
            </>
          ) : (
            <span className="pill bad">API unreachable</span>
          )}
        </div>
      </header>

      <main className="layout">
        <aside className="left">
          <div
            className={`drop card ${drag ? "drag" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDrag(true);
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDrag(false);
              const f = e.dataTransfer.files[0];
              if (f) upload(f);
            }}
          >
            <p>
              <strong>Drop an .xlsx here</strong> or{" "}
              <button className="link" onClick={() => fileInput.current?.click()} disabled={busy}>
                choose a file
              </button>
            </p>
            <p className="small muted">
              or{" "}
              <button className="link" onClick={trySample} disabled={busy}>
                load the sample sales workbook
              </button>
            </p>
            <input
              ref={fileInput}
              type="file"
              accept=".xlsx,.xlsm"
              hidden
              onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
            />
            {busy && <p className="small">Profiling…</p>}
            {err && <p className="error-text small">{err}</p>}
          </div>
          {wb && <WorkbookView profile={wb.profile} workbookId={wb.workbook_id} />}
        </aside>

        <section className="right">
          <form
            className="ask card"
            onSubmit={(e) => {
              e.preventDefault();
              submit(question);
            }}
          >
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={wb ? "Ask a question about this workbook…" : "Upload a workbook first"}
              disabled={!wb}
              rows={2}
              maxLength={1000}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(question);
              }}
            />
            <div className="ask-row">
              <div className="chips">
                {wb?.suggested_questions.map((s) => (
                  <button type="button" key={s} className="chip" onClick={() => setQuestion(s)}>
                    {s}
                  </button>
                ))}
              </div>
              <button className="btn" type="submit" disabled={!wb || running || !question.trim()}>
                {running ? "Running…" : "Ask"}
              </button>
            </div>
          </form>
          {runs.map((r) => (
            <RunView key={r.key} run={r} />
          ))}
          {wb && !runs.length && (
            <p className="muted empty">
              Questions are planned, compiled to a validated IR, and run in a sandbox. Each answer comes with the plan,
              the generated program and downloadable results.
            </p>
          )}
        </section>
      </main>
    </div>
  );
}
