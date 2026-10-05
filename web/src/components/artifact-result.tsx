import { Markdown } from "./markdown";
import { useQuery } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { api } from "@/lib/api";
import type {
  Artifact,
  ComparisonSource,
  Insights,
  VaultDocument,
} from "@/lib/types";
import { ErrorState } from "./common";
import { EvidenceLinks } from "./evidence-links";
interface ComparisonRow {
  dimension: string;
  cells: {
    version_id: string;
    text: string;
    status: string;
    citation_ids: string[];
  }[];
}
export function ArtifactResult({
  id,
  versionId,
  documents = [],
  sources = [],
}: {
  id: string;
  versionId?: string;
  documents?: VaultDocument[];
  sources?: ComparisonSource[];
}) {
  const query = useQuery({
    queryKey: ["artifact", id],
    queryFn: () => api<Artifact>(`/v1/artifacts/${id}`),
    refetchInterval: (q) =>
      !q.state.data ||
      ["queued", "processing", "running", "pending"].includes(
        q.state.data.status,
      )
        ? 2500
        : false,
  });
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  if (
    !query.data ||
    ["queued", "processing", "running", "pending"].includes(query.data.status)
  )
    return (
      <div className="flex items-center gap-3 rounded-lg bg-muted p-5 text-sm text-muted-foreground">
        <LoaderCircle className="size-4 animate-spin" />
        Working through your documents…
      </div>
    );
  if (query.data.error || query.data.status === "failed")
    return (
      <ErrorState
        error={query.data.error || "Could not generate this result."}
      />
    );
  const data = query.data.data;
  if (!data) return <ErrorState error="The result did not include content." />;
  const coverage = data.coverage as
    | { complete?: boolean; chunks_processed?: number; total_chunks?: number }
    | undefined;
  if (Array.isArray(data.rows)) {
    const rows = data.rows as ComparisonRow[];
    const ids = data.version_ids as string[];
    return (
      <div className="space-y-5 animate-enter">
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-left text-xs">
            <thead className="border-b bg-muted">
              <tr>
                <th className="min-w-28 p-3 font-medium">Dimension</th>
                {ids.map((version, index) => {
                  const source = sources.find(
                    (item) => item.version_id === version,
                  );
                  const document = documents.find(
                    (item) =>
                      item.current_version_id === version ||
                      item.latest_version_id === version,
                  );
                  return (
                    <th key={version} className="min-w-48 p-3 font-medium">
                      {source?.filename ||
                        document?.title ||
                        `Document ${index + 1}`}
                      {source?.version_number != null && (
                        <span className="mt-1 block text-[10px] font-normal text-muted-foreground">
                          Version {source.version_number}
                        </span>
                      )}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((row, index) => (
                <tr key={index}>
                  <th className="p-3 align-top font-medium">{row.dimension}</th>
                  {ids.map((version) => {
                    const cell = row.cells.find(
                      (c) => c.version_id === version,
                    );
                    return (
                      <td key={version} className="p-3 align-top leading-6">
                        {cell ? (
                          <>
                            <Markdown>{cell.text}</Markdown>
                            {cell.status === "not_found" && (
                              <p className="mt-1 text-[10px] text-muted-foreground">
                                Not found in this document
                              </p>
                            )}
                            <EvidenceLinks
                              ids={cell.citation_ids}
                              versionId={version}
                            />
                          </>
                        ) : (
                          "No result"
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {coverage && (
          <p className="text-xs text-muted-foreground">
            {coverage.complete
              ? "All document sections were included in the analysis."
              : "Analysis has partial document coverage."}
          </p>
        )}
      </div>
    );
  }
  const summary = data as unknown as Insights & { citation_ids?: string[] };
  return (
    <div className="space-y-5 text-sm animate-enter">
      <Markdown>{summary.summary || "No summary was returned."}</Markdown>
      {versionId && (
        <EvidenceLinks ids={summary.citation_ids || []} versionId={versionId} />
      )}
      {!!summary.key_insights?.length && (
        <section>
          <h3 className="mb-3 text-xs font-semibold text-muted-foreground">
            KEY INSIGHTS
          </h3>
          <ul className="space-y-4">
            {summary.key_insights.map((item, index) => (
              <li key={index} className="border-l-2 pl-4">
                <p className="leading-6">{item.text}</p>
                {versionId && (
                  <EvidenceLinks
                    ids={item.citation_ids}
                    versionId={versionId}
                  />
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      {coverage && (
        <p className="border-t pt-4 text-xs text-muted-foreground">
          {coverage.complete
            ? "All document sections were included in the analysis."
            : "Analysis has partial document coverage."}
        </p>
      )}
    </div>
  );
}
