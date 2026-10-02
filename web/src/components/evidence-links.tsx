import { useState } from "react";
import { FileText, LoaderCircle } from "lucide-react";
import { api } from "@/lib/api";
import type { Citation } from "@/lib/types";
import { Button } from "./ui/button";
import { CitationDrawer } from "./citation-drawer";
import { errorMessage } from "@/lib/utils";
export function EvidenceLinks({
  ids,
  versionId,
}: {
  ids: string[];
  versionId: string;
}) {
  const [citation, setCitation] = useState<Citation | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function open(id: string) {
    setLoading(id);
    setError(null);
    try {
      setCitation(
        await api<Citation>(`/v1/versions/${versionId}/chunks/${id}`),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(null);
    }
  }
  if (!ids?.length) return null;
  return (
    <>
      <div className="mt-2 flex flex-wrap gap-1">
        {[...new Set(ids)].map((id, index) => (
          <Button
            key={id}
            variant="ghost"
            size="sm"
            className="h-6 gap-1 px-1.5 text-[10px] font-normal text-primary"
            disabled={!!loading}
            onClick={() => void open(id)}
          >
            {loading === id ? (
              <LoaderCircle className="size-3 animate-spin" />
            ) : (
              <FileText className="size-3" />
            )}
            Source {index + 1}
          </Button>
        ))}
      </div>
      {error && (
        <p role="alert" className="mt-1 text-xs text-destructive">
          {error}
        </p>
      )}
      <CitationDrawer citation={citation} onClose={() => setCitation(null)} />
    </>
  );
}
