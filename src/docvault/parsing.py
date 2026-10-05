"""Docling conversion and native hybrid chunking with preserved source provenance."""

import importlib.metadata
import re
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path

import tiktoken
from docling_core.transforms.chunker.base import BaseChunk
from docling_core.transforms.chunker.doc_chunk import DocChunk
from docling_core.transforms.chunker.hierarchical_chunker import (
    ChunkingDocSerializer,
    ChunkingSerializerProvider,
)
from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
from docling_core.transforms.chunker.line_chunker import LineBasedTokenChunker
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
from docling_core.transforms.serializer.base import SerializationResult, Span
from docling_core.transforms.serializer.markdown import (
    MarkdownParams,
    MarkdownTableSerializer,
    MarkdownTextSerializer,
)
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.text import SectionHeaderItem, TitleItem
from docling_core.types.doc.labels import DocItemLabel
from pydantic import BaseModel, ConfigDict

from docvault.config import get_settings
from docvault.llm.provider import ContextLimitError, token_count

CHUNK_TOKENS = 750


class ParsedChunk(BaseModel):
    """Source text, contextualized embedding input, location, and token count for one chunk."""

    model_config = ConfigDict(extra="forbid")

    text: str
    embedding_text: str
    location: dict
    token_count: int


class ParsedDocument(BaseModel):
    """Complete parser result before chunk persistence or vector indexing."""

    model_config = ConfigDict(extra="forbid")

    chunks: list[ParsedChunk]
    text: str
    page_count: int | None
    parser: str


class _DocumentTokenizer(BaseTokenizer):
    """Adapt OpenAI tokenization to semchunk's literal-text counting contract."""

    openai: OpenAITokenizer

    def count_tokens(self, text: str) -> int:
        """Treat special-token spellings as ordinary source text, not control tokens."""
        return len(self.openai.get_tokenizer().encode(text, disallowed_special=()))

    def get_max_tokens(self) -> int:
        """Return the contextualized chunk capacity configured on the OpenAI tokenizer."""
        return self.openai.get_max_tokens()

    def get_tokenizer(self) -> Callable[[str], int]:
        """Give Docling's semantic splitter the same literal-text token counter."""
        return self.count_tokens


class _RawTextSerializer(MarkdownTextSerializer):
    """Keep TXT item content unchanged so source offsets can be recovered exactly."""

    def serialize(self, *, item, **kwargs) -> SerializationResult:
        """Return the original item text and its Docling provenance reference."""
        return SerializationResult(text=item.text, spans=[Span(item=item)])


class _DocumentSerializerProvider(ChunkingSerializerProvider):
    """Use Markdown tables and include text nested inside pictures, including OCR."""

    text_serializer: MarkdownTextSerializer = MarkdownTextSerializer()

    def get_serializer(self, doc: DoclingDocument) -> ChunkingDocSerializer:
        """Build one serializer whose table headers are recognizable by HybridChunker."""
        return ChunkingDocSerializer(
            doc=doc,
            text_serializer=self.text_serializer,
            table_serializer=MarkdownTableSerializer(),
            params=MarkdownParams(
                image_placeholder="",
                escape_underscores=False,
                escape_html=False,
                compact_tables=True,
                traverse_pictures=True,
                layers=set(ContentLayer),
            ),
        )


class _DocumentChunker(HybridChunker):
    """Add filename context and account for it in Docling's native token budgets."""

    filename: str

    def chunk(self, dl_doc: DoclingDocument, **kwargs) -> Iterator[DocChunk]:
        """Validate native document metadata once before exposing typed source chunks."""
        for chunk in super().chunk(dl_doc, **kwargs):
            if not isinstance(chunk, DocChunk):
                raise TypeError("Docling hybrid chunking must return document chunks.")
            yield chunk

    def contextualize(self, chunk: BaseChunk) -> str:
        """Include filename and headings without silently discarding oversized context."""
        if not isinstance(chunk, DocChunk):
            raise TypeError("Document context requires Docling document metadata.")
        context = "\n".join([self.filename, *(chunk.meta.headings or [])]) + "\n\n"
        if self.tokenizer.count_tokens(context) >= self.max_tokens:
            raise ContextLimitError(
                "The filename and section headings exceed the chunk context capacity."
            )
        return context + chunk.text

    def segment(self, doc_chunk, available_length, doc_serializer) -> list[str]:
        """Use native splitting while reserving metadata capacity for table segments.

        Docling-core 2.99's table path ignores available_length. A copied chunker
        with the remaining token capacity keeps its native header-aware splitter
        within the same budget as prose, without modifying the shared tokenizer.
        """
        if (
            self.repeat_table_header
            and isinstance(doc_serializer, ChunkingDocSerializer)
            and len(doc_chunk.meta.doc_items) == 1
            and isinstance(doc_chunk.meta.doc_items[0], TableItem)
        ):
            header, _ = doc_serializer.table_serializer.get_header_and_body_lines(
                table_text=doc_chunk.text
            )
            if self.tokenizer.count_tokens("\n".join(header)) >= available_length:
                raise ContextLimitError("The table header exceeds the chunk context capacity.")
            tokenizer = _DocumentTokenizer(
                openai=OpenAITokenizer(
                    tokenizer=tiktoken.get_encoding("cl100k_base"),
                    max_tokens=available_length,
                )
            )
            chunker = self.model_copy(update={"tokenizer": tokenizer})
            return HybridChunker.segment(chunker, doc_chunk, available_length, doc_serializer)
        return super().segment(doc_chunk, available_length, doc_serializer)


def _create_document_chunker(filename: str, *, text_serializer=None, max_tokens=CHUNK_TOKENS):
    """Configure native splitting, heading retention, and merging without fixed overlap."""
    provider = _DocumentSerializerProvider()
    if text_serializer is not None:
        provider.text_serializer = text_serializer
    return _DocumentChunker(
        filename=filename,
        tokenizer=_DocumentTokenizer(
            openai=OpenAITokenizer(
                tokenizer=tiktoken.get_encoding("cl100k_base"), max_tokens=max_tokens
            )
        ),
        serializer_provider=provider,
        merge_peers=True,
        repeat_table_header=True,
        always_emit_headings=True,
        # Raw TXT items already contain their original separators.
        delim="" if text_serializer is not None else "\n",
    )


def _build_source_item_location(item, *, is_pdf: bool) -> dict:
    """Describe an entire source item; Docling does not expose split-chunk offsets."""
    spans = []
    for provenance in item.prov:
        span = {
            "bbox": provenance.bbox.model_dump(mode="json"),
            "charspan": list(provenance.charspan),
        }
        if is_pdf:
            span["page"] = provenance.page_no
        spans.append(span)
    return {
        "item_id": item.self_ref,
        "label": item.label.value,
        "scope": "item",
        "spans": spans,
    }


def _collect_item_heading_sources(document) -> dict:
    """Associate each source item with the exact heading items active at its position."""
    headings, sources = {}, {}
    for item, _ in document.iterate_items(
        traverse_pictures=True, included_content_layers=set(ContentLayer)
    ):
        if isinstance(item, TitleItem | SectionHeaderItem):
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            headings = {depth: heading for depth, heading in headings.items() if depth < level}
            headings[level] = item
        sources[item.self_ref] = list(headings.values())
    return sources


def _build_chunk_source_location(chunk, heading_sources: dict, *, is_pdf: bool) -> dict:
    """Retain all contributing item/page/box references, including multi-page merges."""
    items = {item.self_ref: item for item in chunk.meta.doc_items}
    content_items = list(items.values())
    for item in list(items.values()):
        for heading in heading_sources.get(item.self_ref, []):
            items.setdefault(heading.self_ref, heading)
    source_spans = [_build_source_item_location(item, is_pdf=is_pdf) for item in items.values()]
    spans = [span for source in source_spans for span in source["spans"]]
    location = {
        "kind": "pdf" if is_pdf else "docx",
        "item_id": next(iter(items)),
        "item_ids": list(items),
        "headings": chunk.meta.headings or [],
        "source_spans": source_spans,
        "spans": spans,
    }
    if len(content_items) == 1:
        location["label"] = content_items[0].label.value
    pages = list(dict.fromkeys(span["page"] for span in spans if "page" in span))
    if pages:
        location.update(page=pages[0], pages=pages)
    return location


def _create_validated_chunk(text: str, embedding_text: str, location: dict) -> ParsedChunk:
    """Enforce the complete embedding-input capacity without truncating source text."""
    size = token_count(embedding_text)
    if size > CHUNK_TOKENS:
        raise ContextLimitError(
            f"A document chunk exceeds the {CHUNK_TOKENS}-token context capacity."
        )
    return ParsedChunk(
        text=text, embedding_text=embedding_text, location=location, token_count=size
    )


def _build_text_docling_document(text: str, filename: str) -> DoclingDocument:
    """Adapt original TXT paragraphs/headings into Docling items without normalization."""
    document = DoclingDocument(name=filename)
    boundaries = {0, len(text)}
    for match in re.finditer(r"(?m)^#{1,6}[ \t]+.*$|\n[ \t]*\n", text):
        boundaries.add(match.start() if match.group().startswith("#") else match.end())
    positions = sorted(boundaries)
    for start, end in zip(positions, positions[1:]):
        value = text[start:end]
        if not value.strip():
            continue
        heading = re.match(r"(#{1,6})[ \t]+([^\r\n]+)", value)
        if heading:
            document.add_heading(text=heading[2], level=len(heading[1]))
        # Keep heading syntax in source text; its separate heading item supplies
        # hierarchy only. This makes every returned TXT chunk an exact substring.
        document.add_text(label=DocItemLabel.TEXT, text=value, orig=value)
    return document


def _align_text_chunk_spans(text: str, chunks) -> list[tuple[int, int]]:
    """Align native chunks with the source, restoring whitespace removed by semchunk.

    Reject unexpected non-whitespace transformations instead of inventing offsets.
    Sequential matching also handles repeated paragraphs unambiguously.
    """
    spans, cursor = [], 0
    for chunk in chunks:
        start = cursor
        for character in chunk.text:
            if character.isspace():
                continue
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            if cursor == len(text) or text[cursor] != character:
                raise ValueError("Could not align the chunked TXT content with its source.")
            cursor += 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        spans.append((start, cursor))
    if cursor != len(text):
        raise ValueError("Chunking did not preserve the complete TXT content.")
    return spans


def _split_restored_text_if_needed(text: str, prefix: str, tokenizer) -> list[str]:
    """Use Docling's native splitter if restored TXT whitespace exceeds the capacity."""
    if token_count(prefix + text) <= CHUNK_TOKENS:
        return [text]
    capacity = CHUNK_TOKENS - token_count(prefix) - 2
    if capacity < 1:
        raise ContextLimitError(
            "The filename and section headings exceed the chunk context capacity."
        )
    splitter = LineBasedTokenChunker(
        tokenizer=tokenizer,
        prefix="",
        omit_prefix_on_overflow=False,
        serializer_provider=_DocumentSerializerProvider(),
    )
    pieces = []
    while text:
        head, text = splitter.split_by_token_limit(text, capacity)
        if not head:
            raise ContextLimitError("A source character exceeds the chunk context capacity.")
        pieces.append(head)
    return pieces


def parse_text_document(text: str, filename: str) -> ParsedDocument:
    """Chunk all UTF-8 text through Docling with exact original character/line offsets.

    Preserve the full input and restore whitespace between semantic splits. Reject
    binary or empty input and context that cannot fit, without a whole-file cap.
    """
    if "\x00" in text:
        raise ValueError("The text file contains binary data.")
    if not text.strip():
        raise ValueError("No readable text was found in the file.")
    document = _build_text_docling_document(text, filename)
    chunker = _create_document_chunker(filename, text_serializer=_RawTextSerializer())
    native_chunks = list(chunker.chunk(document))
    spans = _align_text_chunk_spans(text, native_chunks)
    chunks, line_start = [], 1
    for native, (start, end) in zip(native_chunks, spans):
        native.text = ""
        prefix = chunker.contextualize(native)
        for source_text in _split_restored_text_if_needed(
            text[start:end], prefix, chunker.tokenizer
        ):
            if not source_text:
                continue
            location = {
                "kind": "text",
                "char_start": start,
                "char_end": start + len(source_text),
                "line_start": line_start,
                "line_end": line_start + source_text.rstrip("\r\n").count("\n"),
                "headings": native.meta.headings or [],
                "item_ids": list(dict.fromkeys(item.self_ref for item in native.meta.doc_items)),
            }
            chunks.append(_create_validated_chunk(source_text, prefix + source_text, location))
            line_start += source_text.count("\n")
            start += len(source_text)
    return ParsedDocument(
        chunks=chunks, text=text, page_count=None, parser="utf8-docling-hybrid-v1"
    )


@lru_cache(maxsize=1)
def _get_document_converter():
    """Build the cached PDF/DOCX converter with English PDF OCR and table structure.

    RapidOCR uses the torch backend. Missing conversion dependencies raise RuntimeError.
    """
    try:
        from docling.datamodel.accelerator_options import AcceleratorOptions
        from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError("Install the parsing extra to process PDF and DOCX files.") from exc
    settings = get_settings()
    options = PdfPipelineOptions(
        accelerator_options=AcceleratorOptions(num_threads=settings.docling_num_threads),
        layout_batch_size=settings.docling_layout_batch_size,
        ocr_batch_size=settings.docling_ocr_batch_size,
        table_batch_size=settings.docling_table_batch_size,
    )
    options.do_ocr = True
    options.do_table_structure = True
    options.ocr_options = RapidOcrOptions(lang=["english"], backend="torch")
    backend_options = ThreadedDoclingParseBackendOptions.model_validate(
        {"parser_threads": settings.docling_parser_threads or settings.docling_num_threads}
    )
    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_options=options,
                backend_options=backend_options,
            )
        },
    )


def chunk_docling_document(document, filename: str, *, is_pdf: bool) -> ParsedDocument:
    """Chunk the complete converted document with native Docling structure and tokens.

    Keep Markdown table headers, picture/OCR text, and all contributing item-level
    provenance. PDF chunks can cover multiple pages; DOCX has no stable page count.
    """
    chunker = _create_document_chunker(filename)
    heading_sources = _collect_item_heading_sources(document)
    chunks = []
    for native in chunker.chunk(document):
        # Empty heading-only sections are real content, represented as headings
        # by Docling; expose them as readable evidence as well as context.
        text = native.text or "\n".join(native.meta.headings or [])
        if not text.strip():
            continue
        chunks.append(
            _create_validated_chunk(
                text,
                chunker.contextualize(native),
                _build_chunk_source_location(native, heading_sources, is_pdf=is_pdf),
            )
        )
    if not chunks:
        raise ValueError("No readable text was found; check the file or OCR configuration.")
    return ParsedDocument(
        chunks=chunks,
        text=document.export_to_markdown(
            traverse_pictures=True, included_content_layers=set(ContentLayer)
        ),
        page_count=len(document.pages) if is_pdf else None,
        parser=f"docling-{importlib.metadata.version('docling')}-hybrid-v1",
    )


def parse_document_file(path: Path, mime_type: str, filename: str) -> ParsedDocument:
    """Read UTF-8 TXT or convert PDF/DOCX, then apply Docling hybrid chunking.

    PDF conversion enables OCR and may download uncached model assets. Reject
    unsupported formats, invalid TXT encoding, and incomplete conversions.
    """
    if mime_type == "text/plain":
        try:
            return parse_text_document(path.read_text(encoding="utf-8-sig"), filename)
        except UnicodeDecodeError as exc:
            raise ValueError("TXT files must use UTF-8 encoding.") from exc
    supported = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    if mime_type not in supported:
        raise ValueError("Only PDF, DOCX, and UTF-8 TXT documents are supported.")
    result = _get_document_converter().convert(path, raises_on_error=True)
    status = str(getattr(result.status, "value", result.status))
    if status != "success":
        raise ValueError("Document conversion was incomplete; the file was not indexed.")
    return chunk_docling_document(result.document, filename, is_pdf=mime_type == "application/pdf")
