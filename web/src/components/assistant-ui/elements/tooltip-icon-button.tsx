// Radix-compatible composition of assistant-ui's TooltipIconButton pattern:
// https://www.assistant-ui.com/elements/tooltip-icon-button
import type { ComponentProps } from "react";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type TooltipIconButtonProps = ComponentProps<typeof Button> & {
  tooltip: string;
  side?: ComponentProps<typeof TooltipContent>["side"];
};

export function TooltipIconButton({
  tooltip,
  side = "top",
  children,
  className,
  ...props
}: TooltipIconButtonProps) {
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className={cn("size-7 p-1", className)}
            {...props}
          >
            {children}
            <span className="sr-only">{tooltip}</span>
          </Button>
        </TooltipTrigger>
        <TooltipContent side={side}>{tooltip}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
