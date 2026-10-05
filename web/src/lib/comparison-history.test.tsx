import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ComparisonHistoryList } from "../components/comparison-history";
import { ArtifactResult } from "../components/artifact-result";
import type { Artifact, ComparisonRun } from "./types";

const run: ComparisonRun = {
  id: "comparison-1",
  status: "ready",
  created_at: "2026-10-05T00:00:00Z",
  version_ids: ["old-version", "other-version"],
  dimensions: ["Price"],
  error: null,
  sources: [
    {
      version_id: "old-version",
      title: "Terms",
      filename: "terms-v1.txt",
      version_number: 1,
      available: true,
    },
    {
      version_id: "other-version",
      title: "Pricing",
      filename: "pricing.txt",
      version_number: 2,
      available: true,
    },
  ],
};

describe("saved comparison presentation", () => {
  test("saved runs expose their documents, dimensions and distinct outcomes", () => {
    const html = renderToStaticMarkup(
      <ComparisonHistoryList
        items={[
          run,
          { ...run, id: "pending", status: "pending" },
          { ...run, id: "failed", status: "failed", error: "Provider timeout" },
        ]}
        onOpen={() => {}}
      />,
    );
    expect(html).toContain("terms-v1.txt");
    expect(html).toContain("Price");
    expect(html).toContain("Completed");
    expect(html).toContain("Processing");
    expect(html).toContain("Failed");
    expect(html).toContain("Provider timeout");
    expect(html).not.toContain('disabled=""');
  });

  test("deleted-source history remains visible but cannot open an unavailable result", () => {
    const html = renderToStaticMarkup(
      <ComparisonHistoryList
        items={[
          {
            ...run,
            sources: [
              {
                ...run.sources[0],
                available: false,
                filename: null,
                title: "Deleted document",
              },
              run.sources[1],
            ],
          },
        ]}
        onOpen={() => {}}
      />,
    );
    expect(html).toContain("Deleted document");
    expect(html).toContain("Source unavailable");
    expect(html).toContain('disabled=""');
    expect(html).not.toContain("terms-v1.txt");
  });

  test("reopened results label original versions without relying on the current document library", () => {
    const client = new QueryClient();
    const artifact: Artifact = {
      id: run.id,
      status: "ready",
      error: null,
      data: {
        version_ids: run.version_ids,
        rows: [
          {
            dimension: "Price",
            cells: [
              {
                version_id: "old-version",
                text: "USD 1200",
                status: "found",
                citation_ids: [],
              },
            ],
          },
        ],
      },
    };
    client.setQueryData(["artifact", run.id], artifact);
    try {
      const html = renderToStaticMarkup(
        <QueryClientProvider client={client}>
          <ArtifactResult id={run.id} sources={run.sources} />
        </QueryClientProvider>,
      );
      expect(html).toContain("terms-v1.txt");
      expect(html).toContain("Version 1");
      expect(html).toContain("Version 2");
      expect(html).toContain("USD 1200");
      expect(html).not.toContain("Document 1");
    } finally {
      client.clear();
    }
  });
});
