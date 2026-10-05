import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LoaderCircle, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import {
  canRetryProcessing,
  processingDuration,
  processingLabel,
  processingTimestamp,
  type JobDiagnostic,
  type ProcessingError,
  type VersionDiagnostics,
} from "@/lib/processing-diagnostics";
import { Button } from "./ui/button";
import { ErrorState } from "./common";

/** Mounted only when a version's details are expanded; refresh is user initiated. */
export function ProcessingDiagnostics({ versionId }: { versionId: string }) {
  const client = useQueryClient();
  const diagnostics = useQuery({
    queryKey: ["processing-diagnostics", versionId],
    queryFn: ({ signal }) =>
      api<VersionDiagnostics>(`/v1/versions/${versionId}/diagnostics`, { signal }),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
  const retry = useMutation({
    mutationFn: (jobId: string) =>
      api<{ id: string; status: string }>(`/v1/jobs/${jobId}/retry`, {
        method: "POST",
      }),
    onSuccess: async () => {
      await diagnostics.refetch();
      void client.invalidateQueries({ queryKey: ["documents"] });
      void client.invalidateQueries({ queryKey: ["versions"] });
      void client.invalidateQueries({ queryKey: ["insights", versionId] });
    },
  });
  return (
    <section
      className="mt-4 space-y-4 rounded-lg border bg-card p-4"
      aria-label="Processing diagnostics"
    >
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-medium">Processing details</h4>
        <Button
          size="sm"
          variant="ghost"
          disabled={diagnostics.isFetching}
          onClick={() => void diagnostics.refetch()}
        >
          <RefreshCw
            className={diagnostics.isFetching ? "animate-spin" : ""}
          />
          Refresh
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        Snapshot of this version’s processing. Refresh to check for updates.
      </p>
      {diagnostics.isPending && !diagnostics.isError && (
        <p
          role="status"
          className="flex items-center gap-2 text-sm text-muted-foreground"
        >
          <LoaderCircle className="size-4 animate-spin" />
          Loading processing details…
        </p>
      )}
      {diagnostics.error && <ErrorState error={diagnostics.error} />}
      {retry.error && <ErrorState error={retry.error} />}
      {retry.isSuccess && retry.data.status === "queued" && (
        <p role="status" className="text-xs text-muted-foreground">
          Retry queued. Refresh to track progress.
        </p>
      )}
      {diagnostics.data && (
        <ProcessingDiagnosticsContent
          data={diagnostics.data}
          retryingJobId={retry.isPending ? retry.variables : undefined}
          onRetry={(jobId) => retry.mutate(jobId)}
          retryDisabled={retry.isPending}
        />
      )}
    </section>
  );
}

/** Render recorded job/attempt history without fetching or inferring missing measurements. */
export function ProcessingDiagnosticsContent({
  data,
  onRetry,
  retryingJobId,
  retryDisabled = false,
}: {
  data: VersionDiagnostics;
  onRetry: (jobId: string) => void;
  retryingJobId?: string;
  retryDisabled?: boolean;
}) {
  if (!data.items.length)
    return (
      <p className="text-sm text-muted-foreground">
        No processing history is available for this version.
      </p>
    );
  return (
    <div className="space-y-5">
      {data.items.map((job) => (
        <JobHistory
          key={job.id}
          job={job}
          onRetry={onRetry}
          retrying={retryingJobId === job.id}
          retryDisabled={retryDisabled}
        />
      ))}
      {data.total > data.items.length && (
        <p className="text-xs text-muted-foreground">
          Showing the newest {data.items.length} of {data.total} jobs.
        </p>
      )}
    </div>
  );
}

function FailureDetails({ error }: { error: ProcessingError | null }) {
  if (!error) return null;
  return (
    <div className="space-y-1 rounded-md bg-destructive/5 p-3 text-xs">
      <p className="font-medium text-destructive">{error.message}</p>
      <p className="text-muted-foreground">{error.code}</p>
      {error.remediation && <p className="leading-5">{error.remediation}</p>}
    </div>
  );
}

function JobHistory({
  job,
  onRetry,
  retrying,
  retryDisabled,
}: {
  job: JobDiagnostic;
  onRetry: (jobId: string) => void;
  retrying: boolean;
  retryDisabled: boolean;
}) {
  return (
    <article className="space-y-3 border-t pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h5 className="text-sm font-medium">{processingLabel(job.kind)}</h5>
        <span className="rounded-md bg-muted px-2 py-1 text-xs">
          {processingLabel(job.queue_state)}
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
        <div>
          <dt className="text-muted-foreground">Stage</dt>
          <dd className="mt-1">{processingLabel(job.stage)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Attempts</dt>
          <dd className="mt-1">{job.attempts}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Started</dt>
          <dd className="mt-1">{processingTimestamp(job.started_at)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Finished</dt>
          <dd className="mt-1">{processingTimestamp(job.finished_at)}</dd>
        </div>
      </dl>
      {job.next_retry_at && (
        <p className="text-xs text-muted-foreground">
          Next automatic retry: {processingTimestamp(job.next_retry_at)}
        </p>
      )}
      {job.lease_until && (
        <p className="text-xs text-muted-foreground">
          Execution lease expires: {processingTimestamp(job.lease_until)}
        </p>
      )}
      {job.dead_letter && (
        <p className="text-xs font-medium text-destructive">
          Dead letter · automatic retries have stopped.
        </p>
      )}
      <FailureDetails error={job.error} />
      {canRetryProcessing(job) && (
        <Button
          size="sm"
          variant="outline"
          disabled={retryDisabled}
          onClick={() => onRetry(job.id)}
        >
          <RefreshCw />
          {retrying ? "Requesting retry…" : "Retry job"}
        </Button>
      )}
      {!job.history.length ? (
        <p className="text-xs text-muted-foreground">Attempt timelines were not recorded for this job.</p>
      ) : job.history.map((attempt) => (
        <details key={attempt.id} className="rounded-md border p-3">
          <summary className="cursor-pointer text-xs font-medium">
            Attempt {attempt.attempt} · {processingLabel(attempt.status)}
          </summary>
          <div className="mt-3 space-y-3 text-xs">
            <p className="text-muted-foreground">
              Started {processingTimestamp(attempt.started_at)}
              {attempt.finished_at &&
                ` · Finished ${processingTimestamp(attempt.finished_at)}`}
            </p>
            <FailureDetails error={attempt.error} />
            {!attempt.stages.length ? (
              <p className="text-muted-foreground">
                Stage timings were not measured for this attempt.
              </p>
            ) : (
              <ol className="space-y-3 border-l pl-3">
                {attempt.stages.map((stage) => (
                  <li key={stage.id}>
                    <div className="flex flex-wrap justify-between gap-2">
                      <span>{processingLabel(stage.stage)}</span>
                      <span className="text-muted-foreground">
                        {processingLabel(stage.status)} ·{" "}
                        {processingDuration(stage.duration_ms)}
                      </span>
                    </div>
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      {processingTimestamp(stage.started_at)}
                      {stage.finished_at &&
                        ` → ${processingTimestamp(stage.finished_at)}`}
                    </p>
                  </li>
                ))}
              </ol>
            )}
          </div>
        </details>
      ))}
      {job.history_truncated && (
        <p className="text-xs text-muted-foreground">
          Only the newest attempts are shown.
        </p>
      )}
    </article>
  );
}
