"""Structure-aware conversion with original source locations and no file token cap."""

import importlib.metadata
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from docvault.ai.provider import token_count
from docvault.ai.types import ParsedChunk, ParsedDocument

CHUNK_TOKENS = 600


@dataclass(frozen=True)
class Block:
    text: str
    location: dict
    headings: tuple[str, ...] = ()
    table: bool = False


def split_spans(text: str, max_tokens: int) -> list[tuple[int, int]]:
    """Split on readable boundaries; offsets always refer to the original string."""
    if max_tokens < 1:
        raise ValueError("A positive token capacity is required.")
    spans, start = [], 0
    while start < len(text):
        # Bound tokenization work by a chunk-sized window even for enormous
        # paragraphs. Grow the window for unusually compressible whitespace.
        high = min(len(text), start + max_tokens * 8)
        while high < len(text) and token_count(text[start:high]) <= max_tokens:
            high = min(len(text), start + (high - start) * 2)
        if high == len(text) and token_count(text[start:high]) <= max_tokens:
            spans.append((start, high))
            break
        low = start + 1
        while low < high:
            middle = (low + high + 1) // 2
            if token_count(text[start:middle]) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        end = low
        if token_count(text[start:end]) > max_tokens:
            raise ValueError("A character exceeds the chunk token capacity.")
        # Prefer sentence/line/word boundaries without leaving tiny fragments.
        minimum = start + (end - start) // 2
        for separator in ("\n", ". ", " "):
            boundary = text.rfind(separator, minimum, end)
            if boundary >= minimum:
                end = boundary + len(separator)
                break
        spans.append((start, end))
        start = end
    return spans


def _table_pieces(text: str, capacity: int) -> list[tuple[str, dict]]:
    rows = text.splitlines(keepends=True)
    if len(rows) < 3 or not re.match(r"^[\s|:\-]+$", rows[1]):
        return [
            (text[a:b], {"item_char_start": a, "item_char_end": b})
            for a, b in split_spans(text, capacity)
        ]
    header = "".join(rows[:2])
    header_size = token_count(header)
    if header_size >= capacity // 2:
        return [
            (text[a:b], {"item_char_start": a, "item_char_end": b})
            for a, b in split_spans(text, capacity)
        ]
    pieces, current, first = [], header, 3
    for row_number, row in enumerate(rows[2:], 3):
        if token_count(current + row) > capacity and current != header:
            pieces.append((current, {"table_row_start": first, "table_row_end": row_number - 1}))
            current, first = header, row_number
        if token_count(header + row) > capacity:
            for a, b in split_spans(row, capacity - header_size - 2):
                pieces.append(
                    (
                        header + row[a:b],
                        {
                            "table_row_start": row_number,
                            "table_row_end": row_number,
                            "row_char_start": a,
                            "row_char_end": b,
                        },
                    )
                )
            current, first = header, row_number + 1
        else:
            current += row
    if current != header:
        pieces.append((current, {"table_row_start": first, "table_row_end": len(rows)}))
    return pieces


def chunk_blocks(blocks: list[Block], filename: str) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    for block in blocks:
        if not block.text.strip():
            continue
        context = " > ".join(block.headings)
        prefix = f"{filename}\n{context}\n\n" if context else f"{filename}\n\n"
        # Very long titles remain in provenance; cap repeated context, not source text.
        if token_count(prefix) > CHUNK_TOKENS // 3:
            a, b = split_spans(prefix, CHUNK_TOKENS // 3)[0]
            prefix = prefix[a:b] + "\n\n"
        capacity = CHUNK_TOKENS - token_count(prefix) - 2
        if block.table:
            pieces = _table_pieces(block.text, capacity)
        else:
            pieces = [
                (block.text[a:b], {"item_char_start": a, "item_char_end": b})
                for a, b in split_spans(block.text, capacity)
            ]
        for text, span in pieces:
            if not text.strip():
                continue
            location = {**block.location, **span, "headings": list(block.headings)}
            if "char_start" in block.location and "item_char_start" in span:
                location["char_start"] = block.location["char_start"] + span["item_char_start"]
                location["char_end"] = block.location["char_start"] + span["item_char_end"]
                location["line_start"] = block.location["line_start"] + block.text[
                    : span["item_char_start"]
                ].count("\n")
                location["line_end"] = location["line_start"] + text.rstrip("\n").count("\n")
            embedding_text = prefix + text
            chunks.append(
                ParsedChunk(
                    text=text,
                    embedding_text=embedding_text,
                    location=location,
                    token_count=token_count(embedding_text),
                )
            )
    return _merge_document_chunks(chunks)


def _merge_document_chunks(chunks: list[ParsedChunk]) -> list[ParsedChunk]:
    """Pack adjacent prose items while preserving every item's source mapping."""
    merged: list[ParsedChunk] = []
    for chunk in chunks:
        if not merged:
            merged.append(chunk)
            continue
        previous = merged[-1]
        compatible = (
            chunk.location.get("kind") in {"pdf", "docx"}
            and previous.location.get("kind") == chunk.location.get("kind")
            and previous.location.get("page") == chunk.location.get("page")
            and previous.location.get("headings") == chunk.location.get("headings")
            and previous.location.get("label") != "table"
            and chunk.location.get("label") != "table"
        )
        combined_text = previous.text + "\n\n" + chunk.text
        prefix = previous.embedding_text[: -len(previous.text)]
        combined_input = prefix + combined_text
        size = token_count(combined_input) if compatible else CHUNK_TOKENS + 1
        if not compatible or size > CHUNK_TOKENS:
            merged.append(chunk)
            continue
        locations = previous.location.get("source_spans") or [
            {
                **previous.location,
                "text_char_start": 0,
                "text_char_end": len(previous.text),
            }
        ]
        locations = [
            *locations,
            {
                **chunk.location,
                "text_char_start": len(previous.text) + 2,
                "text_char_end": len(combined_text),
            },
        ]
        location = {
            key: value
            for key, value in previous.location.items()
            if key not in {"item_char_start", "item_char_end", "source_spans", "spans"}
        }
        location["source_spans"] = locations
        location["spans"] = [*previous.location.get("spans", []), *chunk.location.get("spans", [])]
        merged[-1] = ParsedChunk(
            text=combined_text,
            embedding_text=combined_input,
            location=location,
            token_count=size,
        )
    return merged


def parse_text(text: str, filename: str) -> ParsedDocument:
    if "\x00" in text:
        raise ValueError("The text file contains binary data.")
    # Paragraph boundaries preserve useful context, while exact offsets remain intact.
    blocks, headings, previous_end, line_number = [], (), 0, 1
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", text):
        value = match.group()
        heading = re.match(r"^(#{1,6})\s+(.+)$", value.splitlines()[0])
        if heading:
            headings = (*headings[: len(heading[1]) - 1], heading[2])
        line_number += text[previous_end : match.start()].count("\n")
        blocks.append(
            Block(
                value,
                {
                    "kind": "text",
                    "char_start": match.start(),
                    "char_end": match.end(),
                    "line_start": line_number,
                    "line_end": line_number + value.count("\n"),
                },
                headings,
            )
        )
        line_number += value.count("\n")
        previous_end = match.end()
    chunks = chunk_blocks(blocks, filename)
    if not chunks:
        raise ValueError("No readable text was found in the file.")
    return ParsedDocument(chunks=chunks, text=text, page_count=None, parser="utf8-v1")


@lru_cache(maxsize=1)
def _converter():
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError("Install the parsing extra to process PDF and DOCX files.") from exc
    options = PdfPipelineOptions()
    options.do_ocr = True
    options.do_table_structure = True
    # Docling's standard bundle includes RapidOCR and PyTorch. Reuse that engine
    # rather than requiring an additional OCR/runtime dependency.
    options.ocr_options = RapidOcrOptions(lang=["english"], backend="torch")
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )


def parse_docling_document(document, filename: str, *, is_pdf: bool) -> ParsedDocument:
    blocks, headings = [], ()
    for item, level in document.iterate_items():
        label = str(getattr(item.label, "value", item.label))
        table = label == "table"
        text = item.export_to_markdown(doc=document) if table else getattr(item, "text", "")
        if not text.strip():
            continue
        if label in {"section_header", "title"}:
            depth = max(1, getattr(item, "level", level) or 1)
            headings = (*headings[: depth - 1], text)
        spans = []
        for provenance in getattr(item, "prov", []):
            span = {"page": provenance.page_no}
            if provenance.bbox is not None:
                span["bbox"] = provenance.bbox.model_dump(mode="json")
            if getattr(provenance, "charspan", None) is not None:
                span["charspan"] = list(provenance.charspan)
            spans.append(span)
        location = {
            "kind": "pdf" if is_pdf else "docx",
            "item_id": item.self_ref,
            "label": label,
            "spans": spans,
        }
        if spans and is_pdf:
            location["page"] = spans[0]["page"]
        blocks.append(Block(text, location, headings, table))
    chunks = chunk_blocks(blocks, filename)
    if not chunks:
        raise ValueError("No readable text was found; check the file or OCR configuration.")
    return ParsedDocument(
        chunks=chunks,
        text=document.export_to_markdown(),
        page_count=len(document.pages) if is_pdf else None,
        parser=f"docling-{importlib.metadata.version('docling')}",
    )


def parse_file(path: Path, mime_type: str, filename: str) -> ParsedDocument:
    if mime_type == "text/plain":
        try:
            return parse_text(path.read_text(encoding="utf-8-sig"), filename)
        except UnicodeDecodeError as exc:
            raise ValueError("TXT files must use UTF-8 encoding.") from exc
    supported = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if mime_type not in supported:
        raise ValueError("Only PDF, DOCX, and UTF-8 TXT documents are supported.")
    result = _converter().convert(path, raises_on_error=True)
    status = str(getattr(result.status, "value", result.status))
    if status != "success":
        raise ValueError("Document conversion was incomplete; the file was not indexed.")
    return parse_docling_document(result.document, filename, is_pdf=mime_type == "application/pdf")
