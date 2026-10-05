"""Measure model initialization, PDF conversion, and chunking without indexing or LLM calls."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat

from docvault.config import get_settings
from docvault.parsing import _get_document_converter, chunk_docling_document


def main() -> None:
    """Benchmark one supplied PDF with explicit tuning and print metadata without source text."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--layout-batch-size", type=int, default=4)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--device", choices=["auto", "cpu", "mps", "cuda", "xpu"], default="auto")
    arguments = parser.parse_args()
    if arguments.path.suffix.lower() != ".pdf" or not arguments.path.is_file():
        parser.error("Provide an existing PDF file.")
    if min(arguments.threads, arguments.layout_batch_size, arguments.runs) < 1:
        parser.error("Threads, batch size, and runs must be positive.")
    os.environ.update(
        DOCLING_NUM_THREADS=str(arguments.threads),
        DOCLING_PARSER_THREADS=str(arguments.threads),
        DOCLING_LAYOUT_BATCH_SIZE=str(arguments.layout_batch_size),
        DOCLING_DEVICE=arguments.device,
    )
    get_settings.cache_clear()
    _get_document_converter.cache_clear()
    started = time.perf_counter()
    converter = _get_document_converter()
    converter.initialize_pipeline(InputFormat.PDF)
    initialization_seconds = time.perf_counter() - started
    for run in range(1, arguments.runs + 1):
        started = time.perf_counter()
        result = converter.convert(arguments.path, raises_on_error=True)
        conversion_seconds = time.perf_counter() - started
        if result.status.value != "success":
            raise RuntimeError("Document conversion was incomplete.")
        started = time.perf_counter()
        parsed = chunk_docling_document(result.document, arguments.path.name, is_pdf=True)
        chunking_seconds = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "run": run,
                    "threads": arguments.threads,
                    "layout_batch_size": arguments.layout_batch_size,
                    "device": arguments.device,
                    "initialization_seconds": round(initialization_seconds, 3),
                    "conversion_seconds": round(conversion_seconds, 3),
                    "chunking_seconds": round(chunking_seconds, 3),
                    "pages": parsed.page_count,
                    "chunks": len(parsed.chunks),
                    "text_sha256": hashlib.sha256(parsed.text.encode()).hexdigest(),
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
