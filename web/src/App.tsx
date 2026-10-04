import { lazy, Suspense, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChartColumn,
  FileStack,
  FolderOpen,
  Menu,
  SquarePen,
  History,
  ChevronDown,
  Search,
  Settings2,
} from "lucide-react";
import { api, API_URL, getDocuments } from "@/lib/api";
import type { Chat, ProviderConfig } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { PastRunRow } from "@/components/past-run-row";
import { ErrorState, LoadingRows, PageSkeleton } from "@/components/common";
import { useDocumentUploads } from "@/hooks/use-document-uploads";
const Library = lazy(() =>
  import("@/pages/library").then((m) => ({ default: m.Library })),
);
const ChatPage = lazy(() =>
  import("@/pages/chat").then((m) => ({ default: m.ChatPage })),
);
const Analytics = lazy(() =>
  import("@/pages/analytics").then((m) => ({ default: m.Analytics })),
);
type Page = "library" | "chat" | "analytics";
function getLocation(): { page: Page; chat: string | null } {
  const [path, chat] = window.location.hash.slice(1).split("/");
  return {
    page:
      path === "agent" || path === "chat"
        ? "chat"
        : path === "analytics"
          ? "analytics"
          : "library",
    chat: (path === "agent" || path === "chat") && chat ? chat : null,
  };
}
export default function App() {
  const client = useQueryClient();
  const transfer = useDocumentUploads();
  const [location, setLocation] = useState(getLocation);
  const [initialVersions, setInitialVersions] = useState<string[]>([]);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [settings, setSettings] = useState(false);
  const [connected, setConnected] = useState(false);
  const [pastOpen, setPastOpen] = useState(true);
  const [runSearch, setRunSearch] = useState("");
  const [runNavigation, setRunNavigation] = useState(0);
  const chats = useQuery({
    queryKey: ["chats"],
    queryFn: () => api<{ items: Chat[] }>("/v1/chats"),
  });
  function selectRun(id: string | null) {
    setRunNavigation((value) => value + 1);
    if (!id) setInitialVersions([]);
    navigate("chat", id);
  }
  const documents = useQuery({
    queryKey: ["documents"],
    queryFn: ({ signal }) => getDocuments(signal),
    refetchInterval: (q) =>
      !connected ||
      q.state.data?.items.some((d) => !["ready", "failed"].includes(d.status))
        ? 3000
        : false,
  });
  const config = useQuery({
    queryKey: ["config"],
    queryFn: () => api<ProviderConfig>("/v1/config"),
    refetchInterval: 30000,
  });
  useEffect(() => {
    const handler = () => setLocation(getLocation());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  useEffect(() => {
    let closed = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout>;
    function connect() {
      const url = new URL(`${API_URL}/v1/events`, window.location.origin);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(url);
      socket.onopen = () => setConnected(true);
      socket.onmessage = () => {
        void client.invalidateQueries({ queryKey: ["documents"] });
        void client.invalidateQueries({ queryKey: ["metrics"] });
        void client.invalidateQueries({ queryKey: ["chats"] });
        void client.invalidateQueries({ queryKey: ["artifact"] });
      };
      socket.onclose = () => {
        setConnected(false);
        if (!closed) timer = setTimeout(connect, 5000);
      };
      socket.onerror = () => socket?.close();
    }
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, [client]);
  function navigate(page: Page, chat: string | null = null) {
    window.location.hash = `${page === "chat" ? "agent" : page}${chat ? `/${chat}` : ""}`;
    setLocation({ page, chat });
    setMobileOpen(false);
  }
  const nav = [
    { id: "library" as const, label: "Files", icon: FolderOpen },
    { id: "analytics" as const, label: "Usage", icon: ChartColumn },
  ];
  const sidebar = (
    <>
      <a
        href="#library"
        className="flex h-12 shrink-0 items-center gap-2.5 px-5 text-[15px] font-semibold tracking-tight"
        onClick={() => setMobileOpen(false)}
      >
        <span className="grid size-7 place-items-center rounded-md bg-foreground text-card">
          <FileStack className="size-4" />
        </span>
        DocVault
      </a>
      <div className="px-3 pt-4">
        <div className="mb-5 flex items-center gap-2.5 px-2">
          <span className="grid size-7 place-items-center rounded-md border bg-card text-xs font-medium">
            W
          </span>
          <div className="min-w-0">
            <p className="text-[13px] font-medium">My workspace</p>
            <p className="text-[11px] text-muted-foreground">
              Personal workspace
            </p>
          </div>
        </div>
        <p className="mb-2 px-2 text-[11px] font-medium text-muted-foreground">
          Workspace
        </p>
        <nav aria-label="Workspace" className="space-y-1">
          {nav.map(({ id, label, icon: Icon }) => (
            <Button
              key={id}
              variant="ghost"
              className={cn(
                "h-9 w-full justify-start gap-2.5 px-2 text-[13px] font-normal",
                location.page === id
                  ? "bg-blue-50 font-medium text-blue-700 hover:bg-blue-50 hover:text-blue-700"
                  : "text-muted-foreground",
              )}
              onClick={() => {
                navigate(id);
              }}
              aria-current={location.page === id ? "page" : undefined}
            >
              <Icon className="size-4" />
              {label}
              {id === "library" && documents.data && (
                <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">
                  {documents.data.items.length}
                </span>
              )}
            </Button>
          ))}
        </nav>
        <p className="mb-2 mt-7 px-2 text-[11px] font-medium text-muted-foreground">
          Agent
        </p>
        <Button
          variant="ghost"
          className={cn(
            "h-9 w-full justify-start gap-2.5 px-2 text-[13px] font-normal",
            location.page === "chat" &&
              !location.chat &&
              "bg-blue-50 font-medium text-blue-700 hover:bg-blue-50 hover:text-blue-700",
          )}
          onClick={() => selectRun(null)}
          aria-current={
            location.page === "chat" && !location.chat ? "page" : undefined
          }
        >
          <SquarePen className="size-4" />
          New run
        </Button>
        <Button
          variant="ghost"
          className="mt-1 h-9 w-full justify-start gap-2.5 px-2 text-[13px] font-normal text-muted-foreground"
          onClick={() => setPastOpen(!pastOpen)}
          aria-expanded={pastOpen}
          aria-controls="past-runs"
        >
          <History className="size-4" />
          Past runs
          <ChevronDown
            className={cn(
              "ml-auto size-3 transition-transform",
              !pastOpen && "-rotate-90",
            )}
          />
        </Button>
      </div>
      {pastOpen && (
        <div
          id="past-runs"
          className="min-h-0 flex-1 overflow-y-auto px-3 pb-4"
        >
          {!!chats.data?.items.length && (
            <div className="relative my-2">
              <Search className="absolute left-2.5 top-2 size-3.5 text-muted-foreground" />
              <Input
                aria-label="Search past runs"
                placeholder="Find a run…"
                value={runSearch}
                onChange={(e) => setRunSearch(e.target.value)}
                className="h-7 border-0 bg-transparent pl-8 text-xs shadow-none"
              />
            </div>
          )}
          {chats.isPending ? (
            <LoadingRows />
          ) : chats.error ? (
            <ErrorState
              error={chats.error}
              retry={() => void chats.refetch()}
            />
          ) : !chats.data?.items.length ? (
            <p className="py-2 pl-8 text-[12px] text-muted-foreground">
              No runs yet
            </p>
          ) : (
            chats.data.items
              .filter((c) =>
                c.title.toLowerCase().includes(runSearch.toLowerCase()),
              )
              .map((chat) => (
                <PastRunRow
                  key={chat.id}
                  chat={chat}
                  active={location.page === "chat" && location.chat === chat.id}
                  onSelect={() => selectRun(chat.id)}
                  onDeleted={(id) => {
                    if (location.page === "chat" && location.chat === id)
                      selectRun(null);
                    document.getElementById("main-content")?.focus();
                  }}
                />
              ))
          )}
          {!!runSearch &&
            !chats.data?.items.some((c) =>
              c.title.toLowerCase().includes(runSearch.toLowerCase()),
            ) && (
              <p className="py-2 pl-8 text-xs text-muted-foreground">
                No matching runs
              </p>
            )}
        </div>
      )}
      <div className="mt-auto shrink-0 border-t px-3 py-3">
        <Button
          variant="ghost"
          className="h-9 w-full justify-start gap-2 px-2 text-[12px] font-normal text-muted-foreground"
          onClick={() => setSettings(true)}
        >
          <Settings2 className="size-4" />
          Workspace settings
          <span
            className={cn(
              "ml-auto size-1.5 rounded-full",
              config.data?.configured ? "bg-emerald-600" : "bg-amber-500",
            )}
            aria-label={
              config.data?.configured
                ? "Provider configured"
                : "Provider not configured"
            }
          />
        </Button>
      </div>
    </>
  );
  return (
    <div className="flex h-dvh min-h-0 overflow-hidden">
      <aside className="hidden w-[216px] shrink-0 flex-col border-r bg-background lg:flex">
        {sidebar}
      </aside>
      <div className="flex min-w-0 flex-1 flex-col bg-card">
        <div className="flex h-12 shrink-0 items-center justify-between border-b bg-card px-5 sm:px-7">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              className="size-7 lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
            >
              <Menu />
            </Button>
            <div className="flex items-center gap-3 text-[13px]">
              <span className="hidden text-muted-foreground sm:inline">
                My workspace
              </span>
              <span className="hidden text-border sm:inline">/</span>
              <span>
                {location.page === "chat"
                  ? "Agent"
                  : location.page === "library"
                    ? "Files"
                    : "Usage"}
              </span>
              {location.page === "chat" && (
                <>
                  <span className="text-border">/</span>
                  <span className="max-w-[40vw] truncate font-medium">
                    {location.chat
                      ? chats.data?.items.find((c) => c.id === location.chat)
                          ?.title || "Run"
                      : "New Run"}
                  </span>
                </>
              )}
            </div>
          </div>
          <span
            className="flex items-center gap-1.5 text-[11px] text-muted-foreground"
            title={
              connected
                ? "Document status updates are connected"
                : "Checking for updates every few seconds"
            }
          >
            <span
              className={cn(
                "size-1.5 rounded-full",
                connected ? "bg-emerald-600" : "bg-amber-500",
              )}
            />
            {transfer.uploading
              ? "Uploading files"
              : connected
                ? "Live updates"
                : "Reconnecting"}
          </span>
        </div>
        {config.data && !config.data.configured && (
          <div className="flex shrink-0 items-center justify-center gap-2 border-b bg-amber-50 px-4 py-2 text-center text-[11px] text-amber-900">
            Add your OpenRouter key to enable processing and answers.
            <Button
              variant="ghost"
              size="sm"
              className="h-auto p-0 text-[11px] underline"
              onClick={() => setSettings(true)}
            >
              View setup
            </Button>
          </div>
        )}
        <main
          id="main-content"
          tabIndex={-1}
          className={cn(
            "min-h-0 flex-1",
            location.page === "chat" ? "overflow-hidden" : "overflow-y-auto",
          )}
        >
          <Suspense fallback={<PageSkeleton page={location.page} />}>
            {location.page === "library" ? (
              <Library
                documents={documents.data?.items || []}
                loading={documents.isPending}
                error={documents.error}
                refresh={() => void documents.refetch()}
                transfer={transfer}
                onChat={(ids) => {
                  setInitialVersions(ids);
                  navigate("chat");
                }}
              />
            ) : location.page === "chat" ? (
              <ChatPage
                documents={documents.data?.items || []}
                chatId={location.chat}
                onSelectChat={(id) => navigate("chat", id)}
                initialVersions={initialVersions}
                modelLabel={config.data?.chat_model.split("/").pop()}
                navigationRevision={runNavigation}
              />
            ) : (
              <Analytics />
            )}
          </Suspense>
        </main>
      </div>
      <Dialog open={mobileOpen} onOpenChange={setMobileOpen}>
        <DialogContent
          sheet
          className="left-0 right-auto flex max-w-[280px] flex-col gap-0 p-0"
        >
          <DialogTitle className="sr-only">Workspace navigation</DialogTitle>
          <DialogDescription className="sr-only">
            Navigate files, agent runs, and usage.
          </DialogDescription>
          {sidebar}
        </DialogContent>
      </Dialog>
      <Dialog open={settings} onOpenChange={setSettings}>
        <DialogContent>
          <DialogTitle>Workspace connection</DialogTitle>
          <DialogDescription>
            Your model provider powers document understanding and answers.
          </DialogDescription>
          {config.error ? (
            <div className="mt-5">
              <ErrorState
                error={config.error}
                retry={() => void config.refetch()}
              />
            </div>
          ) : (
            <div className="mt-6 space-y-5 text-sm">
              <div className="flex justify-between border-b pb-4">
                <span className="text-muted-foreground">Provider</span>
                <span>OpenRouter</span>
              </div>
              <div>
                <p className="mb-1 text-xs text-muted-foreground">Chat model</p>
                <p className="break-all font-mono text-xs">
                  {config.data?.chat_model || "Loading…"}
                </p>
              </div>
              <div>
                <p className="mb-1 text-xs text-muted-foreground">
                  Embedding model
                </p>
                <p className="break-all font-mono text-xs">
                  {config.data?.embedding_model || "Loading…"}
                </p>
              </div>
              <div className="rounded-lg bg-muted p-4 text-xs leading-6 text-muted-foreground">
                {config.data?.configured ? (
                  "The backend has an OpenRouter key configured. Model settings are managed by your local workspace configuration."
                ) : (
                  <>
                    Set{" "}
                    <code className="text-foreground">OPENROUTER_API_KEY</code>{" "}
                    in the backend <code className="text-foreground">.env</code>{" "}
                    file, then restart the API and worker. Your key stays on the
                    server.
                  </>
                )}
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
      <a
        className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded focus:bg-card focus:p-3"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Skip to content
      </a>
    </div>
  );
}
