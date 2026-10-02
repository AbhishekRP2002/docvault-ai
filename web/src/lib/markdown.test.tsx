import { describe, expect, test } from "bun:test";
import { renderToString } from "react-dom/server";
import {
  AssistantRuntimeProvider,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import { createChatAdapter } from "./assistant-runtime";
import type { Citation, Message } from "./types";
import {
  MarkdownCitationContext,
  MarkdownText,
} from "../components/assistant-ui/elements/markdown-text";
import { ScrollToBottom } from "../components/assistant-ui/elements/scroll-to-bottom";
import { TooltipIconButton } from "../components/assistant-ui/elements/tooltip-icon-button";
import { Markdown } from "../components/markdown";

const source: Citation = {
  citation_id: "e97bc55c-0a28-441a-a15e-7f05a7d260ba",
  document_id: "document-1",
  version_id: "version-1",
  chunk_id: "e97bc55c-0a28-441a-a15e-7f05a7d260ba",
  filename: "contract.txt",
  version_number: 1,
  location: { line: 4 },
  quote: "Payment is due in 30 days.",
};

function renderMessage(content: string) {
  const message: Message = {
    id: "answer-1",
    chat_id: "chat-1",
    role: "assistant",
    status: "complete",
    content,
    citations: [source],
    suggestions: [],
    error: null,
    created_at: "2026-10-02T00:00:00Z",
    parent_id: "question-1",
  };
  function Thread() {
    const runtime = useExternalStoreRuntime(
      createChatAdapter({
        messages: [message],
        isRunning: false,
        isLoading: false,
        isSendDisabled: false,
        onSend: async () => {},
        onRetry: async () => {},
        onCancel: async () => {},
      }),
    );
    return (
      <AssistantRuntimeProvider runtime={runtime}>
        <ThreadPrimitive.Root>
          <ThreadPrimitive.ViewportProvider>
            <ThreadPrimitive.Messages>
              {() => (
                <MessagePrimitive.Root>
                  <MarkdownCitationContext.Provider value={() => {}}>
                    <MessagePrimitive.Parts
                      components={{ Text: MarkdownText }}
                    />
                  </MarkdownCitationContext.Provider>
                </MessagePrimitive.Root>
              )}
            </ThreadPrimitive.Messages>
            <ScrollToBottom />
          </ThreadPrimitive.ViewportProvider>
        </ThreadPrimitive.Root>
      </AssistantRuntimeProvider>
    );
  }
  return renderToString(<Thread />);
}

describe("runtime MarkdownText", () => {
  test("renders numbered source actions from real message-part context", () => {
    const html = renderMessage(
      `Payment is due in 30 days [source ${source.citation_id}].`,
    );
    expect(html).toContain('aria-label="View source 1"');
    expect(html).not.toContain(source.citation_id);
    expect(html).toContain('data-status="complete"');
  });

  test("preserves GFM tables, task lists, code language and accessible code copying", () => {
    const html = renderMessage(
      '# Terms\n\n| Term | Value |\n| --- | --- |\n| Payment | 30 days |\n\n- [x] Reviewed\n\n```python\nprint("hello")\n```',
    );
    expect(html).toContain("<h1>Terms</h1>");
    expect(html).toContain("<table>");
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('checked=""');
    expect(html).toContain("python");
    expect(html).toContain("Copy code");
    expect(html).toContain("print(&quot;hello&quot;)");
  });

  test("keeps safe external links and does not render raw HTML or script URLs", () => {
    const html = renderMessage(
      "[Read](https://example.com/policy) [Bad](javascript:alert%281%29)\n\n<script>alert(1)</script>",
    );
    expect(html).toContain('href="https://example.com/policy"');
    expect(html).toContain('target="_blank" rel="noopener noreferrer"');
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain("<script>");
  });

  test("hides unresolved citation IDs instead of exposing internal identifiers", () => {
    const unknown = "624490be-4584-423e-bbb5-940861502770";
    const html = renderMessage(`Unresolved [${unknown}].`);
    expect(html).toContain("[source]");
    expect(html).not.toContain(unknown);
    expect(html).not.toContain('aria-label="View source');
  });

  test("keeps the static document renderer on the same citation contract", () => {
    const html = renderToString(
      <Markdown
        citations={[source]}
        onCitation={() => {}}
      >{`Terms [${source.citation_id}]`}</Markdown>,
    );
    expect(html).toContain('aria-label="View source 1"');
    expect(html).not.toContain(source.citation_id);
  });
});

describe("runtime chat icon actions", () => {
  test("scroll-to-bottom starts disabled and hidden when viewport is already at bottom", () => {
    const html = renderMessage("Complete answer");
    const scrollButton = html.match(
      /<button[^>]*>[\s\S]*?Scroll to latest answer[\s\S]*?<\/button>/,
    )?.[0];
    expect(scrollButton).toBeDefined();
    expect(scrollButton).toContain('disabled=""');
    expect(scrollButton).toContain("disabled:invisible");
  });

  test("tooltip icon buttons preserve primitive disabled state and an accessible name", () => {
    const html = renderToString(
      <TooltipIconButton tooltip="Select files" disabled>
        <svg aria-hidden="true" />
      </TooltipIconButton>,
    );
    expect(html).toContain('disabled=""');
    expect(html).toContain('class="sr-only">Select files');
    expect(html).toContain('type="button"');
  });
});
