import { describe, expect, test } from "bun:test";
import { renderToString } from "react-dom/server";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
  type AssistantRuntime,
} from "@assistant-ui/react";
import {
  createChatAdapter,
  mergeVisibleMessage,
  toAssistantMessage,
  mergeToolProgress,
} from "./assistant-runtime";
import type { Message } from "./types";
import { AssistantToolTimeline } from "../components/assistant-ui/elements/document-tool-activity";
import { AssistantThinking } from "../components/assistant-ui/elements/thinking-indicator";
import { AssistantMessageContent, ChatSuggestions } from "../pages/chat";

function stored(id: string, changes: Partial<Message> = {}): Message {
  return {
    id,
    chat_id: "chat-1",
    role: "assistant",
    status: "complete",
    content: "The price is USD 1200 [c1].",
    suggestions: ["When can I cancel?"],
    citations: [
      {
        citation_id: "c1",
        document_id: "doc-1",
        version_id: "version-1",
        chunk_id: "chunk-1",
        filename: "policy.txt",
        version_number: 1,
        location: { line: 2 },
        quote: "Annual price: USD 1200.",
      },
    ],
    error: null,
    created_at: "2026-10-02T00:00:00Z",
    parent_id: "user-1",
    ...changes,
  };
}

// Exercise the published runtime and real message/composer primitives without
// a browser, DOM shim, provider call, or additional test dependency.
function harness(
  overrides: Partial<Parameters<typeof createChatAdapter>[0]> = {},
  productionContent = false,
) {
  const sends: string[] = [];
  const retries: string[] = [];
  let cancellations = 0;
  let runtime!: AssistantRuntime;
  function Thread() {
    runtime = useExternalStoreRuntime(
      createChatAdapter({
        messages: [
          stored("user-1", {
            role: "user",
            content: "What is the price?",
            parent_id: null,
          }),
          stored("answer-1"),
        ],
        isRunning: false,
        isLoading: false,
        isSendDisabled: false,
        onSend: async (text) => {
          sends.push(text);
        },
        onRetry: async (id) => {
          retries.push(id);
        },
        onCancel: async () => {
          cancellations++;
        },
        ...overrides,
      }),
    );
    return (
      <AssistantRuntimeProvider runtime={runtime}>
        <ThreadPrimitive.Root>
          <ThreadPrimitive.Messages>
            {({ message }) => (
              <MessagePrimitive.Root data-message={message.id}>
                {productionContent ? <AssistantMessageContent onCitation={() => {}} /> : <>
                  <AssistantThinking />
                  <AssistantToolTimeline defaultOpen />
                  <MessagePrimitive.Parts components={{ tools: { Fallback: () => null } }} />
                </>}
              </MessagePrimitive.Root>
            )}
          </ThreadPrimitive.Messages>
          <ComposerPrimitive.Root>
            <ComposerPrimitive.Input aria-label="Question" />
            <ComposerPrimitive.Send>Send</ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
          <ChatSuggestions message={overrides.messages?.at(-1) || stored("answer-1")}
            busy={overrides.isRunning || false} />
        </ThreadPrimitive.Root>
      </AssistantRuntimeProvider>
    );
  }
  const html = renderToString(<Thread />);
  return { runtime, html, sends, retries, cancellations: () => cancellations };
}

describe("Python history to assistant-ui", () => {
  test("preserves IDs, content, provenance and structured suggestions", () => {
    const message = stored("answer-1");
    const converted = toAssistantMessage(message);
    expect(converted.id).toBe(message.id);
    expect(converted.createdAt?.toISOString()).toBe(
      message.created_at.replace("Z", ".000Z"),
    );
    expect(converted.content).toBe(message.content);
    expect(converted.metadata?.custom?.docvault).toBe(message);
    const { runtime, html } = harness();
    expect(runtime.thread.getState().messages.map((m) => m.id)).toEqual([
      "user-1",
      "answer-1",
    ]);
    expect(html).toContain("The price is USD 1200 [c1].");
    expect(html.match(/data-message="answer-1"/g)).toHaveLength(1);
  });
  test.each([
    ["pending", { type: "running" }],
    ["streaming", { type: "running" }],
    ["complete", { type: "complete", reason: "stop" }],
    ["cancelled", { type: "incomplete", reason: "cancelled" }],
    [
      "failed",
      { type: "incomplete", reason: "error", error: "Provider unavailable" },
    ],
  ] as const)("maps persisted %s state explicitly", (status, expected) => {
    expect(
      toAssistantMessage(
        stored("answer-1", { status, error: "Provider unavailable" }),
      ).status,
    ).toEqual(expected);
  });
  test("retry replaces the visible attempt, then completion updates the same bubble", () => {
    const user = stored("user-1", { role: "user", parent_id: null });
    const retry = stored("retry-1", { status: "pending", content: "" });
    const original = [user, stored("answer-1")];
    const updated = mergeVisibleMessage(original, retry);
    expect(updated.map((m) => m.id)).toEqual(["user-1", "retry-1"]);
    expect(original[1]!.id).toBe("answer-1");
    const completed = mergeVisibleMessage(updated, {
      ...retry,
      status: "complete",
      content: "New answer",
    });
    expect(completed).toHaveLength(2);
    expect(completed[1]!.content).toBe("New answer");
    expect(
      mergeVisibleMessage(
        completed,
        stored("user-2", { role: "user", parent_id: null }),
      ),
    ).toHaveLength(3);
  });
});

describe("assistant-ui ThinkingIndicator", () => {
  test.each([
    { status: "pending", content: "", shown: true },
    { status: "streaming", content: "", shown: true },
    { status: "streaming", content: "  ", shown: true },
    { status: "streaming", content: "Answer text", shown: false },
    { status: "complete", content: "", shown: false },
    { status: "failed", content: "", shown: false },
    { status: "cancelled", content: "", shown: false },
  ] as const)(
    "$status with content '$content' shows indicator: $shown",
    ({ status, content, shown }) => {
      const { html } = harness({
        messages: [stored("answer-1", { status, content })],
        isRunning: status === "pending" || status === "streaming",
      });
      expect(html.includes('data-slot="thinking-indicator"')).toBe(shown);
      if (shown) {
        expect(html).toContain('role="status"');
        expect(html).toContain("Thinking with your documents");
        expect(html).toContain("0s");
      }
    },
  );
  test("renders one indicator for the optimistic assistant before server admission", () => {
    const { html } = harness({
      messages: [
        stored("user-1", {
          role: "user",
          parent_id: null,
          content: "Question",
        }),
      ],
      isRunning: true,
    });
    expect(html.match(/data-slot="thinking-indicator"/g)).toHaveLength(1);
  });
});

describe("assistant-ui callbacks", () => {
  test.each(["new", "existing"])("%s conversation sends text exactly once and clears its draft", async (conversation) => {
    const { runtime, sends } = harness(conversation === "new" ? { messages: [] } : {});
    runtime.thread.composer.setText("Explain cancellation");
    await runtime.thread.composer.send();
    await runtime.thread.composer.send();
    expect(sends).toEqual(["Explain cancellation"]);
    expect(runtime.thread.composer.getState().text).toBe("");
  });
  test("loading gate blocks sending but retains typed text", async () => {
    const { runtime, sends } = harness({ isSendDisabled: true });
    runtime.thread.composer.setText("Explain cancellation");
    await runtime.thread.composer.send();
    expect(sends).toEqual([]);
    expect(runtime.thread.composer.getState().text).toBe(
      "Explain cancellation",
    );
  });
  test("reload routes only the newest assistant attempt to the Python retry handler", async () => {
    const { runtime, retries } = harness({
      messages: [
        stored("user-1", { role: "user", parent_id: null }),
        stored("answer-1"),
        stored("user-2", { role: "user", parent_id: null }),
        stored("answer-2", { parent_id: "user-2" }),
      ],
    });
    runtime.thread.getMessageById("answer-1").reload();
    runtime.thread.getMessageById("answer-2").reload();
    await Promise.resolve();
    await Promise.resolve();
    expect(retries).toEqual(["answer-2"]);
  });
  test("Stop invokes cancellation without inventing a persisted cancelled message", async () => {
    const { runtime, retries, cancellations } = harness({
      messages: [stored("answer-1", { status: "streaming" })],
      isRunning: true,
    });
    runtime.thread.getMessageById("answer-1").reload();
    runtime.thread.cancelRun();
    await Promise.resolve();
    expect(cancellations()).toBe(1);
    expect(retries).toEqual([]);
    expect(runtime.thread.getState().messages[0]!.status).toEqual({
      type: "running",
    });
  });
});

describe("persisted tool progress", () => {
  test("upserts one invocation and renders native tool parts before answer text", () => {
    const trace = {
      tool_call_id: "call-1", tool: "retrieve_relevant_chunks",
      arguments: { query: "Annual price" }, status: "running", execution_status: "running" as const,
      observed_at: "2026-10-05T00:00:00Z",
    };
    const items = mergeToolProgress([stored("answer-1", { status: "streaming", content: "" })], "answer-1", trace);
    const converted = toAssistantMessage(items[0]!);
    expect(Array.isArray(converted.content)).toBe(true);
    const parts = converted.content as readonly { type: string; result?: unknown }[];
    expect(parts[0]?.type).toBe("tool-call");
    expect(parts[0]?.result).toBeUndefined();
    const completed = mergeToolProgress(items, "answer-1", {
      ...trace, status: "ok", execution_status: "completed", evidence_ids: ["c1"],
    });
    expect(completed[0]?.agent_trace).toHaveLength(1);
    const final = toAssistantMessage(completed[0]!);
    expect((final.content as readonly { result?: unknown }[])[0]?.result).toMatchObject({ source_count: 1, status: "completed" });
    expect(mergeToolProgress(items, "other-chat-message", trace)).toEqual(items);
  });
  test("real assistant-ui renderer shows tool state and safe details without raw output", () => {
    const trace = {
      tool_call_id: "call-1", tool: "get_selected_document_overviews", arguments: { cursor: null },
      status: "ok", execution_status: "completed" as const, observed_at: "2026-10-05T00:00:00Z",
      evidence_ids: ["c1"], document_references: [{ filename: "policy.txt" }],
    };
    const { html } = harness({ messages: [stored("answer-1", { agent_trace: [trace] })] });
    expect(html).toContain("Read document overviews");
    expect(html).toContain("Complete");
    expect(html).toContain("USD 1200");
  });
  test("failed tools remain distinct from successful tools in a failed answer", () => {
    const trace = { tool_call_id: "call-1", tool: "search_documents", arguments: { query: "pricing" }, status: "error", execution_status: "failed" as const, observed_at: "2026-10-05T00:00:00Z", error: { code: "source_missing", message: "Source unavailable.", retryable: false } };
    const converted = toAssistantMessage(stored("answer-1", { status: "failed", agent_trace: [trace] }));
    expect((converted.content as readonly { isError?: boolean }[])[0]?.isError).toBe(true);
    expect((converted.content as readonly { result?: unknown }[])[0]?.result).toMatchObject({ error: "Source unavailable." });
    expect(harness({ messages: [stored("answer-1", { status: "failed", agent_trace: [trace] })] }).html).toContain("Stopped");
  });
});

describe("production assistant content and follow-ups", () => {
  test("empty pending answers with no tool calls never render a stray zero", () => {
    // React previously rendered this guard as 0 when both fields were empty.
    const message = stored("answer-1", { status: "pending", content: "", agent_trace: [] });
    expect(renderToString(<div>{(message.content || message.agent_trace?.length) && <span>Answer</span>}</div>)).toBe("<div>0</div>");
    const { html } = harness({ messages: [message], isRunning: true }, true);
    expect(html).toContain("Thinking with your documents");
    expect(html).not.toMatch(/>0</);
    expect(html).not.toContain('data-slot="tool-timeline"');
  });
  test("a completed answer displays canonical follow-ups through Suggestion primitives", () => {
    expect(harness().html).toContain("When can I cancel?");
    const message = stored("answer-1", { suggestions: ["First?", "Second?", "Third?", "Fourth?"] });
    const { html } = harness({ messages: [message] });
    for (const question of message.suggestions.slice(0, 3)) expect(html).toContain(question);
    expect(html).not.toContain("Fourth?");
  });
  test("empty, streaming and terminal-failure suggestions are hidden", () => {
    for (const status of ["pending", "streaming", "failed", "cancelled"] as const) {
      expect(harness({ messages: [stored("answer-1", { status })] }).html).not.toContain("When can I cancel?");
    }
    expect(harness({ isRunning: true }).html).not.toContain("When can I cancel?");
    expect(harness({ messages: [stored("answer-1", { suggestions: [] })] }).html).not.toContain("Suggested follow-up questions");
  });
  test("production tool activity renders one timeline without duplicate per-part cards", () => {
    const trace = { tool_call_id: "call-1", tool: "retrieve_relevant_chunks", arguments: { query: "Price" }, status: "ok", execution_status: "completed" as const, observed_at: "2026-10-05T00:00:00Z" };
    const { html } = harness({ messages: [stored("answer-1", { agent_trace: [trace] })] }, true);
    expect(html.match(/data-slot="tool-timeline"/g)).toHaveLength(1);
    expect(html).toContain("1 tool call completed");
    expect(html).not.toContain('data-slot="tool-call"'); // collapsed by default
    expect(html).toContain("USD 1200");
  });
  test("parallel calls keep distinct rows and their individual completion states", () => {
    const base = { tool: "retrieve_relevant_chunks", arguments: { query: "Price" }, observed_at: "2026-10-05T00:00:00Z" };
    const traces = [
      { ...base, tool_call_id: "call-1", status: "ok", execution_status: "completed" as const, evidence_ids: ["c1"] },
      { ...base, tool_call_id: "call-2", status: "running", execution_status: "running" as const },
    ];
    const { html } = harness({ messages: [stored("answer-1", { content: "", status: "streaming", agent_trace: traces })], isRunning: true });
    expect(html.match(/data-slot="tool-call"/g)).toHaveLength(2);
    expect(html).toContain('aria-label="Searched selected documents: Complete"');
    expect(html).toContain('aria-label="Searched selected documents: In progress"');
  });
  test("native tool parts retain receipts without copying raw results into UI state", () => {
    const trace = { tool_call_id: "call-1", tool: "retrieve_relevant_chunks", arguments: { query: "Price" }, status: "ok", execution_status: "completed" as const, observed_at: "2026-10-05T00:00:00Z", evidence_ids: ["c1"], document_references: [{ filename: "policy.txt" }], raw_output: "PRIVATE DOCUMENT PASSAGE" };
    const converted = toAssistantMessage(stored("answer-1", { agent_trace: [trace] }));
    // Full SQL message metadata is kept separately; rendered native parts contain only receipts.
    expect(JSON.stringify(converted.content)).not.toContain(trace.raw_output);
    expect(JSON.stringify(converted.content)).toContain("policy.txt");
    expect(JSON.stringify(converted.content)).toContain('"source_count":1');
  });
});
