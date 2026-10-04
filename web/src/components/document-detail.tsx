import { Markdown } from "./markdown";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  FileText,
  History,
  LoaderCircle,
  RefreshCw,
  Sparkles,
  Upload,
} from "lucide-react";
import { api, apiUrl } from "@/lib/api";
import type { DocumentVersion, Insights, VaultDocument } from "@/lib/types";
import { bytes, date, errorMessage } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";
import { ArtifactResult } from "./artifact-result";
import { EvidenceLinks } from "./evidence-links";
import { ErrorState, Field, Skeleton, StatusBadge } from "./common";
export function DocumentDetail({
  document: doc,
  onClose,
  onChat,
}: {
  document: VaultDocument | null;
  onClose: () => void;
  onChat: (ids: string[]) => void;
}) {
  return (
    <Dialog open={!!doc} onOpenChange={(open) => !open && onClose()}>
      <DialogContent sheet className="p-0">
        {doc && <Detail key={doc.id} doc={doc} onChat={onChat} />}
      </DialogContent>
    </Dialog>
  );
}
function Detail({
  doc,
  onChat,
}: {
  doc: VaultDocument;
  onChat: (ids: string[]) => void;
}) {
  const client = useQueryClient();
  const uploadInput = useRef<HTMLInputElement>(null);
  const [versionId, setVersionId] = useState(
    doc.current_version_id || doc.latest_version_id,
  );
  const [summaryId, setSummaryId] = useState<string | null>(null);
  const [length, setLength] = useState("medium");
  const [tone, setTone] = useState("neutral");
  const [focus, setFocus] = useState("");
  const versions = useQuery({
    queryKey: ["versions", doc.id],
    queryFn: () =>
      api<{ items: DocumentVersion[] }>(`/v1/documents/${doc.id}/versions`),
    refetchInterval: 3000,
  });
  const insights = useQuery({
    queryKey: ["insights", versionId],
    queryFn: () =>
      api<{ status: string; data: Insights | null; error: string | null }>(
        `/v1/versions/${versionId}/insights`,
      ),
    enabled: !!versionId,
    refetchInterval: (q) =>
      ["complete", "ready", "failed"].includes(q.state.data?.status || "")
        ? false
        : 3000,
  });
  const newVersion = useMutation({
    mutationFn: (file: File) => {
      const body = new FormData();
      body.append("file", file);
      return api<VaultDocument>(`/v1/documents/${doc.id}/versions`, {
        method: "POST",
        body,
      });
    },
    onSuccess: (data) => {
      setVersionId(data.latest_version_id);
      void client.invalidateQueries({ queryKey: ["documents"] });
      void client.invalidateQueries({ queryKey: ["versions", doc.id] });
    },
  });
  const retry = useMutation({
    mutationFn: (id: string) =>
      api(`/v1/versions/${id}/retry`, { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["documents"] });
      void versions.refetch();
      void client.invalidateQueries({ queryKey: ["insights", versionId] });
    },
  });
  const summary = useMutation({
    mutationFn: () =>
      api<{ id: string }>(`/v1/versions/${versionId}/summaries`, {
        method: "POST",
        body: JSON.stringify({
          length,
          tone,
          focus_areas: focus
            .split(",")
            .map((v) => v.trim())
            .filter(Boolean),
        }),
      }),
    onSuccess: (data) => {
      setSummaryId(data.id);
      void client.invalidateQueries({ queryKey: ["artifact", data.id] });
    },
  });
  const selectedVersion = versions.data?.items.find((v) => v.id === versionId);
  const isReady = selectedVersion
    ? selectedVersion.status === "ready"
    : doc.status === "ready";
  return (
    <>
      <div className="border-b p-6 pr-12">
        <div className="mb-4 grid size-12 place-items-center rounded-xl border bg-muted text-primary">
          <FileText className="size-6" />
        </div>
        <DialogTitle>{doc.title}</DialogTitle>
        <DialogDescription>
          {bytes(doc.size_bytes)} ·{" "}
          {doc.page_count ? `${doc.page_count} pages · ` : ""}Added{" "}
          {date(doc.created_at)}
        </DialogDescription>
        <div className="mt-4 flex items-center gap-3">
          <StatusBadge status={doc.status} />
          {doc.category && (
            <span className="text-xs text-muted-foreground">
              {doc.category}
            </span>
          )}
        </div>
      </div>
      <div className="p-6">
        <div className="mb-5 flex gap-2">
          <Button
            className="flex-1"
            disabled={!doc.current_version_id}
            onClick={() =>
              doc.current_version_id && onChat([doc.current_version_id])
            }
          >
            <Sparkles />
            Chat with document
          </Button>
          <Button asChild variant="outline" size="icon">
            <a
              href={apiUrl(`/v1/versions/${versionId}/content`)}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Download original document"
            >
              <Download />
            </a>
          </Button>
        </div>
        <Tabs defaultValue="overview">
          <TabsList className="w-full">
            <TabsTrigger className="flex-1" value="overview">
              Overview
            </TabsTrigger>
            <TabsTrigger className="flex-1" value="summary">
              Custom summary
            </TabsTrigger>
            <TabsTrigger className="flex-1" value="versions">
              Versions
            </TabsTrigger>
          </TabsList>
          <TabsContent value="overview">
            {versions.data && versions.data.items.length > 1 && (
              <Field label="Viewing version">
                <Select
                  value={versionId}
                  onValueChange={(value) => {
                    setVersionId(value);
                    setSummaryId(null);
                  }}
                >
                  <SelectTrigger className="mb-5" aria-label="Viewing version">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {versions.data.items.map((v) => (
                      <SelectItem key={v.id} value={v.id}>
                        Version {v.version_number} · {v.status}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            )}
            {doc.error && <ErrorState error={doc.error} />}
            {insights.error && (
              <ErrorState
                error={insights.error}
                retry={() => void insights.refetch()}
              />
            )}
            {insights.isPending ? (
              <div
                className="space-y-5"
                role="status"
                aria-label="Loading document insights"
              >
                <Skeleton className="h-3 w-24" />
                <div className="space-y-2">
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-full" />
                  <Skeleton className="h-3 w-3/4" />
                </div>
                <Skeleton className="h-3 w-28" />
                <Skeleton className="h-12 w-full" />
              </div>
            ) : insights.data?.data ? (
              <div className="space-y-7 animate-enter">
                <section>
                  <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    At a glance
                  </h3>
                  <Markdown>{insights.data.data.summary}</Markdown>
                </section>
                {!!insights.data.data.key_insights?.length && (
                  <section>
                    <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                      Key insights
                    </h3>
                    <ul className="space-y-4">
                      {insights.data.data.key_insights.map((insight, index) => (
                        <li key={index} className="flex gap-3">
                          <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-primary/8 text-[10px] font-semibold text-primary">
                            {index + 1}
                          </span>
                          <div>
                            <p className="text-sm leading-6">{insight.text}</p>
                            <EvidenceLinks
                              ids={insight.citation_ids}
                              versionId={versionId}
                            />
                          </div>
                        </li>
                      ))}
                    </ul>
                  </section>
                )}
                <div className="flex flex-wrap gap-2">
                  {insights.data.data.tags?.map((tag) => (
                    <span
                      key={tag}
                      className="rounded-md border px-2 py-1 text-xs text-muted-foreground"
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
            ) : insights.data?.error ? (
              <ErrorState error={insights.data.error} />
            ) : (
              <div className="flex items-center gap-3 rounded-lg bg-muted p-5 text-sm text-muted-foreground">
                <LoaderCircle className="size-4 animate-spin" />
                {isReady
                  ? "Preparing document insights…"
                  : "Insights appear after processing."}
              </div>
            )}
          </TabsContent>
          <TabsContent value="summary">
            <p className="mb-5 text-sm leading-6 text-muted-foreground">
              A fresh summary, shaped around what matters to you.
            </p>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Length">
                <Select value={length} onValueChange={setLength}>
                  <SelectTrigger aria-label="Summary length">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="short">Concise</SelectItem>
                    <SelectItem value="medium">Balanced</SelectItem>
                    <SelectItem value="long">Detailed</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
              <Field label="Tone">
                <Select value={tone} onValueChange={setTone}>
                  <SelectTrigger aria-label="Summary tone">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="neutral">Neutral</SelectItem>
                    <SelectItem value="executive">Executive</SelectItem>
                    <SelectItem value="plain_language">
                      Plain language
                    </SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            </div>
            <div className="mt-4">
              <Field label="Focus areas (optional)">
                <Input
                  value={focus}
                  onChange={(e) => setFocus(e.target.value)}
                  placeholder="e.g. costs, timelines, key risks"
                />
                <span className="text-[11px] font-normal text-muted-foreground">
                  Up to five focus areas, separated by commas.
                </span>
              </Field>
            </div>
            <Button
              className="mt-4 w-full"
              disabled={
                summary.isPending ||
                !isReady ||
                focus.split(",").filter((v) => v.trim()).length > 5
              }
              onClick={() => summary.mutate()}
            >
              <Sparkles />
              {summary.isPending ? "Requesting…" : "Generate summary"}
            </Button>
            {summary.error && (
              <div className="mt-4">
                <ErrorState error={summary.error} />
              </div>
            )}
            {summaryId && (
              <div className="mt-7 border-t pt-6">
                <ArtifactResult id={summaryId} versionId={versionId} />
              </div>
            )}
          </TabsContent>
          <TabsContent value="versions">
            <div className="mb-5 flex items-center justify-between">
              <h3 className="font-medium">Version history</h3>
              <Button
                variant="outline"
                size="sm"
                disabled={newVersion.isPending}
                onClick={() => uploadInput.current?.click()}
              >
                <Upload />
                {newVersion.isPending ? "Uploading…" : "New version"}
              </Button>
              <input
                type="file"
                className="hidden"
                ref={uploadInput}
                accept=".pdf,.docx,.txt"
                onChange={(e) => {
                  if (e.target.files?.[0]) newVersion.mutate(e.target.files[0]);
                  e.target.value = "";
                }}
              />
            </div>
            {newVersion.error && <ErrorState error={newVersion.error} />}
            {versions.error && (
              <ErrorState
                error={versions.error}
                retry={() => void versions.refetch()}
              />
            )}
            {retry.error && <ErrorState error={retry.error} />}
            {versions.isPending && (
              <div
                className="space-y-4 py-4"
                role="status"
                aria-label="Loading version history"
              >
                {[0, 1, 2].map((index) => (
                  <div key={index} className="flex items-center gap-3">
                    <Skeleton className="size-5" />
                    <div className="flex-1 space-y-2">
                      <Skeleton className="h-3 w-24" />
                      <Skeleton className="h-3 w-32" />
                    </div>
                    <Skeleton className="h-5 w-16" />
                  </div>
                ))}
              </div>
            )}
            <div className="divide-y">
              {versions.data?.items.map((v) => (
                <div key={v.id} className="py-4">
                  <div className="flex items-center gap-3">
                    <History className="size-4 text-muted-foreground" />
                    <div className="flex-1">
                      <p className="text-sm font-medium">
                        Version {v.version_number}
                        {doc.current_version_id === v.id && (
                          <span className="ml-2 text-xs text-primary">
                            Current
                          </span>
                        )}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {date(v.created_at)}
                      </p>
                    </div>
                    <StatusBadge status={v.status} />
                  </div>
                  {v.error && (
                    <p className="mt-2 text-xs text-destructive">{v.error}</p>
                  )}
                  <div className="mt-3 flex gap-2 pl-7">
                    <Button size="sm" variant="ghost" asChild>
                      <a
                        href={apiUrl(`/v1/versions/${v.id}/content`)}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <Download />
                        Download
                      </a>
                    </Button>
                    {(v.status === "failed" ||
                      v.insight_status === "failed") && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={retry.isPending}
                        onClick={() => retry.mutate(v.id)}
                      >
                        <RefreshCw />
                        {v.status === "failed"
                          ? "Retry processing"
                          : "Retry insights"}
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
            {newVersion.isError && (
              <p className="sr-only">{errorMessage(newVersion.error)}</p>
            )}
          </TabsContent>
        </Tabs>
      </div>
    </>
  );
}
