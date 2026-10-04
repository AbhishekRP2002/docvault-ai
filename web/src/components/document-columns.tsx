import { useEffect, useRef, useState } from "react";
import { DropdownMenu } from "radix-ui";
import { Check, Columns3, Copy } from "lucide-react";
import type { VaultDocument } from "@/lib/types";
import { bytes } from "@/lib/utils";
import {
  defaultFileColumns,
  documentCount,
  documentTimestamp,
  fileColumns,
  processingDuration,
  type FileColumn,
} from "@/lib/document-columns";
import { Button } from "./ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "./ui/tooltip";

export function FileColumnsMenu({
  value,
  onChange,
}: {
  value: FileColumn[];
  onChange: (columns: FileColumn[]) => void;
}) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-8"
          aria-label="Choose table columns"
        >
          <Columns3 />
          Columns
        </Button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={5}
          className="z-[60] max-h-[var(--radix-dropdown-menu-content-available-height)] w-52 overflow-y-auto rounded-md border bg-card p-1 shadow-md"
        >
          <DropdownMenu.Label className="px-2 py-2 text-[11px] text-muted-foreground">
            Display columns
          </DropdownMenu.Label>
          {fileColumns.map((column) => (
            <DropdownMenu.CheckboxItem
              key={column.id}
              checked={value.includes(column.id)}
              onSelect={(event) => event.preventDefault()}
              onCheckedChange={(checked) =>
                onChange(
                  checked
                    ? [...value, column.id]
                    : value.filter((id) => id !== column.id),
                )
              }
              className="relative cursor-default rounded-sm py-1.5 pr-2 pl-7 text-xs outline-none data-[highlighted]:bg-muted"
            >
              <span className="absolute left-2 top-2">
                <DropdownMenu.ItemIndicator>
                  <Check className="size-3" />
                </DropdownMenu.ItemIndicator>
              </span>
              {column.label}
            </DropdownMenu.CheckboxItem>
          ))}
          <DropdownMenu.Separator className="my-1 h-px bg-border" />
          <DropdownMenu.Item
            onSelect={() => onChange([...defaultFileColumns])}
            className="cursor-default rounded-sm px-2 py-1.5 text-xs outline-none data-[highlighted]:bg-muted"
          >
            Reset to default
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function RunId({ value }: { value: string }) {
  const [feedback, setFeedback] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setFeedback("copied");
    } catch {
      setFeedback("failed");
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setFeedback(null), 2500);
  }
  return (
    <div>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 max-w-full gap-1.5 px-1 font-mono text-[11px] font-normal"
              aria-label={`Copy run ID ${value}`}
              onClick={() => void copy()}
            >
              <span className="truncate">{value.slice(0, 8)}…</span>
              {feedback === "copied" ? (
                <Check className="size-3" />
              ) : (
                <Copy className="size-3 text-muted-foreground" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{value}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      {feedback && (
        <span
          role="status"
          className={
            feedback === "failed"
              ? "block text-[10px] text-destructive"
              : "sr-only"
          }
        >
          {feedback === "copied" ? "Run ID copied" : "Copy failed"}
        </span>
      )}
    </div>
  );
}

export function DocumentColumnValue({
  column,
  document,
}: {
  column: FileColumn;
  document: VaultDocument;
}) {
  const processing = document.processing;
  switch (column) {
    case "run":
      return processing?.run_id ? <RunId value={processing.run_id} /> : "—";
    case "started":
      return <Timestamp value={processing?.started_at} />;
    case "finished":
      return <Timestamp value={processing?.finished_at} />;
    case "added":
      return <Timestamp value={document.created_at} />;
    case "duration":
      return (
        <span
          title={
            processing?.status === "running"
              ? "Elapsed time in the current attempt"
              : "Duration of the latest attempt; excludes time waiting in the queue"
          }
        >
          {processingDuration(processing?.duration_ms)}
        </span>
      );
    case "pages":
      return document.page_count == null
        ? "—"
        : document.page_count.toLocaleString();
    case "chunks":
      return documentCount(document, "chunk_count");
    case "tokens":
      return documentCount(document, "token_count");
    case "attempts":
      return processing?.attempts ?? "—";
    case "size":
      return bytes(document.size_bytes);
    case "parser":
      return (
        <span className="block truncate" title={document.parser || undefined}>
          {document.parser || "—"}
        </span>
      );
    case "embedding":
      return (
        <span
          className="block truncate"
          title={document.embedding_model || undefined}
        >
          {document.embedding_model || "—"}
        </span>
      );
  }
}

function Timestamp({ value }: { value: string | null | undefined }) {
  const valid = !!value && Number.isFinite(Date.parse(value));
  return (
    <time
      dateTime={valid ? value : undefined}
      title={
        valid
          ? new Date(value).toLocaleString(undefined, {
              dateStyle: "full",
              timeStyle: "long",
            })
          : undefined
      }
    >
      {documentTimestamp(value)}
    </time>
  );
}
