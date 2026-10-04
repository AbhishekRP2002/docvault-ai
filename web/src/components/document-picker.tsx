import { useState } from "react";
import { Check, FileText, Search } from "lucide-react";
import type { VaultDocument } from "@/lib/types";
import { Input } from "./ui/input";
import { Button } from "./ui/button";
import { Checkbox } from "./ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./ui/dialog";
import { cn } from "@/lib/utils";
import { StatusBadge } from "./common";
export function DocumentPicker({
  open,
  onOpenChange,
  documents,
  selected,
  onSave,
  saving,
  error,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  documents: VaultDocument[];
  selected: string[];
  onSave: (ids: string[]) => void;
  saving?: boolean;
  error?: string;
}) {
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState(selected);
  const filtered = documents.filter((d) =>
    `${d.title} ${d.filename}`.toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-xl"
        onOpenAutoFocus={() => {
          setDraft(selected);
          setQuery("");
        }}
      >
        <DialogTitle>Choose your sources</DialogTitle>
        <DialogDescription>
          Select the documents this conversation can use. You can change these
          between questions.
        </DialogDescription>
        <div className="relative my-5">
          <Search className="absolute left-3 top-3 size-4 text-muted-foreground" />
          <Input
            autoFocus
            className="h-10 pl-9"
            placeholder="Search your documents…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search documents"
          />
        </div>
        <div className="mb-2 flex justify-between text-xs text-muted-foreground">
          <span>{draft.length} selected</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={() =>
              setDraft(
                draft.length
                  ? []
                  : documents.flatMap((d) =>
                      d.current_version_id
                        ? [d.current_version_id]
                        : [],
                    ),
              )
            }
          >
            {draft.length ? "Clear selection" : "Select all ready"}
          </Button>
        </div>
        <div className="max-h-[45dvh] space-y-1 overflow-y-auto">
          {filtered.map((doc) => {
            const id = doc.current_version_id;
            const ready = !!id;
            const checked = !!id && draft.includes(id);
            return (
              <label
                key={doc.id}
                className={cn(
                  "flex cursor-pointer items-center gap-3 rounded-lg border p-3 transition-colors",
                  checked
                    ? "border-primary/25 bg-primary/5"
                    : "border-transparent hover:bg-muted",
                  !ready && "cursor-not-allowed opacity-60",
                )}
              >
                <Checkbox
                  checked={checked}
                  disabled={!ready || saving}
                  onCheckedChange={() =>
                    id &&
                    setDraft(
                      checked ? draft.filter((v) => v !== id) : [...draft, id],
                    )
                  }
                  aria-label={`Select ${doc.title}`}
                />
                <FileText className="size-5 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">
                    {doc.title}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {doc.status === "ready"
                      ? `Version ${doc.version_number}`
                      : "Current ready version"}
                  </span>
                </span>
                <StatusBadge status={doc.status} />
              </label>
            );
          })}
          {!filtered.length && (
            <p className="py-10 text-center text-sm text-muted-foreground">
              {documents.length
                ? "No documents match your search."
                : "Upload documents in the library to get started."}
            </p>
          )}
        </div>
        {error && (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {error}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2 border-t pt-4">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={saving || !draft.length}
            onClick={() => onSave(draft)}
          >
            <Check />
            {saving ? "Saving…" : "Use selected documents"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
