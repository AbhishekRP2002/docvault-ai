"use client";

// Adapted from assistant-ui's MIT-licensed ToolTimeline element. Each step may
// contain a ToolCall disclosure, and activity follows its actual execution state.
// https://www.assistant-ui.com/elements/tool-timeline
import { ChevronRight, type LucideIcon } from "lucide-react";
import { Collapsible } from "radix-ui";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface TimelineStep {
  id: string;
  verb: string;
  chip: string;
  icon: LucideIcon;
  details?: ReactNode;
}

export interface ToolTimelineProps {
  steps: readonly TimelineStep[];
  streaming: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  restingLabel: string;
  activeLabel: string;
  className?: string;
}

export function ToolTimeline({ steps, streaming, open, onOpenChange,
  restingLabel, activeLabel, className }: ToolTimelineProps) {
  if (steps.length === 0) return null;
  return (
    <Collapsible.Root data-slot="tool-timeline" open={open} onOpenChange={onOpenChange}
      className={cn("my-3 min-w-0", className)}>
      <Collapsible.Trigger className="group flex items-center gap-1.5 rounded-md py-1 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <ChevronRight aria-hidden className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90 motion-reduce:transition-none" />
        <span className={cn(streaming && "shimmer motion-reduce:animate-none")}>{streaming ? activeLabel : restingLabel}</span>
      </Collapsible.Trigger>
      <Collapsible.Content className="ml-1.5 mt-1 space-y-1 border-l pl-3">
        {steps.map((step) => {
          const Icon = step.icon;
          return <div key={step.id}>
            {step.details ?? <div className="flex items-center gap-2 py-1.5 text-xs text-muted-foreground">
              <Icon aria-hidden className="size-3.5 shrink-0" />
              <span>{step.verb}</span>
              <span className="truncate rounded bg-muted px-1.5 py-0.5">{step.chip}</span>
            </div>}
          </div>;
        })}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
