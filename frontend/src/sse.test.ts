import { describe, expect, it } from "vitest";
import { SSEParser, readSSE } from "./sse";

describe("SSEParser", () => {
  it("parses whole events", () => {
    const p = new SSEParser();
    expect(p.push('event: plan\ndata: {"a":1}\n\n')).toEqual([{ event: "plan", data: '{"a":1}' }]);
  });

  it("reassembles events split across arbitrary chunk boundaries", () => {
    const text = 'event: stage\ndata: {"stage":"plan"}\n\nevent: result\ndata: {"rows":[[1,2]]}\n\n';
    for (let cut = 1; cut < text.length; cut++) {
      const p = new SSEParser();
      const got = [...p.push(text.slice(0, cut)), ...p.push(text.slice(cut))];
      expect(got.map((m) => m.event)).toEqual(["stage", "result"]);
      expect(JSON.parse(got[1].data)).toEqual({ rows: [[1, 2]] });
    }
  });

  it("ignores comments, joins multi-line data, handles CRLF and default event", () => {
    const p = new SSEParser();
    const got = p.push(": stream open\r\n\r\ndata: line1\r\ndata: line2\r\n\r\n");
    expect(got).toEqual([{ event: "message", data: "line1\nline2" }]);
  });

  it("flushes a trailing event without blank line", () => {
    const p = new SSEParser();
    expect(p.push("event: done\ndata: {}")).toEqual([]);
    expect(p.flush()).toEqual([{ event: "done", data: "{}" }]);
  });
});

describe("readSSE", () => {
  it("decodes multi-byte UTF-8 split across chunks", async () => {
    const bytes = new TextEncoder().encode('event: log\ndata: {"m":"café ✓"}\n\n');
    const chunks = [bytes.slice(0, 27), bytes.slice(27, 29), bytes.slice(29)];
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        chunks.forEach((x) => c.enqueue(x));
        c.close();
      },
    });
    const out = [];
    for await (const m of readSSE(stream)) out.push(m);
    expect(out).toEqual([{ event: "log", data: '{"m":"café ✓"}' }]);
  });
});
