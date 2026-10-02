import { AlertDialog as Primitive } from "radix-ui";
import { Button, buttonVariants } from "./button";
import { ErrorState } from "../common";
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  pending,
  onConfirm,
  error,
  onCloseAutoFocus,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  title: string;
  description: string;
  pending?: boolean;
  onConfirm: () => void;
  error?: unknown;
  onCloseAutoFocus?: (event: Event) => void;
}) {
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      <Primitive.Portal>
        <Primitive.Overlay className="fixed inset-0 z-50 bg-foreground/25 backdrop-blur-[2px]" />
        <Primitive.Content
          onCloseAutoFocus={onCloseAutoFocus}
          className="fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-card p-6 shadow-xl"
        >
          <Primitive.Title className="text-lg font-semibold">
            {title}
          </Primitive.Title>
          <Primitive.Description className="mt-2 text-sm leading-relaxed text-muted-foreground">
            {description}
          </Primitive.Description>
          {!!error && (
            <div className="mt-4">
              <ErrorState error={error} />
            </div>
          )}
          <div className="mt-6 flex justify-end gap-2">
            <Primitive.Cancel
              className={buttonVariants({ variant: "outline" })}
              disabled={pending}
            >
              Cancel
            </Primitive.Cancel>
            <Button
              variant="destructive"
              disabled={pending}
              onClick={onConfirm}
            >
              {pending ? "Deleting…" : "Delete"}
            </Button>
          </div>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
