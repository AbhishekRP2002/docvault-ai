import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  mergeUploadedDocuments,
  uploadBatch,
  type UploadRow,
} from "@/lib/document-uploads";
import type { VaultDocument } from "@/lib/types";
import { errorMessage } from "@/lib/utils";

/** Lives in the app shell so navigating to Usage never loses an active transfer. */
export function useDocumentUploads() {
  const client = useQueryClient();
  const busy = useRef(false);
  const [uploading, setUploading] = useState(false);
  const [uploads, setUploads] = useState<UploadRow[]>([]);

  async function upload(files: FileList | File[]) {
    if (!files.length || busy.current) return;
    busy.current = true;
    setUploading(true);
    const rows = Array.from(files).map((file) => ({
      id: crypto.randomUUID(),
      file,
      progress: 0,
    }));
    const ids = new Set<string>(rows.map((row) => row.id));
    setUploads((previous) => [...rows, ...previous]);
    try {
      const items = await uploadBatch(
        rows.map((row) => row.file),
        (progress) => {
          setUploads((previous) =>
            previous.map((row) =>
              ids.has(row.id) ? { ...row, progress } : row,
            ),
          );
        },
      );
      // An older in-flight list response must not overwrite the accepted rows.
      await client.cancelQueries({ queryKey: ["documents"] });
      const accepted = items.flatMap((item) =>
        item.document ? [item.document] : [],
      );
      client.setQueryData<{ items: VaultDocument[] }>(
        ["documents"],
        (current) => ({
          items: mergeUploadedDocuments(current?.items || [], accepted),
        }),
      );
      setUploads((previous) =>
        previous.flatMap((row) => {
          const index = rows.findIndex((candidate) => candidate.id === row.id);
          if (index < 0) return [row];
          const item = items[index];
          return item.error ? [{ ...row, error: item.error }] : [];
        }),
      );
      void client.invalidateQueries({ queryKey: ["documents"] });
      void client.invalidateQueries({ queryKey: ["metrics"] });
    } catch (error) {
      setUploads((previous) =>
        previous.map((row) =>
          ids.has(row.id) ? { ...row, error: errorMessage(error) } : row,
        ),
      );
    } finally {
      busy.current = false;
      setUploading(false);
    }
  }
  function dismiss(id: string) {
    setUploads((rows) => rows.filter((row) => row.id !== id));
  }
  function retry(row: UploadRow) {
    if (busy.current) return;
    dismiss(row.id);
    void upload([row.file]);
  }
  return { uploads, uploading, upload, dismiss, retry };
}
