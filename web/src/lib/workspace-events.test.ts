import { describe, expect, spyOn, test } from "bun:test";
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import {
  createWorkspaceEventReconciler,
  deferStreamingMessageRefetch,
} from "./workspace-events";

function createTab(initial = "old answer") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } },
  });
  const key = ["messages", "chat-1"];
  client.setQueryData(key, { items: [initial] });
  let storedAnswer = initial;
  let requests = 0;
  const observer = new QueryObserver(client, {
    queryKey: key,
    queryFn: async () => {
      requests++;
      return { items: [storedAnswer] };
    },
  });
  const unsubscribe = observer.subscribe(() => {});
  return {
    client, key,
    requests: () => requests,
    store: (answer: string) => { storedAnswer = answer; },
    close: () => { unsubscribe(); client.clear(); },
  };
}

describe("workspace event reconciliation", () => {
  test("two idle tabs refetch persisted messages on changes, not unchanged snapshots", async () => {
    const tabs = [createTab(), createTab()];
    const invalidations = tabs.map((tab) => spyOn(tab.client, "invalidateQueries"));
    try {
      const reconcile = tabs.map((tab) => createWorkspaceEventReconciler(tab.client));
      for (const receive of reconcile) {
        expect(await receive('{"type":"snapshot","revision":"epoch:0"}')).toBe(true);
        expect(await receive('{"type":"snapshot","revision":"epoch:0"}')).toBe(false);
      }
      tabs.forEach((tab) => tab.store("new persisted answer"));
      for (const receive of reconcile) {
        expect(await receive({ type: "update", revision: "epoch:1" })).toBe(true);
        expect(await receive({ type: "snapshot", revision: "epoch:1" })).toBe(false);
      }
      for (const tab of tabs) {
        expect(tab.requests()).toBe(2);
        expect(tab.client.getQueryData<{ items: string[] }>(tab.key)).toEqual({ items: ["new persisted answer"] });
      }
      invalidations.forEach((invalidate) => expect(invalidate).toHaveBeenCalledTimes(2));
    } finally {
      invalidations.forEach((invalidate) => invalidate.mockRestore());
      tabs.forEach((tab) => tab.close());
    }
  });

  test("reconnection reconciles once even with the same revision, including server restarts", async () => {
    const tab = createTab();
    try {
      const original = createWorkspaceEventReconciler(tab.client);
      await original({ type: "snapshot", revision: "epoch:10" });
      tab.store("missed while disconnected");
      const reconnected = createWorkspaceEventReconciler(tab.client);
      expect(await reconnected({ type: "snapshot", revision: "epoch:10" })).toBe(true);
      expect(await reconnected({ type: "snapshot", revision: "epoch:10" })).toBe(false);
      expect(tab.client.getQueryData<{ items: string[] }>(tab.key)).toEqual({ items: ["missed while disconnected"] });
      expect(await reconnected({ type: "update", revision: "new-epoch:0" })).toBe(true);
      expect(tab.requests()).toBe(3);
    } finally { tab.close(); }
  });

  test("invalidates inactive document/version/artifact caches without polling diagnostics", async () => {
    const client = new QueryClient();
    const keys = [
      ["documents"], ["versions", "document-1"], ["insights", "version-2"],
      ["messages", "chat-inactive"], ["chats"], ["artifact", "comparison-1"],
      ["comparisons", 0], ["metrics", "processing"],
    ];
    const untouched = [["processing-diagnostics", "version-2"], ["config"], ["source", "version-1"]];
    try {
      [...keys, ...untouched].forEach((key) => client.setQueryData(key, "cached"));
      const receive = createWorkspaceEventReconciler(client);
      await receive({ type: "snapshot", revision: "epoch:1" });
      keys.forEach((key) => expect(client.getQueryState(key)?.isInvalidated).toBe(true));
      untouched.forEach((key) => expect(client.getQueryState(key)?.isInvalidated).toBe(false));
      // A mounted historical-version view reads fresh data after its inactive cache became stale.
      const observer = new QueryObserver(client, {
        queryKey: ["versions", "document-1"], staleTime: Infinity,
        queryFn: async () => "new version",
      });
      const stop = observer.subscribe(() => {});
      await observer.refetch();
      expect(client.getQueryData<string>(["versions", "document-1"])).toBe("new version");
      stop();
    } finally { client.clear(); }
  });

  test("malformed and unknown events do not invalidate or consume the first valid revision", async () => {
    const tab = createTab();
    try {
      const receive = createWorkspaceEventReconciler(tab.client);
      for (const payload of ["{", "null", "[]", null, 1,
        { type: "snapshot" }, { type: "update", revision: 1 },
        { type: "snapshot", revision: "  " }, { type: "heartbeat", revision: "epoch:1" },
      ]) expect(await receive(payload)).toBe(false);
      expect(tab.requests()).toBe(0);
      expect(await receive({ type: "snapshot", revision: "epoch:1" })).toBe(true);
      expect(tab.requests()).toBe(1);
    } finally { tab.close(); }
  });

  test("workspace events preserve SSE deltas, then generation completion reconciles stored messages", async () => {
    const sender = createTab();
    const idle = createTab();
    try {
      const release = deferStreamingMessageRefetch(sender.client, "chat-1");
      sender.client.setQueryData(sender.key, { items: ["optimistic token delta"] });
      sender.store("complete stored answer");
      idle.store("complete stored answer");
      await createWorkspaceEventReconciler(sender.client)({ type: "snapshot", revision: "epoch:1" });
      await createWorkspaceEventReconciler(idle.client)({ type: "snapshot", revision: "epoch:1" });
      expect(sender.requests()).toBe(0);
      expect(sender.client.getQueryData<{ items: string[] }>(sender.key)).toEqual({ items: ["optimistic token delta"] });
      expect(sender.client.getQueryState(sender.key)?.isInvalidated).toBe(true);
      expect(idle.client.getQueryData<{ items: string[] }>(idle.key)).toEqual({ items: ["complete stored answer"] });
      release();
      release(); // Finalization is safe if called twice.
      await sender.client.invalidateQueries({ queryKey: sender.key });
      expect(sender.requests()).toBe(1);
      expect(sender.client.getQueryData<{ items: string[] }>(sender.key)).toEqual({ items: ["complete stored answer"] });
    } finally { sender.close(); idle.close(); }
  });

  test("cancels a pre-stream read so its late response cannot overwrite optimistic tokens", async () => {
    const client = new QueryClient();
    const key = ["messages", "chat-1"];
    let finishRead: ((value: string) => void) | undefined;
    let signal: AbortSignal | undefined;
    client.setQueryData(key, "old stored content");
    const reading = client.fetchQuery({ queryKey: key, queryFn: ({ signal: requestSignal }) => {
      signal = requestSignal;
      return new Promise<string>((resolve) => { finishRead = resolve; });
    } }).catch(() => undefined);
    const release = deferStreamingMessageRefetch(client, "chat-1");
    try {
      await client.cancelQueries({ queryKey: key, exact: true });
      expect(signal?.aborted).toBe(true);
      client.setQueryData(key, "optimistic token delta");
      finishRead?.("late stale stored response");
      await reading;
      await createWorkspaceEventReconciler(client)({ type: "update", revision: "epoch:1" });
      expect(client.getQueryData<string>(key)).toBe("optimistic token delta");
    } finally { release(); client.clear(); }
  });
});
