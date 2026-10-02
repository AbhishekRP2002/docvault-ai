import type { ComponentProps, ReactNode } from "react";
import { Dialog as Primitive } from "radix-ui";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
export const Dialog = Primitive.Root;
export const DialogTrigger = Primitive.Trigger;
export const DialogClose = Primitive.Close;
export function DialogTitle(props: ComponentProps<typeof Primitive.Title>) {
  return (
    <Primitive.Title
      className="text-lg font-semibold tracking-tight"
      {...props}
    />
  );
}
export function DialogDescription(
  props: ComponentProps<typeof Primitive.Description>,
) {
  return (
    <Primitive.Description
      className="mt-1 text-sm leading-relaxed text-muted-foreground"
      {...props}
    />
  );
}
export function DialogContent({
  children,
  className,
  sheet = false,
  ...props
}: ComponentProps<typeof Primitive.Content> & {
  sheet?: boolean;
  children: ReactNode;
}) {
  return (
    <Primitive.Portal>
      <Primitive.Overlay className="fixed inset-0 z-50 bg-foreground/25 backdrop-blur-[2px]" />
      <Primitive.Content
        className={cn(
          "fixed z-50 bg-card p-6 shadow-xl outline-none animate-enter",
          sheet
            ? "inset-y-0 right-0 w-full max-w-xl overflow-y-auto border-l"
            : "left-1/2 top-1/2 max-h-[90dvh] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border",
          className,
        )}
        {...props}
      >
        {children}
        <Primitive.Close className="absolute right-5 top-5 rounded-md p-1 text-muted-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring">
          <X className="size-4" />
          <span className="sr-only">Close</span>
        </Primitive.Close>
      </Primitive.Content>
    </Primitive.Portal>
  );
}
