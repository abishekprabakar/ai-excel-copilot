import { readSSE } from "./sse";
import type { RunEvent, UploadResponse, WorkbookGraph } from "./types";

let BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

/** In AWS the API URL is written to /config.json at deploy time, so one build serves any stage. */
export async function loadConfig(): Promise<void> {
  try {
    const r = await fetch("/config.json", { cache: "no-store" });
    if (r.ok) {
      const c = (await r.json()) as { apiUrl?: string };
      if (c.apiUrl) BASE = c.apiUrl.replace(/\/$/, "");
    }
  } catch {
    /* no config: same-origin API (dev proxy / nginx) */
  }
}

export async function getHealth(): Promise<{ llm: string; storage: string }> {
  return json(await fetch(`${BASE}/healthz`));
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  let key: string | null = null;
  try {
    key = localStorage.getItem("excelCopilot.apiKey");
  } catch {
    /* storage may be unavailable */
  }
  return key ? { ...extra, "X-API-Key": key } : extra;
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export async function uploadWorkbook(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  return json(await fetch(`${BASE}/v1/workbooks`, { method: "POST", body: form, headers: headers() }));
}

export async function getGraph(id: string): Promise<WorkbookGraph> {
  return json(await fetch(`${BASE}/v1/workbooks/${id}/graph`, { headers: headers() }));
}

export async function getContext(id: string, question: string, budget: number) {
  const q = new URLSearchParams({ question, budget: String(budget) });
  return json<{ text: string; tokens: number; levels: Record<string, number> }>(
    await fetch(`${BASE}/v1/workbooks/${id}/context?${q}`, { headers: headers() }),
  );
}

export async function* ask(id: string, question: string, signal?: AbortSignal): AsyncGenerator<RunEvent> {
  const res = await fetch(`${BASE}/v1/workbooks/${id}/ask`, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify({ question }),
    signal,
  });
  if (!res.ok || !res.body) {
    await json(res);
    throw new Error("no response body");
  }
  for await (const m of readSSE(res.body)) {
    yield { type: m.event, data: JSON.parse(m.data) } as RunEvent;
  }
}

export async function artifactUrl(runId: string, name: string): Promise<string> {
  const r = await json<{ url: string }>(
    await fetch(`${BASE}/v1/runs/${runId}/artifacts/${name}`, { headers: headers() }),
  );
  return r.url;
}
