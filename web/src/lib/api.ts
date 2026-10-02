import type { Message, VaultDocument } from "./types";
export const API_URL = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
export function apiUrl(path: string) {
  return `${API_URL}${path}`;
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(apiUrl(path), { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      body?.error?.message ||
        `Request failed (${response.status}). Please try again.`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export type GenerationEvent =
  | { type: "message.started"; message: Message; user_message?: Message }
  | { type: "answer.delta"; text: string }
  | { type: "answer.completed" | "message.failed"; message: Message };
/** Decode SSE independently of network chunk boundaries, including CRLF framing. */
export function parseSSE(buffer: string): {
  events: { event: string; data: string }[];
  rest: string;
} {
  const events: { event: string; data: string }[] = [];
  let rest = buffer;
  let match: RegExpExecArray | null;
  while ((match = /\r?\n\r?\n/.exec(rest))) {
    const block = rest.slice(0, match.index);
    rest = rest.slice(match.index + match[0].length);
    let event = "message";
    const data: string[] = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length) events.push({ event, data: data.join("\n") });
  }
  return { events, rest };
}
export async function generate(
  path: string,
  content: string | undefined,
  onEvent: (event: GenerationEvent) => void,
  signal: AbortSignal,
) {
  const response = await fetch(apiUrl(path), {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify(content === undefined ? {} : { content }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      body?.error?.message || `Generation failed (${response.status}).`,
    );
  }
  if (!response.body) throw new Error("The response stream is unavailable.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let complete = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const parsed = parseSSE(buffer);
      buffer = parsed.rest;
      for (const frame of parsed.events) {
        const event = {
          ...JSON.parse(frame.data),
          type: frame.event,
        } as GenerationEvent;
        onEvent(event);
        if (
          event.type === "answer.completed" ||
          event.type === "message.failed"
        )
          complete = true;
      }
      if (done) break;
    }
    if (!complete)
      throw new Error(
        "Connection interrupted. The response may still be processing; your chat will refresh automatically.",
      );
  } finally {
    reader.releaseLock();
  }
}

/** Load every library page so the source picker never silently omits documents. */
export async function getDocuments(
  signal?: AbortSignal,
): Promise<{ items: VaultDocument[] }> {
  const items = new Map<string, VaultDocument>();
  const limit = 200;
  for (let offset = 0; ; offset += limit) {
    const page = await api<{ items: VaultDocument[] }>(
      `/v1/documents?limit=${limit}&offset=${offset}`,
      { signal },
    );
    page.items.forEach((document) => items.set(document.id, document));
    if (page.items.length < limit) return { items: [...items.values()] };
  }
}
