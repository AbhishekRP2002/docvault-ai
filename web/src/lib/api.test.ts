import { afterEach, describe, expect, test } from "bun:test";
import { generate, getDocuments, parseSSE } from "./api";
describe("SSE frame decoder", () => {
  test("preserves incomplete data across transport chunks", () => {
    const first = parseSSE(
      'event: answer.delta\ndata: {"text":"one"}\n\nevent: answer.delta\nda',
    );
    expect(first.events).toEqual([
      { event: "answer.delta", data: '{"text":"one"}' },
    ]);
    const second = parseSSE(first.rest + 'ta: {"text":"two"}\n\n');
    expect(second.events).toEqual([
      { event: "answer.delta", data: '{"text":"two"}' },
    ]);
    expect(second.rest).toBe("");
  });
  test("supports CRLF, comments, and multiline payloads", () => {
    expect(
      parseSSE(
        ': heartbeat\r\n\r\nevent: answer.completed\r\ndata: {\r\ndata: "ok": true}\r\n\r\n',
      ).events,
    ).toEqual([{ event: "answer.completed", data: '{\n"ok": true}' }]);
  });
});

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
function mockStream(chunks: string[]) {
  globalThis.fetch = (async () =>
    new Response(
      new ReadableStream({
        start(controller) {
          chunks.forEach((chunk) =>
            controller.enqueue(new TextEncoder().encode(chunk)),
          );
          controller.close();
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    )) as unknown as typeof fetch;
}
describe("generation transport", () => {
  test("does not report success when the connection ends before completion", async () => {
    mockStream(['event: answer.delta\ndata: {"text":"partial"}\n\n']);
    const received: string[] = [];
    await expect(
      generate(
        "/test",
        "question",
        (event) => received.push(event.type),
        new AbortController().signal,
      ),
    ).rejects.toThrow("Connection interrupted");
    expect(received).toEqual(["answer.delta"]);
  });
  test("delivers split transport frames through completion", async () => {
    mockStream([
      'event: answer.delta\ndata: {"text":"hello"}\n',
      '\nevent: answer.completed\ndata: {"message":{"id":"complete"}}\n\n',
    ]);
    const received: string[] = [];
    await generate(
      "/test",
      "question",
      (event) => received.push(event.type),
      new AbortController().signal,
    );
    expect(received).toEqual(["answer.delta", "answer.completed"]);
  });
  test("preserves the actionable server error before opening a stream", async () => {
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          error: { message: "A response is already running." },
        }),
        { status: 409 },
      )) as unknown as typeof fetch;
    await expect(
      generate("/test", "question", () => {}, new AbortController().signal),
    ).rejects.toThrow("A response is already running.");
  });
});

describe("complete document library", () => {
  test("continues beyond 200 documents and deduplicates shifted pages", async () => {
    const requested: string[] = [];
    globalThis.fetch = (async (url: string | URL | Request) => {
      requested.push(String(url));
      return Response.json({
        items:
          requested.length === 1
            ? Array.from({ length: 200 }, (_, index) => ({ id: String(index) }))
            : [{ id: "199" }, { id: "200" }],
      });
    }) as unknown as typeof fetch;
    const result = await getDocuments();
    expect(requested).toEqual([
      "/v1/documents?limit=200&offset=0",
      "/v1/documents?limit=200&offset=200",
    ]);
    expect(result.items).toHaveLength(201);
    expect(result.items.at(-1)?.id).toBe("200");
  });
});
