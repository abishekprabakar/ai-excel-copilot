export interface Issue {
  code: string;
  severity: "info" | "warning" | "error";
  message: string;
  location?: string | null;
  count: number;
}

export interface Column {
  name: string;
  letter: string;
  type: string;
  role: string;
  type_confidence: number;
  non_null: number;
  nulls: number;
  distinct: number;
  unique: boolean;
  numeric?: { min: number; max: number; mean: number; sum: number; p50: number } | null;
  date_min?: string | null;
  date_max?: string | null;
  top_values: [string, number][];
  samples: string[];
  pii?: string | null;
  formula_readable?: string | null;
  issues: Issue[];
}

export interface Table {
  id: string;
  sheet: string;
  name: string;
  source: "excel_table" | "detected";
  range: string;
  n_rows: number;
  title?: string | null;
  layout: "long" | "wide";
  columns: Column[];
  issues: Issue[];
}

export interface Relationship {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  kind: "foreign_key" | "formula_lookup";
  containment: number;
  confidence: number;
}

export interface Profile {
  file_name: string;
  sha256: string;
  size_bytes: number;
  sheets: { name: string; state: string; tables: string[]; loose_cells: [string, string][] }[];
  tables: Table[];
  relationships: Relationship[];
  named_ranges: { name: string; refers_to: string; value?: string | null }[];
  issues: Issue[];
  has_macros: boolean;
  profiled_ms: number;
}

export interface UploadResponse {
  workbook_id: string;
  profile: Profile;
  suggested_questions: string[];
}

export interface WorkbookGraph {
  nodes: { id: string; kind: string; label: string; attrs: Record<string, unknown> }[];
  edges: { source: string; target: string; kind: string; weight: number }[];
}

export interface Plan {
  answerable: boolean;
  reason: string;
  tables: string[];
  columns: string[];
  steps: string[];
  assumptions: string[];
}

export interface Diagnostic {
  step: number | null;
  field: string;
  message: string;
  hint?: string | null;
  severity: "error" | "warning";
}

export interface Usage {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  latency_ms: number;
}

export interface ResultData {
  title: string;
  row_count: number;
  truncated: boolean;
  chart: { kind: "none" | "bar" | "line"; x: string; y: string };
  columns: { name: string; dtype: string }[];
  rows: unknown[][];
}

export type RunEvent =
  | { type: "stage"; data: { stage: string; status?: string; attempt?: number; run_id?: string; backend?: string } }
  | { type: "context"; data: { tokens: number; levels: Record<string, number>; text: string } }
  | { type: "plan"; data: { plan: Plan; usage: Usage } }
  | { type: "ir"; data: { attempt: number | string; program: unknown; usage: Usage } }
  | { type: "diagnostics"; data: { attempt: number | string; ok: boolean; items: Diagnostic[] } }
  | { type: "code"; data: { source: string; tables: string[] } }
  | { type: "log"; data: { stage: string; message: string } }
  | { type: "result"; data: ResultData }
  | { type: "artifact"; data: { name: string; key: string; bytes: number } }
  | { type: "error"; data: { stage: string; message: string; items?: Diagnostic[] } }
  | {
      type: "done";
      data: {
        ok: boolean;
        run_id?: string;
        answerable?: boolean;
        reason?: string;
        elapsed_ms?: number;
        tokens?: { input: number; output: number };
      };
    };
