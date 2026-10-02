import { ThreadPrimitive } from "@assistant-ui/react";
import { ArrowDown } from "lucide-react";
import { cn } from "@/lib/utils";
import { TooltipIconButton } from "./tooltip-icon-button";

export function ScrollToBottom({ className }: { className?: string }) {
  return (
    <ThreadPrimitive.ScrollToBottom asChild>
      <TooltipIconButton
        tooltip="Scroll to latest answer"
        variant="outline"
        className={cn(
          "size-9 rounded-full bg-card shadow-sm disabled:invisible",
          className,
        )}
      >
        <ArrowDown className="size-4" aria-hidden="true" />
      </TooltipIconButton>
    </ThreadPrimitive.ScrollToBottom>
  );
}
