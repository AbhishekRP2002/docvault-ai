import { useCallback, useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "./ui/button";
import type { Citation } from "@/lib/types";

const EMPTY_CITATIONS: Citation[] = [];

/** Citation transforms are shared by static documents and runtime chat parts. */
export function citationMarkdown(text: string, citations: Citation[]) {
  return text.replace(
    /\[(?:source\s+)?([a-f0-9-]{32,36})\]/gi,
    (_match, id: string) => {
      const index = citations.findIndex(
        (citation) => citation.citation_id === id || citation.chunk_id === id,
      );
      return index >= 0 ? `[${index + 1}](#citation-${id})` : "[source]";
    },
  );
}

export function useCitationMarkdown(
  citations: Citation[] = EMPTY_CITATIONS,
  onCitation?: (citation: Citation) => void,
) {
  const preprocess = useCallback(
    (text: string) => citationMarkdown(text, citations),
    [citations],
  );
  const components = useMemo<Components>(
    () => ({
      a: ({ children: label, href, node: _node, ...props }) => {
        if (href?.startsWith("#citation-")) {
          const index = citations.findIndex(
            (citation) =>
              citation.citation_id === href.slice(10) ||
              citation.chunk_id === href.slice(10),
          );
          const citation = citations[index];
          return citation && onCitation ? (
            <Button
              type="button"
              size="sm"
              variant="secondary"
              className="mx-0.5 inline-flex h-5 min-w-5 px-1 align-baseline text-[10px]"
              onClick={() => onCitation(citation)}
              aria-label={`View source ${index + 1}`}
            >
              {index + 1}
            </Button>
          ) : (
            <span>{label}</span>
          );
        }
        return (
          <a {...props} href={href} target="_blank" rel="noopener noreferrer">
            {label}
          </a>
        );
      },
    }),
    [citations, onCitation],
  );
  return { preprocess, components };
}

export function Markdown({
  children,
  citations,
  onCitation,
}: {
  children: string;
  citations?: Citation[];
  onCitation?: (citation: Citation) => void;
}) {
  const { preprocess, components } = useCitationMarkdown(citations, onCitation);
  return (
    <div className="prose">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {preprocess(children)}
      </ReactMarkdown>
    </div>
  );
}
