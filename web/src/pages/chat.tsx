import {
  ActionBarPrimitive,
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import {
  createChatAdapter,
  mergeVisibleMessage,
} from "@/lib/assistant-runtime";
import {
  MarkdownText,
  MarkdownCitationContext,
} from "@/components/assistant-ui/elements/markdown-text";
import { ScrollToBottom } from "@/components/assistant-ui/elements/scroll-to-bottom";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { AssistantThinking } from "@/components/assistant-ui/elements/thinking-indicator";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  Check,
  Copy,
  Plus,
  Sparkles,
  RefreshCw,
  Square,
  FileStack,
  X,
} from "lucide-react";
import { api, generate } from "@/lib/api";
import { deferStreamingMessageRefetch } from "@/lib/workspace-events";
import type { Chat, Citation, Message, VaultDocument } from "@/lib/types";
import { cn, errorMessage } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ErrorState, LoadingRows } from "@/components/common";
import { DocumentPicker } from "@/components/document-picker";
import { CitationDrawer, citationLocation } from "@/components/citation-drawer";
const EMPTY_MESSAGES: Message[] = [];
const TEXT_COMPONENTS = { Text: MarkdownText };
const activeStatus = (m: Message) =>
  m.role === "assistant" && ["pending", "streaming"].includes(m.status);
export function ChatPage({
  documents,
  chatId,
  onSelectChat,
  initialVersions,
  modelLabel,
  navigationRevision,
}: {
  documents: VaultDocument[];
  chatId: string | null;
  onSelectChat: (id: string | null) => void;
  initialVersions: string[];
  modelLabel?: string;
  navigationRevision: number;
}) {
  const client = useQueryClient();
  const [draftSources, setDraftSources] = useState(initialVersions);
  const [picker, setPicker] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [citation, setCitation] = useState<Citation | null>(null);
  const stream = useRef<AbortController | null>(null);
  const chats = useQuery({
    queryKey: ["chats"],
    queryFn: () => api<{ items: Chat[] }>("/v1/chats"),
  });
  const selectedChat = chats.data?.items.find((c) => c.id === chatId);
  const sources = chatId ? selectedChat?.version_ids || [] : draftSources;
  const messages = useQuery({
    queryKey: ["messages", chatId],
    queryFn: ({ signal }) =>
      api<{ items: Message[] }>(`/v1/chats/${chatId}/messages`, { signal }),
    enabled: !!chatId && !streaming,
    refetchInterval: (q) =>
      !streaming && q.state.data?.items.some(activeStatus) ? 1500 : false,
  });
  const items = messages.data?.items || EMPTY_MESSAGES;
  const active = items.find(activeStatus);
  const busy =
    streaming ||
    !!active ||
    (!!chatId && (messages.isPending || !!messages.error));
  const readyDocuments = documents.filter((d) => d.status === "ready");
  useEffect(() => {
    setDraftSources(initialVersions);
  }, [initialVersions]);
  useEffect(() => () => stream.current?.abort(), []);
  const edit = useMutation({
    mutationFn: (data: { version_ids: string[] }) =>
      api<Chat>(`/v1/chats/${chatId}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["chats"] });
      setPicker(false);
    },
  });
  function mergeMessage(id: string, incoming: Message) {
    client.setQueryData<{ items: Message[] }>(["messages", id], (old) => ({
      items: mergeVisibleMessage(old?.items || [], incoming),
    }));
  }
  async function run(content?: string, retryId?: string) {
    if (
      stream.current ||
      busy ||
      (!retryId && !content?.trim()) ||
      !sources.length
    )
      return;
    setError(null);
    setStreaming(true);
    let id = chatId;
    let started = false;
    let responseId: string | null = null;
    let releaseMessageRefetch: (() => void) | undefined;
    const controller = new AbortController();
    stream.current = controller;
    try {
      if (!id) {
        const created = await api<Chat>("/v1/chats", {
          method: "POST",
          body: JSON.stringify({
            title: content?.slice(0, 70) || "New conversation",
            version_ids: sources,
          }),
        });
        id = created.id;
        if (stream.current !== controller) return;
        client.setQueryData<{ items: Chat[] }>(["chats"], (old) => ({
          items: [created, ...(old?.items || [])],
        }));
        client.setQueryData(["messages", id], { items: [] });
        onSelectChat(id);
      }
      releaseMessageRefetch = deferStreamingMessageRefetch(client, id);
      await client.cancelQueries({ queryKey: ["messages", id], exact: true });
      const targetId = id;
      await generate(
        `/v1/chats/${id}/messages${retryId ? `/${retryId}/retry` : ""}`,
        content,
        (event) => {
          if (event.type === "message.started") {
            started = true;
            responseId = event.message.id;
            if (event.user_message) mergeMessage(targetId, event.user_message);
            mergeMessage(targetId, event.message);
          }
          if (event.type === "answer.delta" && responseId)
            client.setQueryData<{ items: Message[] }>(
              ["messages", targetId],
              (old) => ({
                items:
                  old?.items.map((m) =>
                    m.id === responseId
                      ? {
                          ...m,
                          content: m.content + event.text,
                          status: "streaming" as const,
                        }
                      : m,
                  ) || [],
              }),
            );
          if (
            event.type === "answer.completed" ||
            event.type === "message.failed"
          )
            mergeMessage(targetId, event.message);
        },
        controller.signal,
      );
    } catch (err) {
      if (
        stream.current === controller &&
        !(err instanceof DOMException && err.name === "AbortError")
      ) {
        setError(errorMessage(err));
        if (!started && content) runtime.thread.composer.setText(content);
      }
    } finally {
      releaseMessageRefetch?.();
      if (stream.current === controller) {
        setStreaming(false);
        stream.current = null;
      }
      if (id) void client.invalidateQueries({ queryKey: ["messages", id] });
      void client.invalidateQueries({ queryKey: ["chats"] });
    }
  }
  async function cancel() {
    if (!chatId || !active) return;
    setCancelling(true);
    const controller = stream.current;
    try {
      const message = await api<Message>(
        `/v1/chats/${chatId}/messages/${active.id}/cancel`,
        { method: "POST" },
      );
      mergeMessage(chatId, message);
      controller?.abort();
      if (stream.current === controller) setStreaming(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCancelling(false);
    }
  }
  const latestAssistant = [...items]
    .reverse()
    .find((m) => m.role === "assistant");
  const runtime = useExternalStoreRuntime(
    createChatAdapter({
      messages: items,
      isRunning: streaming || !!active,
      isLoading: !!chatId && messages.isPending,
      isSendDisabled: !sources.length || busy,
      onSend: (text) => run(text),
      onRetry: (id) => run(undefined, id),
      onCancel: cancel,
    }),
  );
  useEffect(() => {
    runtime.thread.composer.setText("");
    setError(null);
    setCitation(null);
    setPicker(false);
  }, [chatId, runtime]);
  useEffect(() => {
    // Creating a session during generation preserves its stream. Only explicit
    // sidebar navigation changes this revision and stops the current request.
    stream.current?.abort();
    stream.current = null;
    setStreaming(false);
    runtime.thread.composer.setText("");
    setError(null);
  }, [navigationRevision, runtime]);
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div className="flex h-full min-h-0">
        <ThreadPrimitive.ViewportProvider key={chatId || "new"}>
          <ThreadPrimitive.Root className="flex min-w-0 flex-1 flex-col bg-card">
            <ThreadPrimitive.Viewport
              className={cn(
                "min-h-0 flex-1 overflow-y-auto scroll-smooth",
                !items.length && !chatId && "flex items-end justify-center",
              )}
            >
              <div
                className={cn(
                  "mx-auto w-full max-w-3xl px-5 sm:px-8",
                  items.length || chatId ? "py-8" : "pb-7 pt-10",
                )}
              >
                {messages.error ? (
                  <ErrorState
                    error={messages.error}
                    retry={() => void messages.refetch()}
                  />
                ) : chatId && messages.isPending ? (
                  <LoadingRows />
                ) : !items.length && !streaming ? (
                  <div className="text-center">
                    <h1 className="flex items-center justify-center gap-3 text-[26px] font-semibold tracking-[-.8px] sm:text-[30px]">
                      <Sparkles
                        className="size-6 shrink-0 text-muted-foreground"
                        aria-hidden="true"
                      />
                      <span>What are we working on?</span>
                    </h1>
                    <p className="mt-3 text-[13px] text-muted-foreground">
                      Ask a question. Get answers from your files.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-9">
                    <ThreadPrimitive.Messages>
                      {({ message }) => {
                        const stored = message.metadata.custom.docvault as
                          | Message
                          | undefined;
                        return stored ? (
                          <MessageView
                            message={stored}
                            retryable={
                              stored.id === latestAssistant?.id && !busy
                            }
                            onCitation={setCitation}
                          />
                        ) : (
                          <PendingMessage />
                        );
                      }}
                    </ThreadPrimitive.Messages>
                  </div>
                )}
              </div>
            </ThreadPrimitive.Viewport>
            <div
              className={cn(
                "relative px-5 sm:px-8",
                !items.length && !chatId
                  ? "min-h-0 flex-1 pb-10"
                  : "shrink-0 pb-5",
              )}
            >
              <ScrollToBottom className="absolute -top-12 left-1/2 z-10 -translate-x-1/2" />
              <div className="mx-auto max-w-3xl">
                {error && (
                  <div className="mb-3 flex items-start gap-2">
                    <div className="flex-1">
                      <ErrorState error={error} />
                    </div>
                    <TooltipIconButton
                      tooltip="Dismiss error"
                      className="size-7"
                      onClick={() => setError(null)}
                    >
                      <X />
                    </TooltipIconButton>
                  </div>
                )}
                {!busy &&
                  latestAssistant?.status === "complete" &&
                  !!latestAssistant.suggestions?.length && (
                    <div className="mb-3 flex flex-wrap gap-2">
                      {latestAssistant.suggestions
                        .slice(0, 3)
                        .map((suggestion) => (
                          <ThreadPrimitive.Suggestion
                            key={suggestion}
                            prompt={suggestion}
                            send
                            disabled={!sources.length}
                            asChild
                          >
                            <Button
                              className="h-auto max-w-full whitespace-normal rounded-full px-3 py-2 text-left text-xs font-normal"
                              variant="outline"
                            >
                              {suggestion}
                              <ArrowUp className="size-3 shrink-0 rotate-45 text-muted-foreground" />
                            </Button>
                          </ThreadPrimitive.Suggestion>
                        ))}
                    </div>
                  )}
                <ComposerPrimitive.Root className="rounded-xl border bg-card p-3.5 shadow-[0_2px_6px_rgb(0_0_0/3%)] transition-shadow focus-within:border-primary/40 focus-within:ring-2 focus-within:ring-primary/8">
                  <ComposerPrimitive.Input
                    aria-label="Your question"
                    maxLength={4000}
                    placeholder={
                      sources.length
                        ? "Ask anything about your documents…"
                        : "Select files to start a run…"
                    }
                    submitMode="enter"
                    maxRows={8}
                    className="min-h-[60px] w-full resize-none rounded-md border-0 bg-transparent p-1 text-[14px] outline-none placeholder:text-muted-foreground disabled:opacity-50"
                    disabled={busy}
                  />
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <TooltipIconButton
                        tooltip="Select files"
                        className="size-8 text-muted-foreground"
                        disabled={busy}
                        onClick={() => setPicker(true)}
                      >
                        <Plus className="size-4" />
                      </TooltipIconButton>
                      {sources.length > 0 && (
                        <span className="text-[12px] text-muted-foreground">
                          {sources.length}{" "}
                          {sources.length === 1 ? "file" : "files"}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3">
                      {modelLabel && (
                        <span className="hidden text-[11px] text-muted-foreground sm:inline">
                          {modelLabel}
                        </span>
                      )}
                      {busy ? (
                        <ComposerPrimitive.Cancel
                          asChild
                          disabled={!active || cancelling}
                        >
                          <Button type="button" variant="secondary" size="sm">
                            <Square className="size-3 fill-current" />
                            {cancelling ? "Stopping…" : "Stop"}
                          </Button>
                        </ComposerPrimitive.Cancel>
                      ) : (
                        <ComposerPrimitive.Send asChild>
                          <TooltipIconButton
                            tooltip="Send question"
                            variant="default"
                            className="size-8 rounded-lg"
                          >
                            <ArrowUp />
                          </TooltipIconButton>
                        </ComposerPrimitive.Send>
                      )}
                    </div>
                  </div>
                </ComposerPrimitive.Root>
                <p className="mt-2.5 text-center text-[11px] text-muted-foreground">
                  Answers include source citations. Review important details.
                </p>
              </div>
            </div>
          </ThreadPrimitive.Root>
        </ThreadPrimitive.ViewportProvider>
        <DocumentPicker
          open={picker}
          onOpenChange={setPicker}
          documents={documents}
          selected={sources}
          saving={edit.isPending}
          error={edit.error ? errorMessage(edit.error) : undefined}
          onSave={(ids) => {
            if (chatId) edit.mutate({ version_ids: ids });
            else {
              setDraftSources(ids);
              setPicker(false);
            }
          }}
        />
        <CitationDrawer citation={citation} onClose={() => setCitation(null)} />
        {readyDocuments.length === 0 && !documents.length && (
          <span className="sr-only">
            Upload a document in the library before chatting.
          </span>
        )}
      </div>
    </AssistantRuntimeProvider>
  );
}
function PendingMessage() {
  return (
    <MessagePrimitive.Root className="animate-enter">
      <AssistantThinking className="text-xs" />
    </MessagePrimitive.Root>
  );
}
function MessageView({
  message,
  retryable,
  onCitation,
}: {
  message: Message;
  retryable: boolean;
  onCitation: (citation: Citation) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  if (message.role === "user")
    return (
      <MessagePrimitive.Root className="ml-auto max-w-[90%] rounded-2xl rounded-tr-md bg-muted px-5 py-3.5 text-sm leading-7 whitespace-pre-wrap animate-enter">
        {message.content}
      </MessagePrimitive.Root>
    );
  return (
    <MessagePrimitive.Root asChild>
      <article className="animate-enter">
        <div className="mb-4 flex items-center gap-2.5">
          <div className="grid size-7 place-items-center rounded-lg bg-primary/8 text-primary">
            <FileStack className="size-3.5" />
          </div>
          <span className="text-xs font-semibold">DocVault</span>
        </div>
        <div className="text-sm">
          <AssistantThinking className="py-3 text-xs" />
          {message.content && (
            <MarkdownCitationContext.Provider value={onCitation}>
              <MessagePrimitive.Parts components={TEXT_COMPONENTS} />
            </MarkdownCitationContext.Provider>
          )}
        </div>
        {message.error && (
          <div className="mt-3">
            <ErrorState error={message.error} />
          </div>
        )}
        {message.status === "cancelled" && (
          <p className="mt-3 text-xs text-muted-foreground">
            Response stopped. You can retry this answer.
          </p>
        )}
        {!!message.citations?.length && (
          <div className="mt-5">
            <p className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Sources
            </p>
            <div className="flex flex-wrap gap-2">
              {message.citations.map((source, index) => (
                <Button
                  key={`${source.citation_id}-${index}`}
                  variant="outline"
                  size="sm"
                  className="max-w-full gap-2 rounded-lg text-[11px] font-normal"
                  onClick={() => onCitation(source)}
                  title={citationLocation(source)}
                >
                  <span className="grid size-4 shrink-0 place-items-center rounded bg-muted text-[9px] font-semibold">
                    {index + 1}
                  </span>
                  <span className="max-w-48 truncate">{source.filename}</span>
                  {source.location.page && (
                    <span className="text-muted-foreground">
                      p. {source.location.page}
                    </span>
                  )}
                </Button>
              ))}
            </div>
          </div>
        )}
        {!activeStatus(message) && (
          <ActionBarPrimitive.Root className="mt-3 flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2 text-[11px] font-normal text-muted-foreground"
              disabled={!message.content}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(message.content);
                  setCopied(true);
                  setCopyError(false);
                  timer.current = setTimeout(() => setCopied(false), 2000);
                } catch {
                  setCopyError(true);
                }
              }}
            >
              {copied ? (
                <Check className="size-3" />
              ) : (
                <Copy className="size-3" />
              )}
              {copied ? "Copied" : "Copy"}
            </Button>
            {retryable && (
              <ActionBarPrimitive.Reload asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-[11px] font-normal text-muted-foreground"
                >
                  <RefreshCw className="size-3" />
                  {message.status === "complete" ? "Regenerate" : "Retry"}
                </Button>
              </ActionBarPrimitive.Reload>
            )}
            {copyError && (
              <span role="alert" className="text-[11px] text-destructive">
                Could not copy. Select the text to copy manually.
              </span>
            )}
          </ActionBarPrimitive.Root>
        )}
      </article>
    </MessagePrimitive.Root>
  );
}
