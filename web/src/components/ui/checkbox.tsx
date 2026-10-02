import type { ComponentProps } from "react";
import { Checkbox as Primitive } from "radix-ui";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";
export function Checkbox({
  className,
  ...props
}: ComponentProps<typeof Primitive.Root>) {
  return (
    <Primitive.Root
      className={cn(
        "size-4 shrink-0 rounded border border-input bg-card shadow-xs outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40 data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-white",
        className,
      )}
      {...props}
    >
      <Primitive.Indicator className="grid place-content-center">
        <Check className="size-3" />
      </Primitive.Indicator>
    </Primitive.Root>
  );
}
