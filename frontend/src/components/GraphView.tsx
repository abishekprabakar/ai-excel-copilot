import { useEffect, useState } from "react";
import { getContext } from "../api";
import type { Profile } from "../types";

// Tables as nodes, relationships as edges. Layout: fact tables (those with
// outgoing links) on the left, the tables they point to stacked on the right,
// everything else underneath. Deterministic, so screenshots are stable.
export function GraphView({ profile, workbookId }: { profile: Profile; workbookId: string }) {
  const [ctx, setCtx] = useState<{ text: string; tokens: number } | null>(null);
  const [budget, setBudget] = useState(600);
  useEffect(() => {
    let live = true;
    getContext(workbookId, "", budget).then((c) => live && setCtx(c)).catch(() => live && setCtx(null));
    return () => {
      live = false;
    };
  }, [workbookId, budget]);

  const sources = new Set(profile.relationships.map((r) => r.from_table));
  const targets = new Set(profile.relationships.map((r) => r.to_table));
  const left = profile.tables.filter((t) => sources.has(t.id));
  const right = profile.tables.filter((t) => targets.has(t.id) && !sources.has(t.id));
  const rest = profile.tables.filter((t) => !sources.has(t.id) && !targets.has(t.id));
  const W = 420;
  const NW = 140;
  const NH = 44;
  const pos: Record<string, { x: number; y: number }> = {};
  const rightGap = 62;
  right.forEach((t, i) => (pos[t.id] = { x: W - NW - 8, y: 12 + i * rightGap }));
  const rightH = Math.max(right.length * rightGap, 60);
  left.forEach((t, i) => (pos[t.id] = { x: 8, y: 12 + rightH / 2 - (left.length * rightGap) / 2 + i * rightGap }));
  const baseY = 12 + Math.max(rightH, left.length * rightGap) + 10;
  rest.forEach((t, i) => (pos[t.id] = { x: 8 + (i % 2) * (W - NW - 16), y: baseY + Math.floor(i / 2) * rightGap }));
  const H = baseY + Math.ceil(rest.length / 2) * rightGap + 8;

  return (
    <div className="card graph">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Table relationship graph">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" className="edge-head" />
          </marker>
        </defs>
        {profile.relationships.map((r, i) => {
          const a = pos[r.from_table];
          const b = pos[r.to_table];
          if (!a || !b) return null;
          const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
          const mx = (x1 + x2) / 2;
          return (
            <g key={i}>
              <path
                d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}`}
                className={`edge ${r.kind}`}
                markerEnd="url(#arrow)"
              >
                <title>
                  {r.from_table}.{r.from_column} → {r.to_table}.{r.to_column} ({r.kind.replace("_", " ")},{" "}
                  {(r.containment * 100).toFixed(1)}% match)
                </title>
              </path>
              <text x={x2 - 6} y={y2 - 5} textAnchor="end" className="edge-label">
                {r.from_column}
              </text>
            </g>
          );
        })}
        {profile.tables.map((t) => {
          const p = pos[t.id];
          return (
            <g key={t.id} transform={`translate(${p.x},${p.y})`}>
              <rect width={NW} height={NH} rx={8} className={`node ${t.layout}`} />
              <text x={10} y={18} className="node-title">
                {t.id}
              </text>
              <text x={10} y={34} className="node-sub">
                {t.n_rows} rows · {t.columns.length} cols{t.layout === "wide" ? " · wide" : ""}
              </text>
            </g>
          );
        })}
      </svg>
      <div className="legend small muted">
        <span><i className="sw fk" /> key match</span>
        <span><i className="sw lookup" /> lookup formula</span>
      </div>
      <div className="ctx-head">
        <span className="strong small">LLM context</span>
        <label className="small muted">
          budget{" "}
          <select value={budget} onChange={(e) => setBudget(Number(e.target.value))}>
            {[200, 400, 600, 1200, 3000].map((b) => (
              <option key={b} value={b}>
                {b} tokens
              </option>
            ))}
          </select>
        </label>
        {ctx && <span className="small muted">≈ {ctx.tokens} tokens</span>}
      </div>
      <pre className="ctx">{ctx?.text ?? "…"}</pre>
    </div>
  );
}
