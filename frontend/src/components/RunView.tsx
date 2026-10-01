import { useState } from "react";
import { artifactUrl } from "../api";
import type { RunState } from "../run";
import { ResultView } from "./ResultView";

type StepStatus = "done" | "running" | "failed" | "pending";

function Step({ label, status, children, meta }: { label: string; status: StepStatus; meta?: string; children?: React.ReactNode }) {
  return (
    <li className={`step ${status}`}>
      <span className="dot" aria-hidden />
      <div className="step-body">
        <div className="step-title">
          <span>{label}</span>
          {meta && <span className="muted small">{meta}</span>}
        </div>
        {children}
      </div>
    </li>
  );
}

function Disclosure({ label, children }: { label: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="disclosure">
      <button className="link small" onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? "Hide" : "Show"} {label}
      </button>
      {open && children}
    </div>
  );
}

const ORDER = ["start", "plan", "code", "execute", "done"];

export function RunView({ run }: { run: RunState }) {
  const [downloading, setDownloading] = useState<string | null>(null);
  const at = ORDER.indexOf(run.stage ?? "start");
  const failedAt = run.error?.stage;
  const st = (name: string, has: boolean): StepStatus => {
    if (failedAt && (failedAt === name || (name === "code" && failedAt === "validate"))) return "failed";
    if (has) return "done";
    return ORDER.indexOf(name) === at && run.status === "running" ? "running" : "pending";
  };
  const lastAttempt = run.attempts[run.attempts.length - 1];

  async function download(name: string) {
    if (!run.runId) return;
    setDownloading(name);
    try {
      window.location.assign(await artifactUrl(run.runId, name));
    } finally {
      setTimeout(() => setDownloading(null), 800);
    }
  }

  return (
    <article className={`card run ${run.status}`}>
      <header className="run-head">
        <h3>{run.question}</h3>
        <span className={`status ${run.status}`}>
          {run.status === "running" ? "running…" : run.status === "ok" ? `${((run.elapsedMs ?? 0) / 1000).toFixed(1)} s` : run.status}
        </span>
      </header>
      <ol className="steps">
        <Step label="Context" status={st("start", !!run.context)} meta={run.context ? `${run.context.tokens} tokens` : undefined}>
          {run.context && (
            <Disclosure label="context sent to the model">
              <pre className="ctx">{run.context.text}</pre>
            </Disclosure>
          )}
        </Step>
        <Step label="Plan" status={st("plan", !!run.plan)} meta={run.backend}>
          {run.plan && (
            <>
              <ol className="plan">
                {run.plan.steps.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ol>
              {run.plan.assumptions.length > 0 && (
                <ul className="assumptions small muted">
                  {run.plan.assumptions.map((a, i) => (
                    <li key={i}>Assumption: {a}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Step>
        <Step
          label="IR program"
          status={st("code", !!lastAttempt?.diagnostics?.ok)}
          meta={run.attempts.length > 1 ? `${run.attempts.length} attempts` : undefined}
        >
          {run.attempts.map((a, i) =>
            a.diagnostics && !a.diagnostics.ok ? (
              <div key={i} className="diag">
                <span className="small strong">Attempt {String(a.attempt)} rejected by the validator</span>
                <ul>
                  {a.diagnostics.items
                    .filter((d) => d.severity === "error")
                    .map((d, j) => (
                      <li key={j} className="small mono">
                        step {d.step ?? "–"} · {d.field}: {d.message}
                        {d.hint ? ` (${d.hint})` : ""}
                      </li>
                    ))}
                </ul>
              </div>
            ) : null,
          )}
          {lastAttempt && (
            <Disclosure label="IR JSON">
              <pre className="code">{JSON.stringify(lastAttempt.program, null, 1)}</pre>
            </Disclosure>
          )}
        </Step>
        <Step label="Compiled & sandboxed" status={st("execute", !!run.result)} meta={run.logs[run.logs.length - 1]}>
          {run.code && (
            <Disclosure label="executed Python">
              <pre className="code">{run.code}</pre>
            </Disclosure>
          )}
        </Step>
      </ol>
      {run.error && (
        <div className="error-box">
          <strong>{run.error.stage}:</strong> {run.error.message}
        </div>
      )}
      {run.status === "unanswerable" && <div className="note-box">Can’t answer from this workbook: {run.reason}</div>}
      {run.result && <ResultView r={run.result} />}
      {run.artifacts.length > 0 && (
        <footer className="artifacts">
          {run.artifacts.map((a) => (
            <button key={a.name} className="btn ghost small" onClick={() => download(a.name)} disabled={downloading === a.name}>
              ↓ {a.name}
            </button>
          ))}
          {run.tokens && run.tokens.input > 0 && (
            <span className="muted small">
              {run.tokens.input.toLocaleString()} in / {run.tokens.output.toLocaleString()} out tokens
            </span>
          )}
        </footer>
      )}
    </article>
  );
}
