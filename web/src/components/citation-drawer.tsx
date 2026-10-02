import { Markdown } from "./markdown";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, FileText, Quote } from "lucide-react";
import { api, apiUrl } from "@/lib/api";
import type { Citation } from "@/lib/types";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "./ui/dialog";
import { Button } from "./ui/button";
import { ErrorState } from "./common";
export function citationLocation(citation: Citation) {
  const l = citation.location;
  return (
    [
      l.page && `Page ${l.page}`,
      l.section,
      l.paragraph && `Paragraph ${l.paragraph}`,
      l.line && `Line ${l.line}`,
    ]
      .filter(Boolean)
      .join(" · ") || "Document excerpt"
  );
}
export function CitationDrawer({
  citation,
  onClose,
}: {
  citation: Citation | null;
  onClose: () => void;
}) {
  const source = useQuery({
    queryKey: ["source", citation?.version_id, citation?.chunk_id],
    queryFn: () =>
      api<Citation>(
        `/v1/versions/${citation!.version_id}/chunks/${citation!.chunk_id}`,
      ),
    enabled: !!citation,
  });
  return (
    <Dialog open={!!citation} onOpenChange={(open) => !open && onClose()}>
      <DialogContent sheet>
        {citation && (
          <>
            <div className="mb-7 flex size-12 items-center justify-center rounded-xl border bg-muted text-primary">
              <Quote className="size-5" />
            </div>
            <DialogTitle>Behind the answer</DialogTitle>
            <DialogDescription>
              Review the original evidence for this citation.
            </DialogDescription>
            <div className="my-7 flex gap-3 border-y py-5">
              <FileText className="mt-1 size-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0">
                <p className="break-words text-sm font-medium">
                  {citation.filename}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Version {citation.version_number} ·{" "}
                  {citationLocation(citation)}
                </p>
              </div>
            </div>
            <div className="rounded-lg border-l-2 border-primary bg-primary/4 p-5 text-sm">
              <Markdown>{source.data?.quote || citation.quote}</Markdown>
            </div>
            {source.error && (
              <div className="mt-4">
                <ErrorState
                  error={source.error}
                  retry={() => void source.refetch()}
                />
              </div>
            )}
            <Button asChild variant="outline" className="mt-6">
              <a
                href={`${apiUrl(`/v1/versions/${citation.version_id}/content`)}${citation.location.page ? `#page=${citation.location.page}` : ""}`}
                target="_blank"
                rel="noopener noreferrer"
              >
                <ExternalLink />
                Open original document
              </a>
            </Button>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
