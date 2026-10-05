import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  ProcessingDiagnostics,
  ProcessingDiagnosticsContent,
} from "../components/processing-diagnostics";
import {
  canRetryProcessing,
  processingDuration,
  processingLabel,
  processingTimestamp,
  type JobDiagnostic,
  type VersionDiagnostics,
} from "./processing-diagnostics";

const job: JobDiagnostic = {
  id: "job-old-version",
  kind: "ingest",
  resource_id: "old-version",
  status: "failed",
  stage: "embedding",
  attempts: 2,
  created_at: "2026-10-05T00:00:00Z",
  started_at: "2026-10-05T00:00:01Z",
  finished_at: "2026-10-05T00:00:03Z",
  next_retry_at: null,
  lease_until: null,
  queue_state: "failed",
  retry_eligible: true,
  dead_letter: true,
  error: {
    code: "provider_rate_limited",
    message: "Embedding requests were rate limited.",
    retryable: true,
    remediation: "Wait before retrying this job.",
  },
  history: [{
    id: "attempt-2",
    attempt: 2,
    status: "failed",
    stage: "embedding",
    started_at: "2026-10-05T00:00:01Z",
    finished_at: "2026-10-05T00:00:03Z",
    error: null,
    stages: [{
      id: "stage-1",
      stage: "conversion",
      status: "complete",
      started_at: "2026-10-05T00:00:01Z",
      finished_at: "2026-10-05T00:00:02Z",
      duration_ms: 1000,
    }],
  }],
};

function renderJobs(items: JobDiagnostic[], total = items.length) {
  return renderToStaticMarkup(
    <ProcessingDiagnosticsContent data={{ items, total }} onRetry={() => {}} />,
  );
}

describe("processing diagnostics", () => {
  test("renders recorded attempts, stage timings, failure reason and remediation", () => {
    const html = renderJobs([job]);
    expect(html).toContain("Document processing");
    expect(html).toContain("Attempt 2");
    expect(html).toContain("Conversion");
    expect(html).toContain("1.0 s");
    expect(html).toContain(job.error!.message);
    expect(html).toContain(job.error!.code);
    expect(html).toContain(job.error!.remediation);
    expect(html).toContain("automatic retries have stopped");
    expect(html).toContain("Retry job");
  });

  test("retry controls respect terminal status and backend eligibility", () => {
    for (const status of ["cancelled", "complete", "running", "queued"]) {
      const candidate = { ...job, status };
      expect(canRetryProcessing(candidate)).toBe(false);
      expect(renderJobs([candidate])).not.toContain("Retry job");
    }
    expect(renderJobs([{ ...job, retry_eligible: false }])).not.toContain("Retry job");
    const html = renderToStaticMarkup(
      <ProcessingDiagnosticsContent
        data={{ items: [job], total: 1 }}
        onRetry={() => {}}
        retryingJobId={job.id}
        retryDisabled
      />,
    );
    expect(html).toContain("Requesting retry");
    expect(html).toContain('disabled=""');
  });

  test("queued retries and historical gaps do not fabricate stage measurements", () => {
    const html = renderJobs([{
      ...job,
      status: "queued",
      queue_state: "retry_waiting",
      next_retry_at: "2026-10-05T00:05:00Z",
      dead_letter: false,
      error: null,
      history: [],
    }]);
    expect(html).toContain("Waiting for retry");
    expect(html).toContain("Next automatic retry");
    expect(html).toContain("Attempt timelines were not recorded");
    expect(html).not.toContain("Dead letter");
    expect(renderJobs([{ ...job, history: [{ ...job.history[0], stages: [] }] }])).toContain("Stage timings were not measured");
    expect(processingDuration(null)).toBe("Not measured");
    expect(processingDuration(-1)).toBe("Not measured");
    expect(processingDuration(Number.NaN)).toBe("Not measured");
    expect(processingDuration(0)).toBe("0 ms");
    expect(processingDuration(60_000)).toBe("1.0 min");
    expect(processingTimestamp(null)).toBe("Not recorded");
    expect(processingTimestamp("invalid")).toBe("Not recorded");
    expect(processingLabel("new_stage")).toBe("new stage");
  });

  test("empty and bounded histories are explicit", () => {
    expect(renderJobs([])).toContain("No processing history is available");
    const html = renderJobs([{ ...job, history_truncated: true }], 4);
    expect(html).toContain("Showing the newest 1 of 4 jobs");
    expect(html).toContain("Only the newest attempts are shown");
  });

  test("insights failures remain separate from successful ingestion", () => {
    const html = renderJobs([
      { ...job, status: "complete", queue_state: "complete", error: null, dead_letter: false },
      { ...job, id: "insights-job", kind: "insights", stage: "generating" },
    ]);
    expect(html).toContain("Document processing");
    expect(html).toContain("Document insights");
    expect(html).toContain("Generating insights");
    expect(html.match(/Retry job/g)).toHaveLength(1);
    expect(processingLabel("interrupted")).toBe("Interrupted");
  });

  test("a historical version panel uses that version's cached diagnostics", () => {
    const client = new QueryClient();
    client.setQueryData<VersionDiagnostics>(["processing-diagnostics", "old-version"], { items: [job], total: 1 });
    client.setQueryData<VersionDiagnostics>(["processing-diagnostics", "current-version"], { items: [], total: 0 });
    try {
      const html = renderToStaticMarkup(
        <QueryClientProvider client={client}>
          <ProcessingDiagnostics versionId="old-version" />
        </QueryClientProvider>,
      );
      expect(html).toContain(job.error!.message);
      expect(html).toContain("Refresh");
      expect(html).toContain("Refresh to check for updates");
      expect(html).not.toContain("No processing history is available");
    } finally {
      client.clear();
    }
  });
});
