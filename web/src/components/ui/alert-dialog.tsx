import { AlertDialog as Primitive } from "radix-ui";
import { Button, buttonVariants } from "./button";
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  pending,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  title: string;
  description: string;
  pending?: boolean;
  onConfirm: () => void;
}) {
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      <Primitive.Portal>
        <Primitive.Overlay className="fixed inset-0 z-50 bg-foreground/25 backdrop-blur-[2px]" />
        <Primitive.Content className="fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-xl border bg-card p-6 shadow-xl">
          <Primitive.Title className="text-lg font-semibold">
            {title}
          </Primitive.Title>
          <Primitive.Description className="mt-2 text-sm leading-relaxed text-muted-foreground">
            {description}
          </Primitive.Description>
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
