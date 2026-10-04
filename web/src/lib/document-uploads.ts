import { apiUrl } from "./api";
import type { VaultDocument } from "./types";

export interface UploadRow {
  id: string;
  file: File;
  progress: number;
  error?: string;
}
export interface BatchItem {
  filename: string;
  error: string | null;
  document: VaultDocument | null;
}

/** Keep server-confirmed rows visible immediately, without duplicating live updates. */
export function mergeUploadedDocuments(
  current: VaultDocument[],
  incoming: VaultDocument[],
) {
  const documents = new Map(current.map((doc) => [doc.id, doc]));
  for (const doc of incoming) {
    const existing = documents.get(doc.id);
    if (
      !existing ||
      Date.parse(doc.updated_at) > Date.parse(existing.updated_at)
    ) {
      documents.set(doc.id, doc);
    }
  }
  return [...documents.values()].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  );
}

export function uploadBatch(
  files: File[],
  onProgress: (progress: number) => void,
  xhr = new XMLHttpRequest(),
): Promise<BatchItem[]> {
  return new Promise((resolve, reject) => {
    if (!files.length || files.length > 10) {
      reject(new Error("Choose between one and ten files per upload batch."));
      return;
    }
    if (
      files.reduce((total, file) => total + file.size, 0) >
      100 * 1024 * 1024
    ) {
      reject(new Error("Choose a batch smaller than 100 MiB."));
      return;
    }
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    xhr.open("POST", apiUrl("/v1/document-batches"));
    xhr.setRequestHeader("Idempotency-Key", crypto.randomUUID());
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable)
        onProgress(Math.round((event.loaded / event.total) * 100));
    };
    const interrupted = () =>
      reject(
        new Error("Upload interrupted. Check your connection, then try again."),
      );
    xhr.onerror = interrupted;
    xhr.onabort = interrupted;
    xhr.ontimeout = interrupted;
    xhr.onload = () => {
      try {
        const result = JSON.parse(xhr.responseText);
        if (xhr.status < 200 || xhr.status >= 300) {
          throw new Error(
            result.error?.message || "Upload failed. Please try again.",
          );
        }
        if (
          !Array.isArray(result.items) ||
          result.items.length !== files.length ||
          result.items.some(
            (item: BatchItem) => !item || (!item.error && !item.document?.id),
          )
        ) {
          throw new Error(
            "Could not confirm this upload. Refresh Files before trying again.",
          );
        }
        resolve(result.items);
      } catch (error) {
        reject(
          error instanceof SyntaxError
            ? new Error(
                "Could not confirm this upload. Refresh Files before trying again.",
              )
            : error,
        );
      }
    };
    xhr.send(form);
  });
}
