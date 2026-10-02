import type {
  AppendMessage,
  ExternalStoreAdapter,
  ThreadMessageLike,
} from "@assistant-ui/react";
import type { Message } from "./types";

// SQL/SSE messages remain authoritative. The UI runtime keeps citation and
// suggestion data on the message rather than treating it as model content.
export function toAssistantMessage(message: Message): ThreadMessageLike {
  return {
    id: message.id,
    role: message.role,
    createdAt: new Date(message.created_at),
    content: message.content,
    ...(message.role === "assistant" && {
      status:
        message.status === "pending" || message.status === "streaming"
          ? { type: "running" as const }
          : message.status === "cancelled"
            ? { type: "incomplete" as const, reason: "cancelled" as const }
            : message.status === "failed"
              ? {
                  type: "incomplete" as const,
                  reason: "error" as const,
                  error: message.error,
                }
              : { type: "complete" as const, reason: "stop" as const },
    }),
    metadata: { custom: { docvault: message } },
  };
}

export function questionText(message: AppendMessage): string {
  return message.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("\n");
}

export function createChatAdapter({
  onSend,
  onRetry,
  onCancel,
  ...state
}: {
  messages: Message[];
  isRunning: boolean;
  isLoading: boolean;
  isSendDisabled: boolean;
  onSend: (text: string) => Promise<void>;
  onRetry: (id: string) => Promise<void>;
  onCancel: () => Promise<void>;
}): ExternalStoreAdapter<Message> {
  const latest = [...state.messages]
    .reverse()
    .find((message) => message.role === "assistant");
  return {
    ...state,
    convertMessage: toAssistantMessage,
    onNew: (message) => onSend(questionText(message)),
    onReload: (_parentId, config) => {
      // Match the backend's latest-turn-only retry policy.
      if (state.isRunning || !latest || config.sourceId !== latest.id)
        return Promise.resolve();
      return onRetry(latest.id);
    },
    onCancel,
  };
}

// Match GET history's newest-attempt-per-turn contract during a retry stream,
// too. The server retains older attempts; they are not duplicate chat bubbles.
export function mergeVisibleMessage(
  items: Message[],
  incoming: Message,
): Message[] {
  const index = items.findIndex(
    (message) =>
      message.id === incoming.id ||
      (incoming.role === "assistant" &&
        incoming.parent_id !== null &&
        message.role === "assistant" &&
        message.parent_id === incoming.parent_id),
  );
  return index === -1
    ? [...items, incoming]
    : items.map((message, i) => (i === index ? incoming : message));
}
