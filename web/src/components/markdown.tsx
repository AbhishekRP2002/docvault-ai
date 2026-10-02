import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "./ui/button";
import type { Citation } from "@/lib/types";
export function Markdown({
  children,
  citations = [],
  onCitation,
}: {
  children: string;
  citations?: Citation[];
  onCitation?: (citation: Citation) => void;
}) {
  const content = children.replace(
    /\[(?:source\s+)?([a-f0-9-]{32,36})\]/gi,
    (_match, id: string) => {
      const index = citations.findIndex(
        (c) => c.citation_id === id || c.chunk_id === id,
      );
      return index >= 0 ? `[${index + 1}](#citation-${id})` : "[source]";
    },
  );
  return (
    <div className="prose">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children: label, href, ...props }) => {
            if (href?.startsWith("#citation-")) {
              const citation = citations.find(
                (c) =>
                  c.citation_id === href.slice(10) ||
                  c.chunk_id === href.slice(10),
              );
              return (
                <Button
                  size="sm"
                  variant="secondary"
                  className="mx-0.5 inline-flex h-5 min-w-5 px-1 align-baseline text-[10px]"
                  onClick={() => citation && onCitation?.(citation)}
                  aria-label={`View source ${String(label)}`}
                >
                  {label}
                </Button>
              );
            }
            return (
              <a
                {...props}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
              >
                {label}
              </a>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
