import type { ResultData } from "../types";

function cell(v: unknown, dtype: string): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (dtype.startsWith("int") || dtype.startsWith("Int")) return v.toLocaleString();
    return Math.abs(v) < 10 && !Number.isInteger(v)
      ? v.toLocaleString(undefined, { maximumFractionDigits: 4 })
      : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}T00:00:00/.test(v)) return v.slice(0, 10);
  return String(v);
}

function Chart({ r }: { r: ResultData }) {
  const xi = r.columns.findIndex((c) => c.name === r.chart.x);
  const yi = r.columns.findIndex((c) => c.name === r.chart.y);
  if (r.chart.kind === "none" || xi < 0 || yi < 0) return null;
  const pts = r.rows.slice(0, 40).map((row) => ({ x: cell(row[xi], r.columns[xi].dtype), y: Number(row[yi]) || 0 }));
  if (pts.length < 2) return null;
  const W = 560, H = 200, L = 64, B = 34, T = 10, R = 10;
  const max = Math.max(...pts.map((p) => p.y), 0);
  const min = Math.min(...pts.map((p) => p.y), 0);
  const span = max - min || 1;
  const y = (v: number) => T + (H - T - B) * (1 - (v - min) / span);
  const step = (W - L - R) / pts.length;
  const ticks = [min, min + span / 2, max];
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${r.chart.y} by ${r.chart.x}`}>
      {ticks.map((t, i) => (
        <g key={i}>
          <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} className="grid" />
          <text x={L - 6} y={y(t) + 4} textAnchor="end" className="axis">
            {Math.abs(t) >= 1e3 ? `${(t / 1e3).toFixed(0)}k` : t.toFixed(t % 1 ? 2 : 0)}
          </text>
        </g>
      ))}
      {r.chart.kind === "bar" &&
        pts.map((p, i) => (
          <rect key={i} x={L + i * step + step * 0.15} width={step * 0.7} y={Math.min(y(p.y), y(0))}
            height={Math.abs(y(0) - y(p.y))} rx={3} className="bar">
            <title>{`${p.x}: ${p.y.toLocaleString()}`}</title>
          </rect>
        ))}
      {r.chart.kind === "line" && (
        <polyline className="line" points={pts.map((p, i) => `${L + i * step + step / 2},${y(p.y)}`).join(" ")} />
      )}
      {r.chart.kind === "line" &&
        pts.map((p, i) => (
          <circle key={i} cx={L + i * step + step / 2} cy={y(p.y)} r={3} className="dot">
            <title>{`${p.x}: ${p.y.toLocaleString()}`}</title>
          </circle>
        ))}
      {pts.map((p, i) =>
        pts.length <= 16 || i % Math.ceil(pts.length / 16) === 0 ? (
          <text key={i} x={L + i * step + step / 2} y={H - B + 16} textAnchor="middle" className="axis">
            {p.x.length > 10 ? p.x.slice(0, 9) + "…" : p.x}
          </text>
        ) : null,
      )}
    </svg>
  );
}

export function ResultView({ r }: { r: ResultData }) {
  return (
    <div className="result">
      <div className="result-head">
        <span className="strong">{r.title}</span>
        <span className="muted small">
          {r.row_count.toLocaleString()} row{r.row_count === 1 ? "" : "s"}
          {r.truncated ? " (truncated)" : ""}
        </span>
      </div>
      <Chart r={r} />
      <div className="grid-wrap">
        <table className="data">
          <thead>
            <tr>
              {r.columns.map((c) => (
                <th key={c.name} className={/int|float/i.test(c.dtype) ? "num" : ""}>
                  {c.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {r.rows.map((row, i) => (
              <tr key={i}>
                {row.map((v, j) => (
                  <td key={j} className={typeof v === "number" ? "num" : ""}>
                    {cell(v, r.columns[j].dtype)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
