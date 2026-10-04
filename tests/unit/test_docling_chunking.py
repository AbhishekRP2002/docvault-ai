"""Native Docling chunking regressions for source fidelity and context budgets."""

import re

import pytest
from docling_core.types.doc.base import BoundingBox, Size
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.common.reference import ProvenanceItem
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table_data import TableCell, TableData
from docling_core.types.doc.labels import DocItemLabel

from docvault.llm.provider import ContextLimitError, token_count
from docvault.parsing import chunk_docling_document, parse_text_document


def provenance(page: int, text: str) -> ProvenanceItem:
    """Build deterministic source coordinates without invoking OCR or model downloads."""
    return ProvenanceItem(
        page_no=page,
        charspan=(0, len(text)),
        bbox=BoundingBox(l=10, t=20, r=100, b=40),
    )


def table(document: DoclingDocument, rows: list[list[str]]):
    """Add a real Docling table with the first row marked as column headers."""
    return document.add_table(
        data=TableData(
            num_rows=len(rows),
            num_cols=len(rows[0]),
            table_cells=[
                TableCell(
                    text=value,
                    start_row_offset_idx=row,
                    end_row_offset_idx=row + 1,
                    start_col_offset_idx=column,
                    end_col_offset_idx=column + 1,
                    column_header=row == 0,
                )
                for row, values in enumerate(rows)
                for column, value in enumerate(values)
            ],
        )
    )


def test_txt_native_merges_keep_exact_whitespace_and_literal_special_tokens():
    source = (
        "\r\n# Terms\r\nPayment café <|endoftext|> 🔐.\r\n\r\n"
        + "Repeated text.\t\n\n" * 500
        + "## Renewal\nAnnual.\n\n# End\n"
    )
    parsed = parse_text_document(source, "terms.txt")
    assert parsed.text == source
    assert "".join(chunk.text for chunk in parsed.chunks) == source
    assert len(parsed.chunks) < 30
    for chunk in parsed.chunks:
        start, end = chunk.location["char_start"], chunk.location["char_end"]
        assert source[start:end] == chunk.text
        assert chunk.location["line_start"] == source[:start].count("\n") + 1
        assert chunk.token_count == token_count(chunk.embedding_text) <= 600
        assert "terms.txt" in chunk.embedding_text
    assert any(chunk.location["headings"] == ["Terms", "Renewal"] for chunk in parsed.chunks)
    assert parsed.chunks[-1].location["headings"] == ["End"]


def test_txt_large_whitespace_gap_is_preserved_without_exceeding_context():
    source = "First.\n" + " \t\n" * 2000 + "Last."
    parsed = parse_text_document(source, "spaced.txt")
    assert "".join(chunk.text for chunk in parsed.chunks) == source
    assert all(chunk.token_count <= 600 for chunk in parsed.chunks)
    assert parsed.chunks[-1].location["char_end"] == len(source)


def test_native_chunks_preserve_all_pages_and_heading_item_provenance():
    document = DoclingDocument(name="pages")
    for page in (1, 2):
        document.add_page(page_no=page, size=Size(width=200, height=300))
    heading = document.add_heading("Terms", prov=provenance(1, "Terms"))
    first = document.add_text(
        label=DocItemLabel.TEXT, text="First page.", prov=provenance(1, "First page.")
    )
    second = document.add_text(
        label=DocItemLabel.TEXT, text="Second page.", prov=provenance(2, "Second page.")
    )
    parsed = chunk_docling_document(document, "pages.pdf", is_pdf=True)
    assert len(parsed.chunks) == 1
    location = parsed.chunks[0].location
    assert location["pages"] == [1, 2]
    assert set(location["item_ids"]) == {first.self_ref, second.self_ref, heading.self_ref}
    assert all(span["scope"] == "item" for span in location["source_spans"])
    assert all("text_char_start" not in span for span in location["source_spans"])
    assert parsed.page_count == 2
    docx = chunk_docling_document(document, "pages.docx", is_pdf=False)
    assert docx.page_count is None
    assert "page" not in docx.chunks[0].location
    assert all("page" not in span for span in docx.chunks[0].location["spans"])


def test_picture_child_ocr_and_furniture_remain_readable_and_located():
    document = DoclingDocument(name="scan")
    document.add_page(page_no=1, size=Size(width=200, height=300))
    picture = document.add_picture(prov=provenance(1, ""))
    ocr = document.add_text(
        label=DocItemLabel.TEXT,
        text="Invoice total 125 USD.",
        parent=picture,
        prov=provenance(1, "Invoice total 125 USD."),
    )
    footer = document.add_text(
        label=DocItemLabel.PAGE_FOOTER,
        text="Reference ABC-123",
        content_layer=ContentLayer.FURNITURE,
        prov=provenance(1, "Reference ABC-123"),
    )
    parsed = chunk_docling_document(document, "scan.pdf", is_pdf=True)
    content = "\n".join(chunk.text for chunk in parsed.chunks)
    assert ocr.text in content and footer.text in content
    assert ocr.text in parsed.text and footer.text in parsed.text
    refs = {ref for chunk in parsed.chunks for ref in chunk.location["item_ids"]}
    assert ocr.self_ref in refs and footer.self_ref in refs


def test_empty_sections_remain_evidence_and_oversized_context_fails_visibly():
    document = DoclingDocument(name="headings")
    document.add_heading("First section")
    document.add_heading("Second section")
    parsed = chunk_docling_document(document, "headings.docx", is_pdf=False)
    assert [chunk.text for chunk in parsed.chunks] == ["First section", "Second section"]
    assert all(chunk.token_count <= 600 for chunk in parsed.chunks)
    document.add_heading("Very long heading " * 400)
    document.add_text(label=DocItemLabel.TEXT, text="Content must not be silently truncated.")
    with pytest.raises(ContextLimitError, match="headings"):
        chunk_docling_document(document, "headings.docx", is_pdf=False)
    with pytest.raises(ContextLimitError, match="filename"):
        parse_text_document("Content.", "filename " * 700)


def test_table_segments_reserve_large_filename_and_heading_context():
    document = DoclingDocument(name="table")
    document.add_heading("Quarterly pricing terms " * 100)
    table(
        document,
        [["Product", "Price"], *[[f"SKU-{index}", f"{index} USD"] for index in range(200)]],
    )
    parsed = chunk_docling_document(document, "contract_" * 25 + ".pdf", is_pdf=True)
    chunks = [chunk for chunk in parsed.chunks if "SKU-" in chunk.text]
    assert len(chunks) > 5
    headers = chunks[0].text.splitlines()[:2]
    assert all(chunk.text.splitlines()[:2] == headers for chunk in chunks)
    assert all(
        chunk.token_count == token_count(chunk.embedding_text) <= 600 for chunk in parsed.chunks
    )
    joined = "\n".join(chunk.text for chunk in chunks)
    assert re.findall(r"\|\s*SKU-(\d+)\s*\|", joined) == [str(index) for index in range(200)]


def test_oversized_table_header_does_not_disappear_from_later_chunks():
    document = DoclingDocument(name="wide")
    table(document, [["Huge column name " * 400, "Price"], ["Product", "5 USD"]])
    with pytest.raises(ContextLimitError, match="table header"):
        chunk_docling_document(document, "wide.pdf", is_pdf=True)


def test_oversized_table_row_keeps_every_value_and_repeats_headers():
    document = DoclingDocument(name="long-row")
    document.add_heading("Notes " * 70)
    table(
        document,
        [["Key", "Value"], ["long-row", " ".join(f"term{index}" for index in range(1500))]],
    )
    parsed = chunk_docling_document(document, "long.pdf", is_pdf=True)
    assert len(parsed.chunks) > 1
    headers = parsed.chunks[0].text.splitlines()[:2]
    assert all(chunk.text.splitlines()[:2] == headers for chunk in parsed.chunks)
    assert all(chunk.token_count <= 600 for chunk in parsed.chunks)
    assert all(chunk.location["label"] == "table" for chunk in parsed.chunks)
    values = re.findall(r"term\d+", "\n".join(chunk.text for chunk in parsed.chunks))
    assert values == [f"term{index}" for index in range(1500)]
