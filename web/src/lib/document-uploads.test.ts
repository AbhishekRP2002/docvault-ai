import { describe, expect, test } from "bun:test";
import { mergeUploadedDocuments, uploadBatch } from "./document-uploads";
import type { VaultDocument } from "./types";

function stored(
  id: string,
  status: VaultDocument["status"] = "queued",
): VaultDocument {
  return {
    id,
    status,
    title: "Same filename",
    filename: "same.txt",
    mime_type: "text/plain",
    size_bytes: 12,
    created_at: "2026-10-05T10:00:00Z",
    updated_at: "2026-10-05T10:00:00Z",
    current_version_id: null,
    latest_version_id: `v-${id}`,
    version_number: 1,
    page_count: null,
    chunk_count: 0,
    insight_status: "pending",
    summary: null,
    category: null,
    tags: [],
    error: null,
  };
}
function transport() {
  const xhr = {
    upload: {
      onprogress: null as
        | ((event: {
            lengthComputable: boolean;
            loaded: number;
            total: number;
          }) => void)
        | null,
    },
    status: 202,
    responseText: "",
    headers: {} as Record<string, string>,
    sent: null as FormData | null,
    open() {},
    setRequestHeader(key: string, value: string) {
      this.headers[key] = value;
    },
    send(body: FormData) {
      this.sent = body;
    },
    onload: null as (() => void) | null,
    onerror: null as (() => void) | null,
  };
  return { xhr, request: xhr as unknown as XMLHttpRequest };
}

describe("document uploads", () => {
  test("reports transfer progress then returns per-file persisted results, including partial failure", async () => {
    const { xhr, request } = transport();
    const progress: number[] = [];
    const files = [
      new File(["one"], "same.txt"),
      new File(["two"], "same.txt"),
    ];
    const pending = uploadBatch(
      files,
      (value) => progress.push(value),
      request,
    );
    expect(xhr.sent?.getAll("files")).toHaveLength(2);
    expect(xhr.headers["Idempotency-Key"]).toBeTruthy();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 });
    expect(progress).toEqual([50]);
    xhr.responseText = JSON.stringify({
      items: [
        { filename: "same.txt", document: stored("a"), error: null },
        { filename: "same.txt", document: null, error: "Unsupported document" },
      ],
    });
    xhr.onload?.();
    const result = await pending;
    expect(result[0].document?.status).toBe("queued");
    expect(result[1].error).toBe("Unsupported document");
  });
  test("network failure does not become an accepted upload", async () => {
    const { xhr, request } = transport();
    const pending = uploadBatch(
      [new File(["file"], "a.txt")],
      () => {},
      request,
    );
    xhr.onerror?.();
    await expect(pending).rejects.toThrow("Upload interrupted");
  });
  test("rejects malformed or incomplete success responses", async () => {
    for (const response of [
      "not-json",
      '{"items":[]}',
      '{"items":[{"filename":"a.txt"}]}',
    ]) {
      const { xhr, request } = transport();
      const pending = uploadBatch(
        [new File(["file"], "a.txt")],
        () => {},
        request,
      );
      xhr.responseText = response;
      xhr.onload?.();
      await expect(pending).rejects.toThrow("Could not confirm");
    }
  });
  test("preserves server validation errors and enforces the batch limit", async () => {
    const { xhr, request } = transport();
    const pending = uploadBatch(
      [new File(["file"], "a.txt")],
      () => {},
      request,
    );
    xhr.status = 413;
    xhr.responseText = '{"error":{"message":"File too large"}}';
    xhr.onload?.();
    await expect(pending).rejects.toThrow("File too large");
    const tooMany = transport();
    await expect(
      uploadBatch(
        Array.from({ length: 11 }, () => new File(["x"], "x.txt")),
        () => {},
        tooMany.request,
      ),
    ).rejects.toThrow("ten files");
    expect(tooMany.xhr.sent).toBeNull();
  });
  test("accepted files appear immediately and deduplicate by id, not filename", () => {
    const rows = mergeUploadedDocuments(
      [stored("a")],
      [stored("a"), stored("b")],
    );
    expect(rows.map((row) => row.id)).toEqual(["a", "b"]);
  });
  test("a late upload response cannot overwrite a newer processing event", () => {
    const ready = {
      ...stored("a", "ready"),
      updated_at: "2026-10-05T10:00:05Z",
    };
    expect(mergeUploadedDocuments([ready], [stored("a")])[0].status).toBe(
      "ready",
    );
    expect(
      mergeUploadedDocuments([stored("a", "ready")], [stored("a")])[0].status,
    ).toBe("ready");
  });
});
