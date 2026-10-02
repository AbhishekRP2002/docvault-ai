import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownToLine,
  ArrowUpFromLine,
  Coins,
  Database,
  Files,
  RefreshCw,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  DocumentMetrics,
  ProcessingMetrics,
  UsageMetrics,
} from "@/lib/types";
import { bytes } from "@/lib/utils";
import { ErrorState, PageHeading, Skeleton } from "@/components/common";
import { Button } from "@/components/ui/button";
const number = (value: number) => new Intl.NumberFormat().format(value);
export function Analytics() {
  const documents = useQuery({
    queryKey: ["metrics", "documents"],
    queryFn: () => api<DocumentMetrics>("/v1/metrics/documents"),
    refetchInterval: 10000,
  });
  const processing = useQuery({
    queryKey: ["metrics", "processing"],
    queryFn: () => api<ProcessingMetrics>("/v1/metrics/processing"),
    refetchInterval: 10000,
  });
  const usage = useQuery({
    queryKey: ["metrics", "usage"],
    queryFn: () => api<UsageMetrics>("/v1/metrics/usage"),
    refetchInterval: 10000,
  });
  const refresh = () => {
    void documents.refetch();
    void processing.refetch();
    void usage.refetch();
  };
  const documentMetrics = documents.data;
  const processMetrics = processing.data;
  const usageMetrics = usage.data;
  return (
    <div className="mx-auto max-w-6xl p-5 sm:p-8 lg:p-10 animate-enter">
      <PageHeading
        eyebrow="Workspace overview"
        title="A little perspective"
        description="Document activity, processing health, and AI usage."
      >
        <Button variant="outline" onClick={refresh}>
          <RefreshCw />
          Refresh
        </Button>
      </PageHeading>
      {[documents.error, processing.error, usage.error]
        .filter(Boolean)
        .map((error, i) => (
          <div key={i} className="mb-4">
            <ErrorState error={error} retry={refresh} />
          </div>
        ))}
      <div className="mb-8 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {[
          {
            label: "Documents",
            icon: Files,
            value: documentMetrics && number(documentMetrics.documents),
            detail: documentMetrics
              ? `${number(documentMetrics.versions)} total versions`
              : "All uploaded files",
          },
          {
            label: "Storage used",
            icon: Database,
            value: documentMetrics && bytes(documentMetrics.storage_bytes),
            detail: "Original document storage",
          },
          {
            label: "AI requests",
            icon: Activity,
            value: usageMetrics && number(usageMetrics.requests),
            detail: usageMetrics
              ? `${number(usageMetrics.cache_hits)} cache hits`
              : "Provider requests",
          },
          {
            label: "Reported AI cost",
            icon: Coins,
            value:
              usageMetrics &&
              (usageMetrics.cost_usd == null
                ? "Unavailable"
                : new Intl.NumberFormat("en-US", {
                    style: "currency",
                    currency: "USD",
                    maximumFractionDigits: 4,
                  }).format(usageMetrics.cost_usd)),
            detail: usageMetrics?.unknown_cost_calls
              ? `${number(usageMetrics.unknown_cost_calls)} calls with cost unavailable`
              : "Provider-reported cost",
          },
        ].map(({ label, icon: Icon, value, detail }) => (
          <div key={label} className="rounded-xl border bg-card p-5">
            <div className="mb-5 flex justify-between text-muted-foreground">
              <span className="text-xs">{label}</span>
              <Icon className="size-4" />
            </div>
            {value !== undefined ? (
              <p className="text-2xl font-semibold tracking-tight tabular-nums">
                {value}
              </p>
            ) : (
              <Skeleton className="h-8 w-20" />
            )}
            <p className="mt-2 text-[11px] text-muted-foreground">{detail}</p>
          </div>
        ))}
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="rounded-xl border bg-card p-6">
          <h2 className="font-semibold">Processing health</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Live status across your document pipeline.
          </p>
          <div className="mt-7 space-y-5">
            {[
              {
                label: "Ready documents",
                value: documentMetrics?.ready,
                color: "bg-emerald-500",
              },
              {
                label: "Active jobs",
                value: processMetrics?.active,
                color: "bg-primary",
              },
              {
                label: "Queued jobs",
                value: processMetrics?.queued,
                color: "bg-amber-400",
              },
              {
                label: "Failed jobs",
                value: processMetrics?.failed,
                color: "bg-destructive",
              },
            ].map((row) => (
              <div key={row.label} className="flex items-center gap-3 text-sm">
                <span className={`size-2 rounded-full ${row.color}`} />
                <span className="flex-1 text-muted-foreground">
                  {row.label}
                </span>
                <span className="font-medium tabular-nums">
                  {row.value === undefined ? "—" : number(row.value)}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-7 grid grid-cols-2 gap-4 border-t pt-5">
            <div>
              <p className="text-[11px] text-muted-foreground">
                Average processing time
              </p>
              <p className="mt-2 font-medium tabular-nums">
                {processMetrics?.average_duration_ms == null
                  ? "—"
                  : `${(processMetrics.average_duration_ms / 1000).toFixed(1)}s`}
              </p>
            </div>
            <div>
              <p className="text-[11px] text-muted-foreground">
                95th percentile
              </p>
              <p className="mt-2 font-medium tabular-nums">
                {processMetrics?.p95_duration_ms == null
                  ? "—"
                  : `${(processMetrics.p95_duration_ms / 1000).toFixed(1)}s`}
              </p>
            </div>
          </div>
        </section>
        <section className="rounded-xl border bg-card p-6">
          <h2 className="font-semibold">AI usage</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Tokens reported by your model provider.
          </p>
          <div className="mt-7 space-y-6">
            {[
              {
                label: "Input tokens",
                value: usageMetrics?.input_tokens,
                icon: ArrowUpFromLine,
                description: "Questions, context, and document content",
              },
              {
                label: "Output tokens",
                value: usageMetrics?.output_tokens,
                icon: ArrowDownToLine,
                description: "Generated answers, summaries, and insights",
              },
            ].map(({ label, value, icon: Icon, description }) => (
              <div key={label} className="flex items-start gap-3">
                <div className="rounded-lg border p-2.5 text-muted-foreground">
                  <Icon className="size-4" />
                </div>
                <div className="flex-1">
                  <p className="text-xs text-muted-foreground">{label}</p>
                  <p className="mt-1 text-xl font-semibold tabular-nums">
                    {value === undefined ? "—" : number(value)}
                  </p>
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {description}
                  </p>
                </div>
              </div>
            ))}
          </div>
          <p className="mt-7 border-t pt-5 text-xs leading-6 text-muted-foreground">
            Metrics reflect stored usage. Calls without provider cost data are
            marked as unavailable and excluded from the reported total.
          </p>
        </section>
      </div>
    </div>
  );
}
