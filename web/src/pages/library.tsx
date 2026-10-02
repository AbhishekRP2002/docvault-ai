import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  FileText,
  Files,
  GitCompareArrows,
  LayoutGrid,
  List,
  LoaderCircle,
  MessageSquare,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { api, apiUrl } from "@/lib/api";
import type { VaultDocument } from "@/lib/types";
import { bytes, cn, date, errorMessage } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { NativeSelect } from "@/components/ui/select";
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
  PageHeading,
  StatusBadge,
} from "@/components/common";
import { DocumentDetail } from "@/components/document-detail";
import { ArtifactResult } from "@/components/artifact-result";
interface UploadState {
  name: string;
  progress: number;
  error?: string;
  done?: boolean;
}
export function Library({
  documents,
  loading,
  error,
  refresh,
  onChat,
}: {
  documents: VaultDocument[];
  loading: boolean;
  error: unknown;
  refresh: () => void;
  onChat: (ids: string[]) => void;
}) {
  const client = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [view, setView] = useState<"list" | "grid">("list");
  const [selected, setSelected] = useState<string[]>([]);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<VaultDocument | null>(null);
  const [uploads, setUploads] = useState<UploadState[]>([]);
  const [dragging, setDragging] = useState(false);
  const [comparing, setComparing] = useState(false);
  const [dimensions, setDimensions] = useState(
    "Key themes, Similarities, Differences",
  );
  const [comparisonId, setComparisonId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const filtered = documents.filter(
    (d) =>
      `${d.title} ${d.filename} ${d.tags.join(" ")}`
        .toLowerCase()
        .includes(query.toLowerCase()) &&
      (filter === "all" ||
        (filter === "processing"
          ? !["ready", "failed"].includes(d.status)
          : d.status === filter)),
  );
  const selectedVersions = documents.flatMap((d) =>
    selected.includes(d.id) && d.status === "ready" && d.current_version_id
      ? [d.current_version_id]
      : [],
  );
  const remove = useMutation({
    mutationFn: (id: string) =>
      api(`/v1/documents/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setSelected((ids) => ids.filter((id) => id !== deleting?.id));
      setDeleting(null);
      refresh();
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
  function upload(files: FileList | File[]) {
    if (!files.length || uploading) return;
    const list = Array.from(files);
    setUploading(true);
    setUploads(list.map((file) => ({ name: file.name, progress: 0 })));
    const form = new FormData();
    list.forEach((file) => form.append("files", file));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl("/v1/document-batches"));
    xhr.setRequestHeader("Idempotency-Key", crypto.randomUUID());
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        setUploads((items) =>
          items.map((item) => ({
            ...item,
            progress: Math.round((event.loaded / event.total) * 100),
          })),
        );
    };
    xhr.onerror = () => {
      setUploading(false);
      setUploads((items) =>
        items.map((item) => ({
          ...item,
          error:
            "Upload connection failed. Please select the files and try again.",
        })),
      );
    };
    xhr.onload = () => {
      setUploading(false);
      try {
        const result = JSON.parse(xhr.responseText);
        if (xhr.status >= 400)
          throw new Error(result.error?.message || "Upload failed.");
        setUploads(
          result.items.map((item: { filename: string; error?: string }) => ({
            name: item.filename,
            progress: 100,
            done: !item.error,
            error: item.error,
          })),
        );
        refresh();
        void client.invalidateQueries({ queryKey: ["metrics"] });
      } catch (err) {
        setUploads((items) =>
          items.map((item) => ({ ...item, error: errorMessage(err) })),
        );
      }
    };
    xhr.send(form);
  }
  return (
    <div
      className="relative mx-auto w-full max-w-7xl p-5 sm:p-8 lg:p-10 animate-enter"
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node))
          setDragging(false);
      }}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        upload(e.dataTransfer.files);
      }}
    >
      {dragging && (
        <div className="pointer-events-none absolute inset-3 z-40 grid place-items-center rounded-xl border-2 border-dashed border-primary bg-card/95">
          <div className="text-center">
            <Upload className="mx-auto mb-4 size-9 text-primary" />
            <h2 className="text-xl font-semibold">Drop your documents here</h2>
            <p className="mt-2 text-muted-foreground">
              PDF, DOCX, and TXT files are supported.
            </p>
          </div>
        </div>
      )}
      <PageHeading
        title="Files"
        description="Upload, organize, and work with your documents."
      >
        <Button onClick={() => input.current?.click()} disabled={uploading}>
          <Upload />
          Upload documents
        </Button>
      </PageHeading>
      <input
        type="file"
        ref={input}
        className="hidden"
        multiple
        accept=".pdf,.docx,.txt"
        onChange={(e) => {
          if (e.target.files) upload(e.target.files);
          e.target.value = "";
        }}
      />
      <div className="mb-7 grid grid-cols-3 gap-3 sm:gap-5">
        {[
          { label: "Total documents", value: documents.length, icon: Files },
          {
            label: "Ready to explore",
            value: documents.filter((d) => d.status === "ready").length,
            icon: FileText,
          },
          {
            label: "Processing",
            value: documents.filter(
              (d) => !["ready", "failed"].includes(d.status),
            ).length,
            icon: LoaderCircle,
          },
        ].map(({ label, value, icon: Icon }) => (
          <div
            key={label}
            className="rounded-xl border bg-card px-4 py-4 sm:px-5"
          >
            <div className="mb-3 flex items-center justify-between">
              <span className="text-xs text-muted-foreground">{label}</span>
              <Icon className="hidden size-4 text-muted-foreground/70 sm:block" />
            </div>
            <p className="text-2xl font-semibold tracking-tight tabular-nums">
              {loading || error ? "—" : value}
            </p>
          </div>
        ))}
      </div>
      {uploads.length > 0 && (
        <div className="mb-6 rounded-xl border bg-card p-4">
          <div className="mb-3 flex justify-between">
            <h3 className="text-sm font-medium">
              {uploading ? "Uploading documents" : "Upload results"}
            </h3>
            {!uploading && (
              <Button
                size="icon"
                variant="ghost"
                className="size-6"
                aria-label="Dismiss upload results"
                onClick={() => setUploads([])}
              >
                <X />
              </Button>
            )}
          </div>
          <div className="space-y-3">
            {uploads.map((item, i) => (
              <div key={`${item.name}-${i}`}>
                <div className="flex items-center justify-between gap-3 text-xs">
                  <span className="truncate">{item.name}</span>
                  <span
                    className={cn(
                      "shrink-0 text-muted-foreground",
                      item.error && "text-destructive",
                    )}
                  >
                    {item.error
                      ? "Upload failed"
                      : item.done
                        ? "Uploaded · processing next"
                        : item.progress === 100
                          ? "Saving…"
                          : `${item.progress}%`}
                  </span>
                </div>
                {item.error ? (
                  <p role="alert" className="mt-1 text-xs text-destructive">
                    {item.error}
                  </p>
                ) : (
                  !item.done && (
                    <div className="mt-2 h-1 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full bg-primary transition-[width]"
                        style={{ width: `${item.progress}%` }}
                      />
                    </div>
                  )
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {error ? (
        <ErrorState error={error} retry={refresh} />
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="relative min-w-48 flex-1 sm:max-w-xs">
              <Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
              <Input
                className="bg-card pl-9"
                placeholder="Search documents…"
                aria-label="Search library"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <div className="flex items-center gap-2">
              <NativeSelect
                aria-label="Filter by status"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              >
                <option value="all">All statuses</option>
                <option value="ready">Ready</option>
                <option value="processing">Processing</option>
                <option value="failed">Needs attention</option>
              </NativeSelect>
              <div className="flex rounded-md border bg-card p-0.5">
                <Button
                  aria-label="List view"
                  aria-pressed={view === "list"}
                  variant={view === "list" ? "secondary" : "ghost"}
                  size="icon"
                  className="size-7"
                  onClick={() => setView("list")}
                >
                  <List />
                </Button>
                <Button
                  aria-label="Grid view"
                  aria-pressed={view === "grid"}
                  variant={view === "grid" ? "secondary" : "ghost"}
                  size="icon"
                  className="size-7"
                  onClick={() => setView("grid")}
                >
                  <LayoutGrid />
                </Button>
              </div>
            </div>
          </div>
          {selectedVersions.length > 0 && (
            <div className="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-primary/15 bg-primary/5 px-4 py-2">
              <span className="mr-auto text-xs font-medium text-primary">
                {selectedVersions.length} documents selected
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
          {loading ? (
            <div className="rounded-xl border bg-card">
              <LoadingRows />
            </div>
          ) : !filtered.length ? (
            <div className="rounded-xl border bg-card">
              <EmptyState
                title={
                  documents.length
                    ? "No matching documents"
                    : "Make room for a little clarity"
                }
                description={
                  documents.length
                    ? "Try a different search or status filter."
                    : "Upload your first PDF, Word document, or text file. Turn your documents into answers, insights, and ideas."
                }
              >
                {!documents.length && (
                  <Button onClick={() => input.current?.click()}>
                    <Upload />
                    Upload your first document
                  </Button>
                )}
              </EmptyState>
              {!documents.length && (
                <p className="-mt-8 mb-9 text-center text-xs text-muted-foreground">
                  PDF, DOCX & TXT · Up to 10 files per batch · Drag and drop
                </p>
              )}
            </div>
          ) : view === "list" ? (
            <div className="overflow-x-auto rounded-xl border bg-card">
              <table className="w-full text-left text-sm">
                <thead className="border-b bg-muted/35 text-[11px] font-medium text-muted-foreground">
                  <tr>
                    <th className="w-10 py-3 pl-5">
                      <Checkbox
                        aria-label="Select all visible ready documents"
                        checked={
                          filtered.filter((d) => d.status === "ready").length >
                            0 &&
                          filtered
                            .filter((d) => d.status === "ready")
                            .every((d) => selected.includes(d.id))
                        }
                        onCheckedChange={(checked) =>
                          setSelected(
                            checked
                              ? filtered
                                  .filter((d) => d.status === "ready")
                                  .map((d) => d.id)
                              : [],
                          )
                        }
                      />
                    </th>
                    <th className="px-4 py-3 font-medium">Name</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="hidden px-4 py-3 font-medium md:table-cell">
                      Size
                    </th>
                    <th className="hidden px-4 py-3 font-medium lg:table-cell">
                      Added
                    </th>
                    <th className="w-12">
                      <span className="sr-only">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {filtered.map((doc) => (
                    <tr
                      key={doc.id}
                      className="group transition-colors hover:bg-muted/25"
                    >
                      <td className="py-5 pl-5">
                        <Checkbox
                          aria-label={`Select ${doc.title}`}
                          disabled={doc.status !== "ready"}
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
                      <td className="max-w-xs px-4 py-4">
                        <Button
                          variant="ghost"
                          className="h-auto max-w-full justify-start gap-3 p-0 text-left hover:bg-transparent"
                          onClick={() => setDetailId(doc.id)}
                        >
                          <span className="grid size-10 shrink-0 place-items-center rounded-lg border bg-background text-primary/80">
                            <FileText className="size-5" />
                          </span>
                          <span className="min-w-0">
                            <span className="block truncate font-medium">
                              {doc.title}
                            </span>
                            <span className="mt-1 block text-[11px] font-normal text-muted-foreground">
                              {doc.filename.split(".").pop()?.toUpperCase()} ·
                              Version {doc.version_number}
                              {doc.page_count
                                ? ` · ${doc.page_count} pages`
                                : ""}
                            </span>
                          </span>
                        </Button>
                      </td>
                      <td className="px-4 py-4">
                        <StatusBadge status={doc.status} />
                      </td>
                      <td className="hidden px-4 py-4 text-xs text-muted-foreground md:table-cell">
                        {bytes(doc.size_bytes)}
                      </td>
                      <td className="hidden whitespace-nowrap px-4 py-4 text-xs text-muted-foreground lg:table-cell">
                        {date(doc.created_at)}
                      </td>
                      <td className="pr-3">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="text-muted-foreground hover:text-destructive"
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
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {filtered.map((doc) => (
                <div
                  key={doc.id}
                  className="rounded-xl border bg-card p-5 transition-shadow hover:shadow-md"
                >
                  <div className="mb-6 flex justify-between">
                    <FileText className="size-7 text-primary/75" />
                    <Checkbox
                      aria-label={`Select ${doc.title}`}
                      disabled={doc.status !== "ready"}
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
                  <h3 className="truncate font-medium">{doc.title}</h3>
                  <p className="mb-5 mt-2 text-xs text-muted-foreground">
                    {bytes(doc.size_bytes)} · Version {doc.version_number}
                  </p>
                  <div className="flex items-center justify-between">
                    <StatusBadge status={doc.status} />
                    <Button
                      variant="ghost"
                      size="icon"
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
          <p className="mt-4 text-xs text-muted-foreground">
            {filtered.length} {filtered.length === 1 ? "document" : "documents"}
            {documents.length > filtered.length
              ? ` of ${documents.length}`
              : ""}
          </p>
        </>
      )}
      <DocumentDetail
        document={documents.find((d) => d.id === detailId) || null}
        onClose={() => setDetailId(null)}
        onChat={onChat}
      />
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
          <DialogTitle>See the bigger picture</DialogTitle>
          <DialogDescription>
            Compare {selectedVersions.length} documents, with evidence from each
            source.
          </DialogDescription>
          {comparisonId ? (
            <div className="mt-6">
              <ArtifactResult id={comparisonId} documents={documents} />
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
