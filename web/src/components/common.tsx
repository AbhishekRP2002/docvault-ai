import type { ReactNode } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Circle,
  FileText,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";
import type { DocumentStatus } from "@/lib/types";
export function ErrorState({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex items-start gap-3 rounded-lg border border-destructive/20 bg-destructive/5 p-4 text-sm"
    >
      <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
      <div className="min-w-0 flex-1">
        <p>{error instanceof Error ? error.message : String(error)}</p>
        {retry && (
          <Button className="mt-2" variant="outline" size="sm" onClick={retry}>
            <RefreshCw />
            Try again
          </Button>
        )}
      </div>
    </div>
  );
}
export function EmptyState({
  icon,
  title,
  description,
  children,
}: {
  icon?: ReactNode;
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center px-6 py-16 text-center animate-enter">
      <div className="mb-5 grid size-14 place-items-center rounded-2xl border bg-card text-primary shadow-xs">
        {icon || <FileText className="size-6" />}
      </div>
      <h3 className="text-lg font-semibold tracking-tight">{title}</h3>
      <p className="mt-2 max-w-sm text-sm leading-6 text-muted-foreground">
        {description}
      </p>
      {children && <div className="mt-6">{children}</div>}
    </div>
  );
}
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn("animate-pulse rounded-md bg-muted", className)} />;
}
export function LoadingRows() {
  return (
    <div className="space-y-6 p-6" aria-label="Loading" role="status">
      {[0, 1, 2].map((n) => (
        <div key={n} className="flex items-center gap-4">
          <Skeleton className="size-10" />
          <div className="flex-1 space-y-2">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-3 w-1/5" />
          </div>
          <Skeleton className="h-5 w-16" />
        </div>
      ))}
    </div>
  );
}
const statusLabels: Record<DocumentStatus, string> = {
  ready: "Ready",
  failed: "Needs attention",
  queued: "Queued",
  parsing: "Reading document",
  chunking: "Organizing content",
  embedding: "Indexing",
};
export function StatusBadge({ status }: { status: DocumentStatus }) {
  const Icon =
    status === "ready"
      ? CheckCircle2
      : status === "failed"
        ? AlertCircle
        : status === "queued"
          ? Circle
          : LoaderCircle;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-1 text-[11px] font-medium",
        status === "ready"
          ? "bg-emerald-50 text-emerald-700"
          : status === "failed"
            ? "bg-red-50 text-red-700"
            : "bg-primary/7 text-primary",
      )}
    >
      <Icon
        className={cn(
          "size-3",
          !["ready", "failed", "queued"].includes(status) && "animate-spin",
        )}
      />
      {statusLabels[status]}
    </span>
  );
}
export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-2 text-xs font-medium">
      {label}
      {children}
    </label>
  );
}
export function PageHeading({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow?: string;
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div>
        {eyebrow && (
          <p className="mb-2 text-[11px] font-medium text-muted-foreground">
            {eyebrow}
          </p>
        )}
        <h1 className="text-2xl font-semibold tracking-tight sm:text-[28px]">
          {title}
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">{description}</p>
      </div>
      {children}
    </div>
  );
}
