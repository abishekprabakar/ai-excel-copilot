// Incremental parser for text/event-stream bodies read through fetch().
// EventSource cannot POST, so the ask endpoint is read as a byte stream and
// parsed here. Handles events split across chunks, CRLF line endings,
// multi-line data fields and comment lines (": keep-alive").

export interface SSEMessage {
  event: string;
  data: string;
}

export class SSEParser {
  private buffer = "";

  push(chunk: string): SSEMessage[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");
    const out: SSEMessage[] = [];
    let idx: number;
    while ((idx = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      const msg = parseBlock(block);
      if (msg) out.push(msg);
    }
    return out;
  }

  flush(): SSEMessage[] {
    const rest = this.buffer;
    this.buffer = "";
    const msg = rest.trim() ? parseBlock(rest) : null;
    return msg ? [msg] : [];
  }
}

function parseBlock(block: string): SSEMessage | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

export async function* readSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<SSEMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const m of parser.push(decoder.decode(value, { stream: true }))) yield m;
    }
    for (const m of parser.push(decoder.decode())) yield m;
    for (const m of parser.flush()) yield m;
  } finally {
    reader.releaseLock();
  }
}
