"use client";

import { useState, type ComponentProps } from "react";
import { useAuiState, type ToolCallMessagePartComponent } from "@assistant-ui/react";
import { Activity, FileSearch, FileText, Search, type LucideIcon } from "lucide-react";
import { ToolCall } from "./tool-call";
import { ToolTimeline } from "./tool-timeline";

const toolLabels: Record<string, { label: string; activeLabel: string; icon: LucideIcon }> = {
  get_selected_document_overviews: { label: "Read document overviews", activeLabel: "Reading document overviews", icon: FileText },
  retrieve_relevant_chunks: { label: "Searched selected documents", activeLabel: "Searching selected documents", icon: FileSearch },
  search_documents: { label: "Searched the library", activeLabel: "Searching the library", icon: Search },
  get_document_processing_metrics: { label: "Checked processing status", activeLabel: "Checking processing status", icon: Activity },
  get_document_processing_diagnostics: { label: "Read processing diagnostics", activeLabel: "Reading processing diagnostics", icon: Activity },
  get_document_analysis_result: { label: "Read saved analysis", activeLabel: "Reading saved analysis", icon: FileText },
};

/** Show only the safe receipt projected by the backend/runtime, never raw tool output. */
export function DocumentToolCall({ toolName, args, argsText, result, status, isError }:
  Pick<ComponentProps<ToolCallMessagePartComponent>, "toolName" | "args" | "argsText" | "result" | "status" | "isError">) {
  const [open, setOpen] = useState(false);
  const meta = toolLabels[toolName];
  const receipt = result as { source_count?: number; documents?: string[]; error?: string } | undefined;
  const running = status.type === "running";
  const failed = !!isError || status.type === "incomplete";
  const lines = [
    ...(receipt?.documents || []),
    ...(receipt?.source_count !== undefined && ["retrieve_relevant_chunks", "get_selected_document_overviews", "get_document_analysis_result"].includes(toolName)
      ? [`${receipt.source_count} supporting passages`] : []),
    ...(receipt?.error ? [receipt.error] : []),
  ];
  return <ToolCall label={meta?.label || "Read document information"}
    activeLabel={meta?.activeLabel || "Reading document information"}
    query={typeof args.query === "string" ? args.query : typeof args.scope === "string" ? args.scope : ""}
    request={argsText || "{}"}
    result={lines.join("\n") || (running ? "Waiting for the result…" : failed ? "Execution stopped." : "Execution completed.")}
    running={running} failed={failed} open={open} onOpenChange={setOpen} />;
}

/** Group a message's native calls once; nested disclosures use their live per-call state. */
export function AssistantToolTimeline({ defaultOpen = false }: { defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const parts = useAuiState((s) => s.message.parts);
  const calls = parts.filter((part) => part.type === "tool-call");
  const running = calls.some((part) => part.status.type === "running");
  const failed = calls.filter((part) => part.isError || part.status.type === "incomplete").length;
  const active = calls.find((part) => part.status.type === "running");
  return <ToolTimeline
    steps={calls.map((part) => ({
      id: part.toolCallId,
      verb: toolLabels[part.toolName]?.label || "Read document information",
      chip: typeof part.args.query === "string" ? part.args.query : "",
      icon: toolLabels[part.toolName]?.icon || FileText,
      details: <DocumentToolCall {...part} />,
    }))}
    streaming={running} open={open} onOpenChange={setOpen}
    activeLabel={active ? toolLabels[active.toolName]?.activeLabel || "Using tools" : "Using tools"}
    restingLabel={`${calls.length} tool ${calls.length === 1 ? "call" : "calls"}${failed ? ` · ${failed} stopped` : " completed"}`} />;
}
