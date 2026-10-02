import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { DropdownMenu } from "radix-ui";
import { MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { Chat } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ErrorState } from "./common";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { ConfirmDialog } from "./ui/alert-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./ui/dialog";

export function PastRunRow({
  chat,
  active,
  onSelect,
  onDeleted,
}: {
  chat: Chat;
  active: boolean;
  onSelect: () => void;
  onDeleted: (id: string) => void;
}) {
  const client = useQueryClient();
  const trigger = useRef<HTMLButtonElement>(null);
  const [action, setAction] = useState<"rename" | "delete" | null>(null);
  const [name, setName] = useState(chat.title);
  const rename = useMutation({
    mutationFn: (title: string) =>
      api<Chat>(`/v1/chats/${chat.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      }),
    onSuccess: (updated) => {
      client.setQueryData<{ items: Chat[] }>(
        ["chats"],
        (old) =>
          old && {
            items: old.items.map((item) =>
              item.id === updated.id ? updated : item,
            ),
          },
      );
      void client.invalidateQueries({ queryKey: ["chats"] });
      setAction(null);
    },
  });
  const remove = useMutation({
    mutationFn: () => api(`/v1/chats/${chat.id}`, { method: "DELETE" }),
    onSuccess: () => {
      client.setQueryData<{ items: Chat[] }>(
        ["chats"],
        (old) =>
          old && {
            items: old.items.filter((item) => item.id !== chat.id),
          },
      );
      client.removeQueries({ queryKey: ["messages", chat.id] });
      void client.invalidateQueries({ queryKey: ["chats"] });
      setAction(null);
      onDeleted(chat.id);
    },
  });
  function restoreFocus(event: Event) {
    event.preventDefault();
    trigger.current?.focus();
  }
  return (
    <div
      className={cn(
        "group mb-0.5 flex items-center rounded-md",
        active ? "bg-secondary" : "hover:bg-secondary/60",
      )}
    >
      <Button
        variant="ghost"
        onClick={onSelect}
        aria-current={active ? "page" : undefined}
        className={cn(
          "h-8 min-w-0 flex-1 justify-start rounded-r-none pl-8 pr-1 text-[12px] hover:bg-transparent",
          active ? "font-medium" : "font-normal text-muted-foreground",
        )}
      >
        <span className="truncate">{chat.title}</span>
      </Button>
      <DropdownMenu.Root modal={false}>
        <DropdownMenu.Trigger asChild>
          <Button
            ref={trigger}
            variant="ghost"
            size="icon"
            aria-label={`Actions for ${chat.title}`}
            className="mr-1 size-7 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 data-[state=open]:opacity-100 [@media(hover:none)]:opacity-100"
          >
            <MoreHorizontal className="size-4" />
          </Button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="start"
            sideOffset={5}
            onCloseAutoFocus={(event) => {
              if (action) event.preventDefault();
            }}
            className="z-[60] min-w-36 rounded-lg border bg-card p-1 shadow-md"
          >
            <DropdownMenu.Item
              className="flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring/40 data-[highlighted]:bg-secondary"
              onSelect={() => {
                setName(chat.title);
                rename.reset();
                setAction("rename");
              }}
            >
              <Pencil className="size-3.5" />
              Rename
            </DropdownMenu.Item>
            <DropdownMenu.Separator className="my-1 h-px bg-border" />
            <DropdownMenu.Item
              className="flex cursor-pointer items-center gap-2 rounded-md px-2.5 py-2 text-xs text-destructive outline-none focus-visible:ring-2 focus-visible:ring-ring/40 data-[highlighted]:bg-destructive/5"
              onSelect={() => {
                remove.reset();
                setAction("delete");
              }}
            >
              <Trash2 className="size-3.5" />
              Delete
            </DropdownMenu.Item>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>
      <Dialog
        open={action === "rename"}
        onOpenChange={(open) => {
          if (!open && !rename.isPending) setAction(null);
        }}
      >
        <DialogContent onCloseAutoFocus={restoreFocus}>
          <DialogTitle>Rename run</DialogTitle>
          <DialogDescription>
            Give this run a name that is easy to find.
          </DialogDescription>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (name.trim() && !rename.isPending) rename.mutate(name.trim());
            }}
          >
            <Input
              aria-label="Run name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={200}
              disabled={rename.isPending}
              className="my-5"
            />
            {rename.error && <ErrorState error={rename.error} />}
            <div className="mt-5 flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                disabled={rename.isPending}
                onClick={() => setAction(null)}
              >
                Cancel
              </Button>
              <Button disabled={rename.isPending || !name.trim()}>
                {rename.isPending ? "Saving…" : "Save name"}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={action === "delete"}
        onOpenChange={(open) => {
          if (!open && !remove.isPending) setAction(null);
        }}
        title="Delete this run?"
        description={`“${chat.title}” and its message history will be permanently removed. Your files will remain in the library.`}
        pending={remove.isPending}
        error={remove.error}
        onConfirm={() => remove.mutate()}
        onCloseAutoFocus={restoreFocus}
      />
    </div>
  );
}
