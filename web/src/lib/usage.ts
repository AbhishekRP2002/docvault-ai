export interface RecordedUsage {
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  unknown_cost_calls: number;
}

export interface UsageDay extends RecordedUsage {
  date: string;
}

export interface UsageHistory {
  timezone: "UTC";
  start_date: string;
  end_date: string;
  totals: RecordedUsage;
  buckets: UsageDay[];
  models: (RecordedUsage & { model: string })[];
}

export type UsageMetric = "requests" | "tokens" | "cost";

export const formatNumber = (value: number) =>
  new Intl.NumberFormat().format(value);

export function formatCost(value: number | null) {
  if (value === null) return "Unavailable";
  if (value > 0 && value < 0.0001) return "<$0.0001";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(value);
}

export function usageValue(usage: RecordedUsage, metric: UsageMetric) {
  if (metric === "tokens") return usage.input_tokens + usage.output_tokens;
  return metric === "cost" ? usage.cost_usd : usage.requests;
}

export function formatUsage(value: number | null, metric: UsageMetric) {
  return metric === "cost" ? formatCost(value) : formatNumber(value ?? 0);
}

export function usageDate(date: string, long = false) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    ...(long ? { year: "numeric" } : {}),
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
}

/** Do not connect unknown-cost days to zero or interpolate across their gaps. */
export function chartSegments(values: (number | null)[], ceiling: number) {
  const segments: { x: number; y: number }[][] = [];
  values.forEach((value, index) => {
    if (value === null) return;
    if (index === 0 || values[index - 1] === null) segments.push([]);
    segments[segments.length - 1].push({
      x: 56 + (index / Math.max(1, values.length - 1)) * 800,
      y: 234 - (value / ceiling) * 206,
    });
  });
  return segments;
}

export function chartCeiling(values: (number | null)[], metric: UsageMetric) {
  const max = Math.max(0, ...values.map((value) => value ?? 0));
  if (max === 0) return metric === "cost" ? 0.01 : 4;
  const magnitude = 10 ** Math.floor(Math.log10(max));
  const step = metric === "cost" ? magnitude / 2 : Math.max(1, magnitude) * 2;
  return Math.ceil((max * 1.1) / step) * step;
}

export function chartIndex(relativeX: number, width: number, count: number) {
  const position = ((relativeX / width) * 880 - 56) / 800;
  return Math.max(0, Math.min(count - 1, Math.round(position * (count - 1))));
}
