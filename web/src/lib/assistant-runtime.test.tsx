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
} from "./assistant-runtime";
import type { Message } from "./types";
import { AssistantThinking } from "../components/assistant-ui/elements/thinking-indicator";

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
                <AssistantThinking />
                {message.content
                  .map((part) => (part.type === "text" ? part.text : ""))
                  .join("")}
              </MessagePrimitive.Root>
            )}
          </ThreadPrimitive.Messages>
          <ComposerPrimitive.Root>
            <ComposerPrimitive.Input aria-label="Question" />
            <ComposerPrimitive.Send>Send</ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
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
  test("composer sends text exactly once and clears its draft", async () => {
    const { runtime, sends } = harness();
    runtime.thread.composer.setText("Explain cancellation");
    await runtime.thread.composer.send();
    await runtime.thread.composer.send();
    expect(sends).toEqual(["Explain cancellation"]);
    expect(runtime.thread.composer.getState().text).toBe("");
  });
  test("source/loading gate blocks sending but retains typed text", async () => {
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
