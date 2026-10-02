// Adapted from assistant-ui's MIT-licensed MarkdownText registry component:
// https://github.com/assistant-ui/assistant-ui/blob/main/packages/ui/src/components/react/assistant-ui/elements/markdown-text.tsx
// Keep the existing prose typography; use the runtime for smoothing and deferral.
import {
  createContext,
  memo,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useAuiState } from "@assistant-ui/react";
import {
  MarkdownTextPrimitive,
  type CodeHeaderProps,
  useIsMarkdownCodeBlock,
} from "@assistant-ui/react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy } from "lucide-react";
import { useCitationMarkdown } from "@/components/markdown";
import type { Citation, Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import { TooltipIconButton } from "./tooltip-icon-button";

export const MarkdownCitationContext = createContext<
  ((citation: Citation) => void) | undefined
>(undefined);

export function CodeHeader({ language, code }: CodeHeaderProps) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  async function copy() {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setCopyError(false);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
      setCopyError(true);
    }
  }

  return (
    <div className="rounded-t-lg border border-b-0 bg-muted/70 px-3 py-1.5">
      <div className="flex items-center justify-between gap-3">
        <span className="font-mono text-[11px] text-muted-foreground">
          {language || "text"}
        </span>
        <TooltipIconButton
          tooltip={copied ? "Copied code" : "Copy code"}
          disabled={!code}
          onClick={() => void copy()}
          className="size-6 text-muted-foreground"
        >
          {copied ? (
            <Check className="size-3.5" />
          ) : (
            <Copy className="size-3.5" />
          )}
        </TooltipIconButton>
      </div>
      {copyError && (
        <p role="alert" className="mt-1 text-xs text-destructive">
          Could not copy. Select the code to copy manually.
        </p>
      )}
    </div>
  );
}

const codeComponents = {
  CodeHeader,
  pre: ({ className, node: _node, ...props }) => (
    <pre className={cn("!mt-0 !rounded-t-none", className)} {...props} />
  ),
  code: function Code({ className, node: _node, ...props }) {
    const isBlock = useIsMarkdownCodeBlock();
    return (
      <code
        className={cn(!isBlock && "rounded bg-muted px-1 py-0.5", className)}
        {...props}
      />
    );
  },
} satisfies Components & { CodeHeader: typeof CodeHeader };

export const MarkdownText = memo(function MarkdownText() {
  const message = useAuiState(
    (state) => state.message.metadata.custom.docvault as Message | undefined,
  );
  const onCitation = useContext(MarkdownCitationContext);
  const { preprocess, components: citationComponents } = useCitationMarkdown(
    message?.citations,
    onCitation,
  );
  const components = useMemo(
    () => ({ ...codeComponents, ...citationComponents }),
    [citationComponents],
  );
  return (
    <MarkdownTextPrimitive
      className="prose"
      remarkPlugins={[remarkGfm]}
      components={components}
      preprocess={preprocess}
      smooth
      defer
    />
  );
});
