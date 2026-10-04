import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import type { DocumentMetrics, ProcessingMetrics } from "@/lib/types";
import {
  chartCeiling,
  chartIndex,
  chartSegments,
  formatCost,
  formatNumber,
  formatUsage,
  usageDate,
  usageValue,
  type UsageHistory,
  type UsageMetric,
} from "@/lib/usage";
import { bytes, cn } from "@/lib/utils";
import { ErrorState, PageHeading, Skeleton } from "@/components/common";
import { Button } from "@/components/ui/button";

const metrics: { key: UsageMetric; label: string; unit: string }[] = [
  { key: "requests", label: "Requests", unit: "requests" },
  { key: "tokens", label: "Tokens", unit: "recorded tokens" },
  { key: "cost", label: "Reported cost", unit: "USD · known costs" },
];

function UsageChart({
  data,
  metric,
}: {
  data: UsageHistory;
  metric: UsageMetric;
}) {
  const [selected, setSelected] = useState<number | null>(null);
  const descriptionId = useId();
  const gradientId = useId();
  const values = data.buckets.map((day) => usageValue(day, metric));
  const ceiling = chartCeiling(values, metric);
  const segments = chartSegments(values, ceiling);
  const index = Math.min(
    selected ?? data.buckets.length - 1,
    data.buckets.length - 1,
  );
  const day = data.buckets[index];
  const x = 56 + (index / Math.max(1, values.length - 1)) * 800;
  const selectedValue = values[index];
  const unit = metrics.find((item) => item.key === metric)!.unit;
  const path = (points: { x: number; y: number }[]) =>
    points.map((point) => `${point.x},${point.y}`).join(" ");
  const ticks = [0, ceiling / 2, ceiling];
  const dateIndices = [
    ...new Set([0, Math.floor(values.length / 2), values.length - 1]),
  ];

  return (
    <div className="px-4 pb-4 pt-5 sm:px-6">
      <div className="mb-1 flex min-h-12 flex-wrap items-start justify-between gap-2 text-xs">
        <span className="text-muted-foreground">Daily {unit}</span>
        <div
          id={descriptionId}
          aria-live="polite"
          className="text-right tabular-nums"
        >
          <span className="text-muted-foreground">{usageDate(day.date)} </span>
          <span className="ml-2 font-medium">
            {formatUsage(selectedValue, metric)}
          </span>
          {metric === "cost" && day.unknown_cost_calls > 0 && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              {formatNumber(day.unknown_cost_calls)}{" "}
              {day.unknown_cost_calls === 1 ? "call" : "calls"} without cost
              data
            </p>
          )}
        </div>
      </div>
      <div
        tabIndex={0}
        role="group"
        aria-label={`Daily ${unit} chart. Use left and right arrow keys to inspect each day, Home for the first day, End for today.`}
        aria-describedby={descriptionId}
        className="relative rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-4"
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
            return;
          event.preventDefault();
          setSelected(
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? values.length - 1
                : Math.max(
                    0,
                    Math.min(
                      values.length - 1,
                      index + (event.key === "ArrowLeft" ? -1 : 1),
                    ),
                  ),
          );
        }}
        onPointerMove={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setSelected(
            chartIndex(
              event.clientX - bounds.left,
              bounds.width,
              values.length,
            ),
          );
        }}
        onPointerDown={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          setSelected(
            chartIndex(
              event.clientX - bounds.left,
              bounds.width,
              values.length,
            ),
          );
        }}
      >
        <svg
          viewBox="0 0 880 274"
          className="block w-full overflow-visible"
          aria-hidden="true"
        >
          <defs>
            <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor="#64748b" stopOpacity="0.15" />
              <stop offset="100%" stopColor="#64748b" stopOpacity="0.015" />
            </linearGradient>
          </defs>
          {ticks.map((tick) => {
            const y = 234 - (tick / ceiling) * 206;
            return (
              <g key={tick}>
                <line
                  x1="56"
                  x2="856"
                  y1={y}
                  y2={y}
                  className="stroke-border"
                  strokeDasharray={tick ? "3 5" : undefined}
                />
                <text
                  x="43"
                  y={y + 4}
                  textAnchor="end"
                  className="fill-muted-foreground text-[26px] sm:text-[11px]"
                >
                  {metric === "cost"
                    ? `$${Number(tick.toPrecision(2))}`
                    : new Intl.NumberFormat("en", {
                        notation: "compact",
                        maximumFractionDigits: 1,
                      }).format(tick)}
                </text>
              </g>
            );
          })}
          {segments.map((points, segmentIndex) => (
            <g key={segmentIndex}>
              {points.length > 1 && (
                <>
                  <polygon
                    points={`${points[0].x},234 ${path(points)} ${points[points.length - 1].x},234`}
                    fill={`url(#${gradientId})`}
                  />
                  <polyline
                    points={path(points)}
                    fill="none"
                    stroke="#64748b"
                    strokeWidth="2"
                    strokeLinejoin="round"
                    vectorEffect="non-scaling-stroke"
                  />
                </>
              )}
              {points.length === 1 && (
                <circle
                  cx={points[0].x}
                  cy={points[0].y}
                  r="3"
                  fill="#64748b"
                />
              )}
            </g>
          ))}
          <line
            x1={x}
            x2={x}
            y1="24"
            y2="234"
            className="stroke-muted-foreground/30"
            strokeDasharray="3 4"
          />
          {selectedValue !== null && (
            <circle
              cx={x}
              cy={234 - (selectedValue / ceiling) * 206}
              r="4"
              className="fill-foreground stroke-background"
              strokeWidth="2"
            />
          )}
          {dateIndices.map((i) => (
            <text
              key={i}
              x={56 + (i / Math.max(1, values.length - 1)) * 800}
              y="264"
              textAnchor={
                i === 0 ? "start" : i === values.length - 1 ? "end" : "middle"
              }
              className="fill-muted-foreground text-[26px] sm:text-[11px]"
            >
              {usageDate(data.buckets[i].date)}
            </text>
          ))}
        </svg>
        {data.totals.requests === 0 && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center pb-10 text-center">
            <div className="bg-background/95 px-5 py-3">
              <p className="text-sm font-medium">No requests in this period</p>
              <p className="mt-1 text-xs text-muted-foreground">
                Process a document or start a run to see activity here.
              </p>
            </div>
          </div>
        )}
        {metric === "cost" &&
          data.totals.requests > 0 &&
          data.totals.cost_usd === null && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center pb-10 text-center">
              <div className="bg-background/95 px-5 py-3">
                <p className="text-sm font-medium">Cost data unavailable</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  The provider has not reported cost for these requests.
                </p>
              </div>
            </div>
          )}
      </div>
      <p className="mt-3 text-[11px] text-muted-foreground">
        Hover, tap, or use arrow keys to inspect · UTC · Today is still in
        progress
      </p>
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div
      role="status"
      aria-label="Loading usage dashboard"
      className="rounded-lg border"
    >
      <div className="grid grid-cols-3 gap-4 border-b p-6">
        {[0, 1, 2].map((index) => (
          <div key={index} className="space-y-3">
            <Skeleton className="h-3 w-20 max-w-full" />
            <Skeleton className="h-7 w-16" />
          </div>
        ))}
      </div>
      <div className="space-y-14 px-6 py-10">
        {[0, 1, 2].map((index) => (
          <Skeleton key={index} className="h-px w-full" />
        ))}
        <Skeleton className="h-3 w-40" />
      </div>
    </div>
  );
}

export function Analytics() {
  const [days, setDays] = useState(30);
  const [metric, setMetric] = useState<UsageMetric>("requests");
  const history = useQuery({
    queryKey: ["metrics", "usage", "history", days],
    queryFn: ({ signal }) =>
      api<UsageHistory>(`/v1/metrics/usage/history?days=${days}`, { signal }),
    staleTime: 30_000,
  });
  const documents = useQuery({
    queryKey: ["metrics", "documents"],
    queryFn: ({ signal }) =>
      api<DocumentMetrics>("/v1/metrics/documents", { signal }),
    staleTime: 30_000,
  });
  const processing = useQuery({
    queryKey: ["metrics", "processing"],
    queryFn: ({ signal }) =>
      api<ProcessingMetrics>("/v1/metrics/processing", { signal }),
    refetchInterval: (query) =>
      query.state.data?.active || query.state.data?.queued ? 5_000 : false,
  });
  const refresh = () => {
    void history.refetch();
    void documents.refetch();
    void processing.refetch();
  };
  const isRefreshing =
    history.isFetching || documents.isFetching || processing.isFetching;
  const data = history.data;
  const jobs = processing.data;
  const jobRows = [
    { label: "Completed", value: jobs?.completed, color: "bg-emerald-500" },
    { label: "Running", value: jobs?.active, color: "bg-blue-500" },
    { label: "Queued", value: jobs?.queued, color: "bg-amber-400" },
    { label: "Failed", value: jobs?.failed, color: "bg-red-400" },
  ];
  const totalJobs = jobRows.reduce((total, row) => total + (row.value ?? 0), 0);

  return (
    <div className="mx-auto max-w-[1400px] p-5 sm:p-7 animate-enter">
      <PageHeading
        title="Usage"
        description="Track model activity and workspace processing."
      >
        <div className="flex items-center gap-2">
          <div
            role="group"
            aria-label="Usage date range"
            className="flex rounded-md border bg-muted/40 p-0.5"
          >
            {[7, 30, 90].map((range) => (
              <button
                key={range}
                onClick={() => setDays(range)}
                aria-pressed={days === range}
                className={cn(
                  "rounded px-3 py-1.5 text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
                  days === range
                    ? "bg-background text-foreground shadow-xs"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {range} days
              </button>
            ))}
          </div>
          <Button
            variant="outline"
            size="icon"
            onClick={refresh}
            disabled={isRefreshing}
            aria-label="Refresh usage"
          >
            <RefreshCw
              className={cn(
                isRefreshing && "animate-spin motion-reduce:animate-none",
              )}
            />
          </Button>
        </div>
      </PageHeading>
      {[history.error, documents.error, processing.error]
        .filter(Boolean)
        .map((error, index) => (
          <div key={index} className="mb-4">
            <ErrorState error={error} retry={refresh} />
          </div>
        ))}
      {history.isPending ? (
        <DashboardSkeleton />
      ) : data ? (
        <section
          aria-label="Model usage over time"
          className="rounded-lg border bg-card animate-enter"
        >
          <div className="flex flex-wrap items-stretch justify-between border-b">
            <div role="group" aria-label="Chart metric" className="flex flex-1">
              {metrics.map((item) => (
                <button
                  key={item.key}
                  onClick={() => setMetric(item.key)}
                  aria-pressed={metric === item.key}
                  className={cn(
                    "relative min-w-0 flex-1 px-4 py-5 text-left outline-none transition-colors hover:bg-muted/40 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:max-w-52 sm:px-6",
                    metric === item.key &&
                      "bg-muted/25 after:absolute after:inset-x-4 after:bottom-0 after:h-0.5 after:bg-foreground",
                  )}
                >
                  <span className="text-xs text-muted-foreground">
                    {item.label}
                  </span>
                  <span className="mt-2 block break-all text-xl font-semibold tracking-tight tabular-nums sm:text-2xl">
                    {item.key === "cost" && data.totals.cost_usd === null
                      ? "—"
                      : formatUsage(
                          usageValue(data.totals, item.key),
                          item.key,
                        )}
                  </span>
                  {item.key === "cost" &&
                    data.totals.unknown_cost_calls > 0 && (
                      <span className="mt-1 block text-[10px] text-muted-foreground">
                        {data.totals.cost_usd === null
                          ? "Not reported"
                          : "Partial total"}
                      </span>
                    )}
                </button>
              ))}
            </div>
            <p className="self-center px-6 py-3 text-[11px] text-muted-foreground">
              {usageDate(data.start_date)} – {usageDate(data.end_date, true)}
            </p>
          </div>
          <UsageChart key={days} data={data} metric={metric} />
          <div className="flex flex-wrap gap-x-6 gap-y-2 border-t px-6 py-3 text-[11px] text-muted-foreground">
            <span>
              Input tokens{" "}
              <strong className="ml-1 font-medium text-foreground tabular-nums">
                {formatNumber(data.totals.input_tokens)}
              </strong>
            </span>
            <span>
              Output tokens{" "}
              <strong className="ml-1 font-medium text-foreground tabular-nums">
                {formatNumber(data.totals.output_tokens)}
              </strong>
            </span>
            {data.totals.unknown_cost_calls > 0 && (
              <span>
                {formatNumber(data.totals.unknown_cost_calls)} requests without
                cost data · excluded from reported cost
              </span>
            )}
          </div>
        </section>
      ) : null}

      <div className="mt-7 grid gap-8 xl:grid-cols-[minmax(0,1fr)_300px]">
        <section className="min-w-0">
          <div className="mb-4 flex items-center justify-between gap-3">
            <h2 className="text-sm font-medium">Usage by model</h2>
            <span className="text-[11px] text-muted-foreground">
              Last {days} days · up to 20 models
            </span>
          </div>
          {history.isPending ? (
            <div className="space-y-5 rounded-lg border p-5">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-6 w-full" />
              ))}
            </div>
          ) : data ? (
            <div className="overflow-x-auto rounded-lg border">
              <table className="w-full text-left text-xs">
                <thead className="border-b bg-muted/30 text-[11px] text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 font-normal">Model</th>
                    <th className="px-4 py-3 text-right font-normal">
                      Requests
                    </th>
                    <th className="px-4 py-3 text-right font-normal">Tokens</th>
                    <th className="px-4 py-3 text-right font-normal">
                      Cost (USD)
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {data.models.map((model) => (
                    <tr key={model.model} className="hover:bg-muted/30">
                      <td className="max-w-64 px-4 py-3.5">
                        <span
                          className="block truncate font-medium"
                          title={model.model}
                        >
                          {model.model}
                        </span>
                        <div
                          className="mt-2 h-1 w-28 overflow-hidden rounded-full bg-muted"
                          aria-hidden="true"
                        >
                          <div
                            className="h-full rounded-full bg-slate-400"
                            style={{
                              width: `${(model.requests / Math.max(1, data.totals.requests)) * 100}%`,
                            }}
                          />
                        </div>
                      </td>
                      <td className="px-4 py-3.5 text-right tabular-nums">
                        {formatNumber(model.requests)}
                      </td>
                      <td className="px-4 py-3.5 text-right tabular-nums">
                        {formatNumber(model.input_tokens + model.output_tokens)}
                      </td>
                      <td className="px-4 py-3.5 text-right tabular-nums">
                        <span>{formatCost(model.cost_usd)}</span>
                        {model.unknown_cost_calls > 0 && (
                          <span className="mt-1 block whitespace-nowrap text-[10px] text-muted-foreground">
                            {model.cost_usd === null
                              ? "Not reported"
                              : "Partial"}{" "}
                            · {formatNumber(model.unknown_cost_calls)} unknown
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                  {!data.models.length && (
                    <tr>
                      <td
                        colSpan={4}
                        className="px-4 py-9 text-center text-muted-foreground"
                      >
                        No recorded model usage in this period.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="py-6 text-xs text-muted-foreground">
              Model usage is unavailable. Retry above to load it.
            </p>
          )}
        </section>

        <section>
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-medium">Processing</h2>
            <span className="text-[11px] text-muted-foreground">All time</span>
          </div>
          {processing.isPending ? (
            <div className="space-y-5">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-5 w-full" />
              ))}
            </div>
          ) : jobs ? (
            <>
              <div
                className="mb-5 flex h-2 overflow-hidden rounded-full bg-muted"
                role="img"
                aria-label={jobRows
                  .map((row) => `${row.value} ${row.label.toLowerCase()} jobs`)
                  .join(", ")}
              >
                {jobRows.map((row) => (
                  <div
                    key={row.label}
                    className={row.color}
                    style={{
                      width: `${((row.value ?? 0) / Math.max(totalJobs, 1)) * 100}%`,
                    }}
                  />
                ))}
              </div>
              <div className="space-y-3.5">
                {jobRows.map((row) => (
                  <div
                    key={row.label}
                    className="flex items-center gap-2 text-xs"
                  >
                    <span className={cn("size-1.5 rounded-full", row.color)} />
                    <span className="flex-1 text-muted-foreground">
                      {row.label}
                    </span>
                    <span className="tabular-nums">
                      {formatNumber(row.value ?? 0)}
                    </span>
                  </div>
                ))}
              </div>
              <div className="mt-5 flex justify-between border-t pt-4 text-[11px] text-muted-foreground">
                <span>
                  Avg.{" "}
                  {jobs.average_duration_ms == null
                    ? "—"
                    : `${(jobs.average_duration_ms / 1000).toFixed(1)}s`}
                </span>
                <span>
                  P95{" "}
                  {jobs.p95_duration_ms == null
                    ? "—"
                    : `${(jobs.p95_duration_ms / 1000).toFixed(1)}s`}
                </span>
              </div>
            </>
          ) : (
            <p className="text-xs text-muted-foreground">
              Processing data is unavailable.
            </p>
          )}
        </section>
      </div>

      <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3 border-t pt-4 text-xs text-muted-foreground">
        <a
          href="#library"
          className="inline-flex items-center gap-1.5 font-medium text-foreground hover:underline"
        >
          Workspace <ArrowUpRight className="size-3" />
        </a>
        {documents.data ? (
          <>
            <span>{formatNumber(documents.data.documents)} files</span>
            <span>{bytes(documents.data.storage_bytes)} stored</span>
            <span>{formatNumber(documents.data.ready)} ready versions</span>
            <span>{formatNumber(documents.data.processing)} processing</span>
          </>
        ) : documents.isPending ? (
          <Skeleton className="h-3 w-56" />
        ) : (
          <span>Storage data unavailable</span>
        )}
      </div>
    </div>
  );
}
