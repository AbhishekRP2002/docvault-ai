import type { QueryClient, QueryKey } from "@tanstack/react-query";

const workspaceQueries = new Set([
  "documents", "versions", "insights", "messages", "chats", "artifact",
  "comparisons", "metrics",
]);
const streamingChats = new WeakMap<QueryClient, Map<string, number>>();

/** Keep workspace refetches from replacing local SSE deltas before generation ends. */
export function deferStreamingMessageRefetch(client: QueryClient, chatId: string) {
  const chats = streamingChats.get(client) || new Map<string, number>();
  streamingChats.set(client, chats);
  chats.set(chatId, (chats.get(chatId) || 0) + 1);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const remaining = (chats.get(chatId) || 1) - 1;
    if (remaining) chats.set(chatId, remaining);
    else chats.delete(chatId);
  };
}

function isWorkspaceQuery(key: QueryKey) {
  return typeof key[0] === "string" && workspaceQueries.has(key[0]);
}

/** Create one reconciler per socket connection so reconnects recover missed changes. */
export function createWorkspaceEventReconciler(client: QueryClient) {
  let lastRevision: string | null = null;
  return async (payload: unknown): Promise<boolean> => {
    let event: unknown;
    try {
      event = typeof payload === "string" ? JSON.parse(payload) : payload;
    } catch {
      return false;
    }
    if (!event || typeof event !== "object") return false;
    const { type, revision } = event as Record<string, unknown>;
    if (
      (type !== "snapshot" && type !== "update") ||
      typeof revision !== "string" || !revision.trim() ||
      revision === lastRevision
    ) return false;
    lastRevision = revision;
    // Inactive queries and protected streams still become stale for their next read.
    await client.invalidateQueries({
      predicate: (query) => isWorkspaceQuery(query.queryKey),
      refetchType: "none",
    });
    await client.refetchQueries({
      type: "active",
      predicate: (query) => isWorkspaceQuery(query.queryKey) && !(
        query.queryKey[0] === "messages" &&
        typeof query.queryKey[1] === "string" &&
        streamingChats.get(client)?.has(query.queryKey[1])
      ),
    });
    return true;
  };
}
