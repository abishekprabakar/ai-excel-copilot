import { useState } from "react";
import type { Column, Profile, Table } from "../types";
import { GraphView } from "./GraphView";

const ROLE_LABEL: Record<string, string> = {
  identifier: "id",
  measure: "measure",
  dimension: "dim",
  temporal: "time",
  text: "text",
  flag: "flag",
  empty: "empty",
};

function fmt(n: number): string {
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + "k";
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

function ColumnRow({ c }: { c: Column }) {
  const detail =
    c.formula_readable ??
    (c.numeric && c.role !== "identifier" ? `${fmt(c.numeric.min)} – ${fmt(c.numeric.max)}` : null) ??
    (c.date_min ? `${c.date_min} – ${c.date_max}` : null) ??
    (c.top_values.length ? c.top_values.slice(0, 4).map(([v]) => v).join(", ") : null) ??
    c.samples.slice(0, 2).join(", ");
  return (
    <tr>
      <td className="mono">
        {c.name}
        {c.issues
          .filter((i) => i.code !== "pii")
          .map((i) => (
            <div key={i.code}>
              <span className={`flag ${i.severity}`} title={i.message}>
                {i.code.replace(/_/g, " ")}
                {i.count > 1 ? ` ×${i.count}` : ""}
              </span>
            </div>
          ))}
      </td>
      <td>
        <span className={`badge t-${c.type}`}>{c.type}</span>
      </td>
      <td className="muted">{ROLE_LABEL[c.role] ?? c.role}</td>
      <td className={c.formula_readable ? "mono formula" : "muted"} title={detail ?? ""}>
        {c.pii ? <span className="badge pii">PII · {c.pii}</span> : null} {detail}
      </td>
    </tr>
  );
}

function TableCard({ t, open, onToggle }: { t: Table; open: boolean; onToggle: () => void }) {
  return (
    <section className="card table-card">
      <button className="table-head" onClick={onToggle} aria-expanded={open}>
        <span className="caret">{open ? "▾" : "▸"}</span>
        <span className="mono strong">{t.id}</span>
        <span className="muted">
          {t.sheet}!{t.range}
        </span>
        <span className="spacer" />
        {t.layout === "wide" && <span className="badge wide">wide</span>}
        {t.source === "excel_table" && <span className="badge">Excel table</span>}
        <span className="muted">{t.n_rows.toLocaleString()} rows</span>
      </button>
      {open && (
        <div className="table-body">
          {t.title && <p className="muted small">“{t.title}”</p>}
          <table className="cols">
            <tbody>
              {t.columns.map((c) => (
                <ColumnRow key={c.name} c={c} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function WorkbookView({ profile, workbookId }: { profile: Profile; workbookId: string }) {
  const [tab, setTab] = useState<"tables" | "graph" | "issues">("tables");
  const [open, setOpen] = useState<Record<string, boolean>>(() => ({ [profile.tables[0]?.id ?? ""]: true }));
  const issues = [
    ...profile.issues.map((i) => ({ where: "workbook", ...i })),
    ...profile.tables.flatMap((t) => [
      ...t.issues.map((i) => ({ where: t.id, ...i })),
      ...t.columns.flatMap((c) => c.issues.filter((i) => i.code !== "pii").map((i) => ({ where: `${t.id}.${c.name}`, ...i }))),
    ]),
  ];
  return (
    <div className="workbook">
      <div className="wb-summary">
        <div>
          <div className="strong">{profile.file_name}</div>
          <div className="muted small">
            {profile.sheets.length} sheets · {profile.tables.length} tables · {profile.relationships.length} links · profiled in{" "}
            {Math.round(profile.profiled_ms)} ms
          </div>
        </div>
      </div>
      <div className="tabs" role="tablist">
        {(["tables", "graph", "issues"] as const).map((k) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {k === "issues" ? `Issues (${issues.length})` : k[0].toUpperCase() + k.slice(1)}
          </button>
        ))}
      </div>
      {tab === "tables" &&
        profile.tables.map((t) => (
          <TableCard key={t.id} t={t} open={!!open[t.id]} onToggle={() => setOpen((o) => ({ ...o, [t.id]: !o[t.id] }))} />
        ))}
      {tab === "graph" && <GraphView profile={profile} workbookId={workbookId} />}
      {tab === "issues" && (
        <ul className="issues card">
          {issues.map((i, n) => (
            <li key={n}>
              <span className={`flag ${i.severity}`}>{i.severity}</span>
              <span className="mono small">{i.where}</span>
              <span>{i.message}</span>
            </li>
          ))}
          {!issues.length && <li className="muted">No issues found.</li>}
        </ul>
      )}
    </div>
  );
}
