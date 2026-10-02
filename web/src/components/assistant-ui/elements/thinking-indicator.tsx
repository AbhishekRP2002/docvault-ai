"use client";

// Adapted from assistant-ui's MIT-licensed ThinkingIndicator registry source:
// https://github.com/assistant-ui/assistant-ui/blob/main/packages/ui/src/components/react/assistant-ui/elements/thinking-indicator.tsx
// ShimmerLabel/mono are the required subset of its surfaces.tsx helper. Local
// animation utilities and neutral tokens replace the upstream style utilities.
import { useEffect, useState, type ComponentProps } from "react";
import { useAuiState } from "@assistant-ui/react";
import { cn } from "@/lib/utils";

const mono = "font-mono text-[11px] tracking-tight";

function ShimmerLabel({
  active = true,
  className,
  ...props
}: ComponentProps<"span"> & { active?: boolean }) {
  return (
    <span
      className={cn(active && "shimmer motion-reduce:animate-none", className)}
      {...props}
    />
  );
}

export function ThinkingIndicator({
  label,
  elapsed,
  className,
  ...props
}: Omit<ComponentProps<"div">, "children" | "label" | "elapsed"> & {
  label: string;
  elapsed?: string;
}) {
  return (
    <div
      data-slot="thinking-indicator"
      className={cn(
        "flex items-center gap-2.5 text-sm text-muted-foreground",
        className,
      )}
      {...props}
    >
      <span
        aria-hidden
        className="size-1.5 shrink-0 animate-pulse rounded-full bg-primary/60 motion-reduce:animate-none"
      />
      <ShimmerLabel
        key={label}
        className="animate-enter relative inline-block leading-none"
      >
        {label}
      </ShimmerLabel>
      {elapsed !== undefined && (
        <span
          aria-hidden
          className={cn(mono, "tabular-nums text-muted-foreground/60")}
        >
          {elapsed}
        </span>
      )}
    </div>
  );
}

// Runtime-bound adapter: empty text parts count as waiting too, since Python's
// pending messages contain an empty string rather than an absent text part.
export function AssistantThinking({ className }: { className?: string }) {
  const active = useAuiState(
    (s) =>
      s.message.role === "assistant" &&
      s.message.status?.type === "running" &&
      !s.message.parts.some(
        (part) => part.type === "text" && part.text.trim().length > 0,
      ),
  );
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!active) return;
    const start = Date.now();
    setSeconds(0);
    const timer = setInterval(
      () => setSeconds(Math.floor((Date.now() - start) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, [active]);
  if (!active) return null;
  return (
    <ThinkingIndicator
      label="Thinking with your documents"
      elapsed={`${seconds}s`}
      className={className}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    />
  );
}
