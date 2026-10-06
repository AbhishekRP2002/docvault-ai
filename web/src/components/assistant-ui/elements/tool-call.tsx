"use client";

// Adapted from assistant-ui's MIT-licensed ToolCall element. Existing Radix
// primitives and local animation tokens replace its registry styling helpers.
// https://www.assistant-ui.com/elements/tool-call
import { Check, ChevronRight, CircleAlert, LoaderCircle } from "lucide-react";
import { Collapsible } from "radix-ui";
import { cn } from "@/lib/utils";

export interface ToolCallProps {
  label: string;
  activeLabel: string;
  query: string;
  request: string;
  result: string;
  running: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  failed?: boolean;
  className?: string;
}

export function ToolCall({ label, activeLabel, query, request, result, running,
  open, onOpenChange, failed = false, className }: ToolCallProps) {
  const state = running ? "In progress" : failed ? "Stopped" : "Complete";
  return (
    <Collapsible.Root data-slot="tool-call" open={open} onOpenChange={onOpenChange}
      className={cn("min-w-0 text-xs", className)}>
      <Collapsible.Trigger aria-label={`${label}: ${state}`}
        className="group flex w-full items-center gap-2 rounded-md py-1.5 text-left text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRight aria-hidden className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90 motion-reduce:transition-none" />
        <span className={cn("shrink-0", running && "shimmer motion-reduce:animate-none")}>{running ? activeLabel : label}</span>
        {query && <span className="truncate rounded bg-muted px-1.5 py-0.5 text-[11px]" title={query}>{query}</span>}
        <span className="ml-auto shrink-0">
          {running ? <LoaderCircle aria-hidden className="size-3.5 animate-spin motion-reduce:animate-none" />
            : failed ? <CircleAlert aria-hidden className="size-3.5 text-destructive" />
              : <Check aria-hidden className="size-3.5" />}
        </span>
        <span className="sr-only">{state}</span>
      </Collapsible.Trigger>
      <Collapsible.Content className="mt-1 space-y-3 rounded-lg border bg-muted/30 px-3 py-2.5">
        <div>
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Request</p>
          <p className="break-words whitespace-pre-wrap text-muted-foreground">{request}</p>
        </div>
        <div className="border-t pt-2">
          <p className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Result</p>
          <p className={cn("break-words whitespace-pre-wrap", failed && "text-destructive")} role={failed ? "alert" : undefined}>{result}</p>
        </div>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
