import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";
export function NativeSelect({
  className,
  ...props
}: ComponentProps<"select">) {
  return (
    <select
      className={cn(
        "h-9 rounded-md border bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}
