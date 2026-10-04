import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  FileText,
  GitCompareArrows,
  LayoutGrid,
  List,
  LoaderCircle,
  MessageSquare,
  Search,
  Trash2,
  Upload,
  X,
  RefreshCw,
} from "lucide-react";
import { api } from "@/lib/api";
import type { VaultDocument } from "@/lib/types";
import type { useDocumentUploads } from "@/hooks/use-document-uploads";
import { bytes, cn } from "@/lib/utils";
import {
  fileColumns,
  fileColumnsStorageKey,
  parseFileColumns,
} from "@/lib/document-columns";
import {
  DocumentColumnValue,
  FileColumnsMenu,
} from "@/components/document-columns";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ConfirmDialog } from "@/components/ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  EmptyState,
  ErrorState,
  Field,
  LoadingRows,
  Skeleton,
  StatusBadge,
} from "@/components/common";

const DocumentDetail = lazy(() =>
  import("@/components/document-detail").then((m) => ({
    default: m.DocumentDetail,
  })),
);
const ArtifactResult = lazy(() =>
  import("@/components/artifact-result").then((m) => ({
    default: m.ArtifactResult,
  })),
);

function DetailLoading({ onClose }: { onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent sheet>
        <DialogTitle>Loading document</DialogTitle>
        <DialogDescription>Retrieving document details.</DialogDescription>
        <LoadingRows />
      </DialogContent>
    </Dialog>
  );
}

export function Library({
  documents,
  loading,
  error,
  refresh,
  onChat,
  transfer,
}: {
  documents: VaultDocument[];
  loading: boolean;
  error: unknown;
  refresh: () => void;
  onChat: (ids: string[]) => void;
  transfer: ReturnType<typeof useDocumentUploads>;
}) {
  const client = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState("newest");
  const [view, setView] = useState<"list" | "grid">("list");
  const [columns, setColumns] = useState(() => {
    try {
      return parseFileColumns(localStorage.getItem(fileColumnsStorageKey));
    } catch {
      return parseFileColumns(null);
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(fileColumnsStorageKey, JSON.stringify(columns));
    } catch {
      /* Keep the controls usable when browser storage is unavailable. */
    }
  }, [columns]);
  const visibleColumns = fileColumns.filter((column) =>
    columns.includes(column.id),
  );
  const [selected, setSelected] = useState<string[]>([]);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<VaultDocument | null>(null);
  const [dragging, setDragging] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [dimensions, setDimensions] = useState(
    "Key themes, Similarities, Differences",
  );
  const [comparisonId, setComparisonId] = useState<string | null>(null);
  const { uploads, uploading, upload } = transfer;
  const pendingUploads = uploads.filter((row) => !row.error).length;
  const failedUploads = uploads.length - pendingUploads;
  const processing = documents.filter(
    (doc) => !["ready", "failed"].includes(doc.status),
  ).length;
  const filtered = documents
    .filter(
      (doc) =>
        `${doc.title} ${doc.filename} ${doc.processing?.run_id || ""} ${doc.tags.join(" ")}`
          .toLowerCase()
          .includes(query.toLowerCase()) &&
        (filter === "all" ||
          (filter === "processing"
            ? !["ready", "failed"].includes(doc.status)
            : doc.status === filter)),
    )
    .sort((a, b) =>
      sort === "name"
        ? a.title.localeCompare(b.title)
        : sort === "oldest"
          ? a.created_at.localeCompare(b.created_at)
          : b.created_at.localeCompare(a.created_at),
    );
  const selectedVersions = documents.flatMap((doc) =>
    selected.includes(doc.id) && doc.current_version_id
      ? [doc.current_version_id]
      : [],
  );
  const readyVisible = filtered.filter((doc) => !!doc.current_version_id);
  function startUpload(files: FileList | File[]) {
    if (!files.length || uploading) return;
    setFilter("all");
    setQuery("");
    setSort("newest");
    setView("list");
    void upload(files);
  }
  const remove = useMutation({
    mutationFn: (id: string) =>
      api(`/v1/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setSelected((ids) => ids.filter((id) => id !== deleting?.id));
      setDeleting(null);
      refresh();
      void client.invalidateQueries({ queryKey: ["metrics"] });
    },
  });
  const compare = useMutation({
    mutationFn: () =>
      api<{ id: string }>("/v1/comparisons", {
        method: "POST",
        body: JSON.stringify({
          version_ids: selectedVersions,
          dimensions: dimensions
            .split(",")
            .map((v) => v.trim())
            .filter(Boolean),
        }),
      }),
    onSuccess: (result) => {
      setComparisonId(result.id);
      void client.invalidateQueries({ queryKey: ["artifact", result.id] });
    },
  });
  return (
    <div
      className="relative flex min-h-full min-w-0 flex-col overflow-x-clip animate-enter"
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) {
          event.preventDefault();
          setDragging(true);
        }
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node))
          setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        startUpload(event.dataTransfer.files);
      }}
    >
      {dragging && (
        <div className="pointer-events-none absolute inset-3 z-40 grid place-items-center rounded-lg border-2 border-dashed border-primary bg-card/95">
          <div className="text-center">
            <Upload className="mx-auto mb-3 size-7" />
            <h2 className="text-base font-medium">Drop documents to upload</h2>
            <p className="mt-2 text-xs text-muted-foreground">
              PDF, DOCX or TXT · Up to 10 files
            </p>
          </div>
        </div>
      )}
      <header className="flex flex-wrap items-center justify-between gap-3 px-5 pb-4 pt-6 sm:px-7">
        <div className="flex items-center gap-2.5">
          <h1 className="text-lg font-semibold tracking-tight">Files</h1>
          {!loading && !error && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground">
              {documents.length}
            </span>
          )}
        </div>
        <div className="flex items-center gap-4">
          {(processing > 0 || uploading) && (
            <span
              role="status"
              className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex"
            >
              <LoaderCircle className="size-3 animate-spin" />
              {uploading ? "Uploading" : `${processing} processing`}
            </span>
          )}
          <Button
            size="sm"
            onClick={() => input.current?.click()}
            disabled={uploading}
          >
            <Upload />
            Upload files
          </Button>
        </div>
      </header>
      <input
        type="file"
        ref={input}
        className="hidden"
        aria-label="Upload documents"
        multiple
        accept=".pdf,.docx,.txt"
        onChange={(event) => {
          if (event.target.files) startUpload(event.target.files);
          event.target.value = "";
        }}
      />
      <div className="flex flex-wrap items-center gap-2 border-b px-5 pb-4 sm:px-7">
        <div className="relative w-full sm:w-72">
          <Search className="absolute left-2.5 top-2 size-3.5 text-muted-foreground" />
          <Input
            className="h-8 bg-card pl-8 text-xs shadow-none"
            placeholder="Search files or run ID…"
            aria-label="Search library"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger
            aria-label="Filter by status"
            className="h-8 w-[150px] text-xs"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="ready">Ready</SelectItem>
            <SelectItem value="processing">Processing</SelectItem>
            <SelectItem value="failed">Needs attention</SelectItem>
          </SelectContent>
        </Select>
        <div className="ml-auto flex items-center gap-2">
          <FileColumnsMenu value={columns} onChange={setColumns} />
          <Select value={sort} onValueChange={setSort}>
            <SelectTrigger
              aria-label="Sort files"
              className="h-8 w-[130px] text-xs"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="newest">Newest first</SelectItem>
              <SelectItem value="oldest">Oldest first</SelectItem>
              <SelectItem value="name">Name A–Z</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex items-center rounded-md border p-0.5">
            <Button
              aria-label="List view"
              aria-pressed={view === "list"}
              variant={view === "list" ? "secondary" : "ghost"}
              size="icon"
              className="size-6 rounded-sm"
              onClick={() => setView("list")}
            >
              <List />
            </Button>
            <Button
              aria-label="Grid view"
              aria-pressed={view === "grid"}
              variant={view === "grid" ? "secondary" : "ghost"}
              size="icon"
              className="size-6 rounded-sm"
              onClick={() => setView("grid")}
            >
              <LayoutGrid />
            </Button>
          </div>
        </div>
      </div>
      {error ? (
        <div className="m-5">
          <ErrorState error={error} retry={refresh} />
        </div>
      ) : null}
      {selectedVersions.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b bg-accent/50 px-5 py-2 sm:px-7">
          <span className="mr-auto text-xs font-medium">
            {selectedVersions.length} selected
          </span>
          <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
            Clear
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={selectedVersions.length < 2}
            onClick={() => {
              setComparisonId(null);
              setComparing(true);
            }}
          >
            <GitCompareArrows />
            Compare
          </Button>
          <Button size="sm" onClick={() => onChat(selectedVersions)}>
            <MessageSquare />
            Start a chat
          </Button>
        </div>
      )}
      {view === "list" || uploads.length > 0 || loading ? (
        <div
          className="overflow-x-auto outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/30"
          role="region"
          aria-label="Files table"
          tabIndex={0}
        >
          <table
            className="w-full table-fixed text-left text-[13px]"
            style={{
              minWidth:
                540 +
                visibleColumns.reduce(
                  (width, column) => width + column.width,
                  0,
                ),
            }}
            aria-label="Document files"
            aria-busy={loading}
          >
            <colgroup>
              <col className="w-11" />
              <col className="w-[300px]" />
              <col className="w-[152px]" />
              {visibleColumns.map((column) => (
                <col key={column.id} style={{ width: column.width }} />
              ))}
              <col className="w-11" />
            </colgroup>
            <thead className="border-b bg-muted/25 text-[11px] text-muted-foreground">
              <tr>
                <th className="sm:sticky left-0 z-20 bg-background py-2.5 pl-5 sm:pl-7">
                  <Checkbox
                    aria-label="Select all visible ready documents"
                    disabled={!readyVisible.length}
                    checked={
                      readyVisible.length > 0 &&
                      readyVisible.every((doc) => selected.includes(doc.id))
                    }
                    onCheckedChange={(checked) =>
                      setSelected((ids) =>
                        checked
                          ? [
                              ...new Set([
                                ...ids,
                                ...readyVisible.map((doc) => doc.id),
                              ]),
                            ]
                          : ids.filter(
                              (id) =>
                                !readyVisible.some((doc) => doc.id === id),
                            ),
                      )
                    }
                  />
                </th>
                <th className="sm:sticky left-11 z-20 bg-background px-3 py-2.5 font-medium">
                  Name
                </th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                {visibleColumns.map((column) => (
                  <th
                    key={column.id}
                    className="px-3 py-2.5 font-medium"
                    title={column.description}
                  >
                    {column.label}
                  </th>
                ))}
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {uploads.map((row) => (
                <tr key={row.id} className="animate-enter bg-muted/15">
                  <td className="sm:sticky left-0 z-10 bg-card pl-5 sm:pl-7">
                    <Checkbox
                      disabled
                      aria-label={`Uploading ${row.file.name}`}
                    />
                  </td>
                  <td className="sm:sticky left-11 z-10 bg-card px-3 py-3">
                    <div className="flex items-center gap-3">
                      <FileText className="size-4 shrink-0 text-muted-foreground" />
                      <div className="min-w-0 flex-1">
                        <p
                          className="truncate font-medium"
                          title={row.file.name}
                        >
                          {row.file.name}
                        </p>
                        {row.error ? (
                          <p
                            role="alert"
                            className="mt-1 break-words text-xs text-destructive"
                          >
                            {row.error}
                          </p>
                        ) : (
                          <div
                            role="progressbar"
                            aria-label={`Uploading ${row.file.name}`}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={row.progress}
                            className="mt-2 h-1 max-w-44 overflow-hidden rounded bg-muted"
                          >
                            <div
                              className="h-full origin-left bg-primary transition-transform duration-300"
                              style={{
                                transform: `scaleX(${row.progress / 100})`,
                              }}
                            />
                          </div>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    {row.error ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-xs text-destructive">
                          Upload failed
                        </span>
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-6 px-1 text-xs"
                          disabled={uploading}
                          onClick={() => transfer.retry(row)}
                        >
                          <RefreshCw className="size-3" />
                          Retry
                        </Button>
                      </div>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                        <LoaderCircle className="size-3 animate-spin" />
                        {row.progress === 100
                          ? "Saving…"
                          : `Uploading ${row.progress}%`}
                      </span>
                    )}
                  </td>
                  {visibleColumns.map((column) => (
                    <td
                      key={column.id}
                      className="px-3 text-xs tabular-nums text-muted-foreground"
                    >
                      {column.id === "size" ? bytes(row.file.size) : "—"}
                    </td>
                  ))}
                  <td>
                    {row.error && (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7"
                        aria-label={`Dismiss failed upload ${row.file.name}`}
                        onClick={() => transfer.dismiss(row.id)}
                      >
                        <X />
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
              {loading && !documents.length
                ? Array.from({ length: 6 }, (_, index) => (
                    <tr key={index} aria-hidden="true">
                      <td className="sm:sticky left-0 z-10 bg-card py-4 pl-5 sm:pl-7">
                        <Skeleton className="size-3.5" />
                      </td>
                      <td className="sm:sticky left-11 z-10 bg-card px-3 py-4">
                        <Skeleton
                          className={cn("h-4", index % 2 ? "w-3/5" : "w-4/5")}
                        />
                      </td>
                      <td className="px-3">
                        <Skeleton className="h-5 w-20" />
                      </td>
                      {visibleColumns.map((column) => (
                        <td key={column.id} className="px-3">
                          <Skeleton className="h-3 w-3/5" />
                        </td>
                      ))}
                      <td />
                    </tr>
                  ))
                : filtered.map((doc) => (
                    <tr
                      key={doc.id}
                      className={cn(
                        "group transition-colors hover:bg-muted/35",
                        selected.includes(doc.id) && "bg-accent/60",
                      )}
                    >
                      <td
                        className={cn(
                          "sm:sticky left-0 z-10 py-3 pl-5 group-hover:bg-background sm:pl-7",
                          selected.includes(doc.id) ? "bg-accent" : "bg-card",
                        )}
                      >
                        <Checkbox
                          aria-label={`Select ${doc.title}`}
                          disabled={!doc.current_version_id}
                          checked={selected.includes(doc.id)}
                          onCheckedChange={(checked) =>
                            setSelected((ids) =>
                              checked
                                ? [...ids, doc.id]
                                : ids.filter((id) => id !== doc.id),
                            )
                          }
                        />
                      </td>
                      <td
                        className={cn(
                          "sm:sticky left-11 z-10 px-3 py-3 group-hover:bg-background",
                          selected.includes(doc.id) ? "bg-accent" : "bg-card",
                        )}
                      >
                        <Button
                          variant="ghost"
                          className="h-auto w-full min-w-0 justify-start gap-3 p-0 text-left text-[13px] hover:bg-transparent"
                          onClick={() => setDetailId(doc.id)}
                          title={doc.title}
                        >
                          <FileText className="size-4 shrink-0 text-muted-foreground" />
                          <span className="min-w-0">
                            <span className="block truncate font-medium">
                              {doc.title}
                            </span>
                            <span className="mt-0.5 block text-[11px] font-normal text-muted-foreground">
                              {doc.filename.split(".").pop()?.toUpperCase()} · v
                              {doc.version_number}
                            </span>
                          </span>
                        </Button>
                      </td>
                      <td className="px-3 py-3">
                        <StatusBadge status={doc.status} />
                      </td>
                      {visibleColumns.map((column) => (
                        <td
                          key={column.id}
                          className="whitespace-nowrap px-3 text-xs tabular-nums text-muted-foreground"
                        >
                          <DocumentColumnValue
                            column={column.id}
                            document={doc}
                          />
                        </td>
                      ))}
                      <td className="pr-3">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-muted-foreground hover:text-destructive"
                          aria-label={`Delete ${doc.title}`}
                          onClick={() => {
                            remove.reset();
                            setDeleting(doc);
                          }}
                        >
                          <Trash2 className="size-3.5" />
                        </Button>
                      </td>
                    </tr>
                  ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="grid gap-3 p-5 sm:grid-cols-2 sm:px-7 xl:grid-cols-3">
          {filtered.map((doc) => (
            <div
              key={doc.id}
              className="rounded-lg border p-4 transition-colors hover:bg-muted/20"
            >
              <div className="mb-4 flex justify-between">
                <FileText className="size-5 text-muted-foreground" />
                <Checkbox
                  aria-label={`Select ${doc.title}`}
                  disabled={!doc.current_version_id}
                  checked={selected.includes(doc.id)}
                  onCheckedChange={(checked) =>
                    setSelected((ids) =>
                      checked
                        ? [...ids, doc.id]
                        : ids.filter((id) => id !== doc.id),
                    )
                  }
                />
              </div>
              <h3 className="truncate text-sm font-medium" title={doc.title}>
                {doc.title}
              </h3>
              <p className="mb-4 mt-1 text-xs text-muted-foreground">
                {bytes(doc.size_bytes)} · v{doc.version_number}
              </p>
              <div className="flex items-center justify-between">
                <StatusBadge status={doc.status} />
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7"
                  onClick={() => setDetailId(doc.id)}
                  aria-label={`View ${doc.title}`}
                >
                  <ArrowRight />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
      {!loading && !error && !filtered.length && !uploads.length && (
        <EmptyState
          title={
            documents.length ? "No matching files" : "Your files, in one place"
          }
          description={
            documents.length
              ? "Try another search or status filter."
              : "Drop a PDF, DOCX or TXT file here to get started."
          }
        >
          {documents.length ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setQuery("");
                setFilter("all");
              }}
            >
              Clear filters
            </Button>
          ) : (
            <Button size="sm" onClick={() => input.current?.click()}>
              <Upload />
              Upload files
            </Button>
          )}
        </EmptyState>
      )}
      <footer className="flex flex-wrap items-center justify-between gap-2 border-t px-5 py-3 text-[11px] text-muted-foreground sm:px-7">
        <span role="status">
          {loading
            ? "Loading files…"
            : error
              ? "Could not refresh files"
              : `${filtered.length} ${filtered.length === 1 ? "file" : "files"}${filtered.length !== documents.length ? ` of ${documents.length}` : ""}`}
          {pendingUploads > 0 ? ` · ${pendingUploads} uploading` : ""}
          {failedUploads > 0
            ? ` · ${failedUploads} ${failedUploads === 1 ? "upload needs attention" : "uploads need attention"}`
            : ""}
        </span>
        <span>
          Latest version · Times in{" "}
          {Intl.DateTimeFormat().resolvedOptions().timeZone}
        </span>
      </footer>
      {detailId && (
        <Suspense
          fallback={<DetailLoading onClose={() => setDetailId(null)} />}
        >
          <DocumentDetail
            document={documents.find((d) => d.id === detailId) || null}
            onClose={() => setDetailId(null)}
            onChat={onChat}
          />
        </Suspense>
      )}
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Delete this document?"
        description={`“${deleting?.title || ""}” and its versions will be removed from your library. This cannot be undone.`}
        pending={remove.isPending}
        onConfirm={() => deleting && remove.mutate(deleting.id)}
      />
      {remove.error && (
        <div className="fixed bottom-5 right-5 z-[60] max-w-sm">
          <ErrorState error={remove.error} />
        </div>
      )}
      <Dialog open={comparing} onOpenChange={setComparing}>
        <DialogContent className="max-w-2xl">
          <DialogTitle>Compare documents</DialogTitle>
          <DialogDescription>
            Compare {selectedVersions.length} documents, with evidence from each
            source.
          </DialogDescription>
          {comparisonId ? (
            <div className="mt-6">
              <Suspense fallback={<LoadingRows />}>
                <ArtifactResult id={comparisonId} documents={documents} />
              </Suspense>
              <Button
                className="mt-5"
                variant="outline"
                onClick={() => setComparisonId(null)}
              >
                Adjust or retry comparison
              </Button>
            </div>
          ) : (
            <>
              <div className="my-6">
                <Field label="What should we compare?">
                  <Input
                    value={dimensions}
                    onChange={(e) => setDimensions(e.target.value)}
                    placeholder="e.g. pricing, scope, risks"
                  />
                  <span className="font-normal text-muted-foreground">
                    Add one to five dimensions, separated by commas.
                  </span>
                </Field>
              </div>
              {compare.error && <ErrorState error={compare.error} />}
              <Button
                className="mt-4"
                disabled={
                  compare.isPending ||
                  selectedVersions.length < 2 ||
                  !dimensions.trim() ||
                  dimensions.split(",").filter((v) => v.trim()).length > 5
                }
                onClick={() => compare.mutate()}
              >
                <GitCompareArrows />
                {compare.isPending ? "Starting…" : "Compare documents"}
              </Button>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
