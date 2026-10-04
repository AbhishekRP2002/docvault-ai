import { describe, expect, test } from "bun:test";
import {
  defaultFileColumns,
  documentCount,
  documentTimestamp,
  parseFileColumns,
  processingDuration,
} from "./document-columns";
import type { VaultDocument } from "./types";

describe("processing table values", () => {
  test("keeps unknown timing distinct from a recorded zero", () => {
    expect(processingDuration(null)).toBe("—");
    expect(processingDuration(undefined)).toBe("—");
    expect(processingDuration(NaN)).toBe("—");
    expect(processingDuration(-50)).toBe("—");
    expect(processingDuration(0)).toBe("0 ms");
    expect(processingDuration(425)).toBe("425 ms");
    expect(processingDuration(1250)).toBe("1.3 s");
    expect(processingDuration(125_000)).toBe("2m 5s");
    expect(processingDuration(3_660_000)).toBe("1h 1m");
  });
  test("unstarted documents do not display output counts as measured zeros", () => {
    const queued = {
      status: "queued",
      parser: null,
      chunk_count: 0,
      token_count: 0,
    } as VaultDocument;
    expect(documentCount(queued, "chunk_count")).toBe("—");
    expect(documentCount({ ...queued, status: "failed" }, "token_count")).toBe(
      "—",
    );
    expect(documentCount({ ...queued, status: "ready" }, "chunk_count")).toBe(
      "0",
    );
    expect(documentCount({ ...queued, parser: "docling" }, "token_count")).toBe(
      "0",
    );
    expect(documentCount({ ...queued, chunk_count: 17 }, "chunk_count")).toBe(
      "17",
    );
  });
  test("missing and invalid timestamps remain unknown", () => {
    expect(documentTimestamp(null)).toBe("—");
    expect(documentTimestamp("invalid timestamp")).toBe("—");
    expect(documentTimestamp("2026-10-05T10:30:15Z")).not.toBe("—");
  });
  test("restores supported preferences and safely handles corrupted storage", () => {
    expect(parseFileColumns(null)).toEqual(defaultFileColumns);
    expect(parseFileColumns("invalid json")).toEqual(defaultFileColumns);
    expect(parseFileColumns('{"run":true}')).toEqual(defaultFileColumns);
    expect(parseFileColumns('["run","run","unknown","chunks"]')).toEqual([
      "run",
      "chunks",
    ]);
    expect(parseFileColumns("[]")).toEqual([]);
  });
});
