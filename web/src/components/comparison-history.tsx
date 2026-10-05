import { lazy, Suspense, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  GitCompareArrows,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { api } from "@/lib/api";
import type { ComparisonHistory, ComparisonRun } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./ui/dialog";
import { EmptyState, ErrorState, LoadingRows } from "./common";

const ArtifactResult = lazy(() =>
  import("./artifact-result").then((module) => ({
    default: module.ArtifactResult,
  })),
);
const pageSize = 30;
const activeStatuses = new Set(["pending", "queued", "processing", "running"]);

function ComparisonStatus({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded px-1.5 py-0.5 text-[11px]",
        status === "ready"
          ? "bg-emerald-50 text-emerald-700"
          : status === "failed"
            ? "bg-red-50 text-red-700"
            : "bg-muted text-muted-foreground",
      )}
    >
      {activeStatuses.has(status) && (
        <LoaderCircle className="size-3 animate-spin" />
      )}
      {status === "ready"
        ? "Completed"
        : status === "failed"
          ? "Failed"
          : status === "cancelled"
            ? "Cancelled"
            : "Processing"}
    </span>
  );
}

export function ComparisonHistoryList({
  items,
  onOpen,
}: {
  items: ComparisonRun[];
  onOpen: (run: ComparisonRun) => void;
}) {
  return (
    <div className="divide-y rounded-lg border">
      {items.map((run) => {
        const available = run.sources.every((source) => source.available);
        return (
          <button
            key={run.id}
            className="flex w-full items-start justify-between gap-3 px-4 py-3 text-left outline-none hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60"
            disabled={!available}
            onClick={() => onOpen(run)}
          >
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">
                {run.sources
                  .map((source) => source.filename || source.title)
                  .join(" · ")}
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {run.dimensions.join(", ")}
              </p>
              <p className="mt-1 text-[11px] text-muted-foreground">
                {new Date(run.created_at).toLocaleString()} ·{" "}
                {available ? "Saved comparison" : "Source unavailable"}
              </p>
              {run.error && (
                <p className="mt-1 text-xs text-red-700">{run.error}</p>
              )}
            </div>
            <ComparisonStatus status={run.status} />
          </button>
        );
      })}
    </div>
  );
}

export function ComparisonHistoryDialog({ onClose }: { onClose: () => void }) {
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<ComparisonRun | null>(null);
  const history = useQuery({
    queryKey: ["comparisons", page],
    queryFn: ({ signal }) =>
      api<ComparisonHistory>(
        `/v1/comparisons?limit=${pageSize}&offset=${page * pageSize}`,
        { signal },
      ),
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (run) =>
          activeStatuses.has(run.status) &&
          run.sources.every((source) => source.available),
      )
        ? 2500
        : false,
  });
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl">
        {selected && (
          <Button
            variant="ghost"
            size="sm"
            className="mb-3 -ml-2"
            onClick={() => setSelected(null)}
          >
            <ArrowLeft /> Back to comparisons
          </Button>
        )}
        <DialogTitle>
          {selected ? "Comparison result" : "Comparisons"}
        </DialogTitle>
        <DialogDescription>
          {selected
            ? selected.dimensions.join(", ")
            : "Saved runs stay here when you close the result, switch pages, or refresh."}
        </DialogDescription>
        {selected ? (
          <div className="mt-5">
            <p className="mb-4 text-xs text-muted-foreground">
              Created {new Date(selected.created_at).toLocaleString()}
            </p>
            <Suspense fallback={<LoadingRows />}>
              <ArtifactResult id={selected.id} sources={selected.sources} />
            </Suspense>
          </div>
        ) : (
          <div className="mt-5 space-y-4">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">
                {history.data
                  ? `${history.data.total} saved runs`
                  : "Loading runs…"}
              </span>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Refresh comparisons"
                disabled={history.isFetching}
                onClick={() => void history.refetch()}
              >
                <RefreshCw
                  className={cn(history.isFetching && "animate-spin")}
                />
              </Button>
            </div>
            {history.error ? (
              <ErrorState
                error={history.error}
                retry={() => void history.refetch()}
              />
            ) : history.isPending ? (
              <LoadingRows />
            ) : history.data?.items.length ? (
              <ComparisonHistoryList
                items={history.data.items}
                onOpen={setSelected}
              />
            ) : (
              <EmptyState
                icon={<GitCompareArrows />}
                title={page ? "No more comparisons" : "No comparisons yet"}
                description="Select at least two ready files and choose Compare. Every accepted run is saved here."
              />
            )}
            {history.data && (page > 0 || history.data.total > pageSize) && (
              <div className="flex items-center justify-between">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page === 0}
                  onClick={() => setPage((value) => value - 1)}
                >
                  <ChevronLeft /> Previous
                </Button>
                <span className="text-xs text-muted-foreground">
                  Page {page + 1}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={(page + 1) * pageSize >= history.data.total}
                  onClick={() => setPage((value) => value + 1)}
                >
                  Next <ChevronRight />
                </Button>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
