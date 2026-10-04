import type { ComponentProps } from "react";
import { Select as Primitive } from "radix-ui";
import { Check, ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/utils";

export const Select = Primitive.Root;
export const SelectGroup = Primitive.Group;
export const SelectValue = Primitive.Value;

export function SelectTrigger({
  className,
  children,
  ...props
}: ComponentProps<typeof Primitive.Trigger>) {
  return (
    <Primitive.Trigger
      className={cn(
        "flex h-8 w-full items-center justify-between gap-2 rounded-md border bg-card px-2.5 text-xs shadow-xs outline-none transition-colors hover:bg-muted/60 focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20 disabled:cursor-not-allowed disabled:opacity-50 data-[placeholder]:text-muted-foreground [&>span]:truncate",
        className,
      )}
      {...props}
    >
      {children}
      <Primitive.Icon asChild>
        <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
      </Primitive.Icon>
    </Primitive.Trigger>
  );
}

export function SelectScrollUpButton({
  className,
  ...props
}: ComponentProps<typeof Primitive.ScrollUpButton>) {
  return (
    <Primitive.ScrollUpButton
      className={cn(
        "flex items-center justify-center py-1 text-muted-foreground",
        className,
      )}
      {...props}
    >
      <ChevronUp className="size-3.5" />
    </Primitive.ScrollUpButton>
  );
}

export function SelectScrollDownButton({
  className,
  ...props
}: ComponentProps<typeof Primitive.ScrollDownButton>) {
  return (
    <Primitive.ScrollDownButton
      className={cn(
        "flex items-center justify-center py-1 text-muted-foreground",
        className,
      )}
      {...props}
    >
      <ChevronDown className="size-3.5" />
    </Primitive.ScrollDownButton>
  );
}

export function SelectContent({
  className,
  children,
  position = "popper",
  sideOffset = 5,
  ...props
}: ComponentProps<typeof Primitive.Content>) {
  return (
    <Primitive.Portal>
      <Primitive.Content
        position={position}
        sideOffset={sideOffset}
        className={cn(
          "relative z-[60] max-h-[min(20rem,var(--radix-select-content-available-height))] min-w-36 overflow-hidden rounded-md border bg-card text-foreground shadow-lg animate-enter",
          position === "popper" && "min-w-[var(--radix-select-trigger-width)]",
          className,
        )}
        {...props}
      >
        <SelectScrollUpButton />
        <Primitive.Viewport className="p-1">{children}</Primitive.Viewport>
        <SelectScrollDownButton />
      </Primitive.Content>
    </Primitive.Portal>
  );
}

export function SelectItem({
  className,
  children,
  ...props
}: ComponentProps<typeof Primitive.Item>) {
  return (
    <Primitive.Item
      className={cn(
        "relative flex w-full cursor-default select-none items-center rounded-sm py-1.5 pr-7 pl-2 text-xs outline-none focus:bg-muted data-[state=checked]:font-medium data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
        className,
      )}
      {...props}
    >
      <Primitive.ItemText>{children}</Primitive.ItemText>
      <span className="absolute right-2 flex size-3.5 items-center justify-center">
        <Primitive.ItemIndicator>
          <Check className="size-3.5" />
        </Primitive.ItemIndicator>
      </span>
    </Primitive.Item>
  );
}

export function SelectLabel({
  className,
  ...props
}: ComponentProps<typeof Primitive.Label>) {
  return (
    <Primitive.Label
      className={cn(
        "px-2 py-1.5 text-[11px] font-medium text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

export function SelectSeparator({
  className,
  ...props
}: ComponentProps<typeof Primitive.Separator>) {
  return (
    <Primitive.Separator
      className={cn("-mx-1 my-1 h-px bg-border", className)}
      {...props}
    />
  );
}
