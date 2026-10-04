import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from docvault.llm.graphs import InvalidCitationError, RewrittenQuestion, run_document_chat_workflow
from docvault.llm.insights import (
    DimensionFinding,
    DocumentInsights,
    KeyInsight,
    generate_document_comparison,
    generate_document_summary,
)
from docvault.llm.parsing import (
    parse_document_file,
    parse_text_document,
)
from docvault.llm.provider import (
    ContextLimitError,
    OpenRouterLLM,
    ProviderError,
    build_strict_response_schema,
    extract_streamed_response_prefix,
    token_count,
)
from docvault.llm.types import Answer, Evidence


def source(id="c1", text="Payment is due in 30 days.", version="v1"):
    return Evidence(
        id=id,
        document_id="d1",
        version_id=version,
        filename="contract.txt",
        version_number=1,
        text=text,
        location={"line_start": 1},
    )


@pytest.mark.parametrize(
    "value",
    [
        'A quote: "yes". A slash: \\.\nA line.',
        "Renewal is due. 🧾 Café हिंदी",
        'This says response: "not another field"',
    ],
)
def test_partial_response_streams_only_stable_complete_characters(value):
    raw = json.dumps({"suggestions": ["Next?"], "response": value, "citation_ids": ["c1"]})
    previous = ""
    observed_before_completion = False
    for index in range(len(raw) + 1):
        prefix = extract_streamed_response_prefix(raw[:index])
        assert prefix.startswith(previous)
        assert value.startswith(prefix)
        observed_before_completion |= bool(prefix) and index < len(raw)
        previous = prefix
    assert previous == value
    assert observed_before_completion


def test_answer_contract_and_strict_schema():
    with pytest.raises(ValidationError):
        Answer(response="Hi", suggestions=["a", "b", "c", "d"], citation_ids=[], outcome="answered")
    schema = build_strict_response_schema(Answer)["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert set(schema["schema"]["required"]) == {
        "response",
        "suggestions",
        "citation_ids",
        "outcome",
    }


def test_long_unicode_text_keeps_every_character_without_token_cap():
    text = "Privacy café 🔐 and information. " * 15000
    assert token_count(text) > 100_000
    parsed = parse_text_document(text, "terms.txt")
    assert parsed.text == text
    assert "".join(chunk.text for chunk in parsed.chunks) == text
    assert all(chunk.token_count <= 600 for chunk in parsed.chunks)
    for chunk in parsed.chunks:
        assert text[chunk.location["char_start"] : chunk.location["char_end"]] == chunk.text


def test_txt_citations_refer_to_exact_lines_and_characters(tmp_path):
    text = "# Terms\nPayment due in 30 days.\n\nRenewal is annual.\n"
    path = tmp_path / "terms.txt"
    path.write_text(text)
    parsed = parse_document_file(path, "text/plain", "terms.txt")
    assert parsed.chunks[0].location["char_start"] == 0
    assert parsed.chunks[-1].location["char_end"] == len(text)
    for chunk in parsed.chunks:
        start, end = chunk.location["char_start"], chunk.location["char_end"]
        assert text[start:end] == chunk.text
        assert chunk.location["line_start"] == text[:start].count("\n") + 1
        assert chunk.location["line_end"] == (
            chunk.location["line_start"] + chunk.text.rstrip("\n").count("\n")
        )
    with pytest.raises(ValueError, match="No readable"):
        parse_text_document("\n  \n", "empty.txt")
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="UTF-8"):
        parse_document_file(path, "text/plain", "bad.txt")


def test_tables_repeat_headers_and_preserve_all_rows():
    import re

    from docling_core.types.doc import DoclingDocument, TableCell, TableData

    from docvault.llm.parsing import chunk_docling_document

    document = DoclingDocument(name="quote")
    document.add_heading("Pricing")
    cells = []
    rows = [["item", "price"], *[[f"Product {index}", f"{index} USD"] for index in range(300)]]
    for row_number, row in enumerate(rows):
        for column, value in enumerate(row):
            cells.append(
                TableCell(
                    text=value,
                    start_row_offset_idx=row_number,
                    end_row_offset_idx=row_number + 1,
                    start_col_offset_idx=column,
                    end_col_offset_idx=column + 1,
                    column_header=row_number == 0,
                )
            )
    document.add_table(data=TableData(table_cells=cells, num_rows=len(rows), num_cols=2))
    parsed = chunk_docling_document(document, "quote.pdf", is_pdf=True)
    table_chunks = [chunk for chunk in parsed.chunks if "Product" in chunk.text]
    assert len(table_chunks) > 1
    headers = table_chunks[0].text.splitlines()[:2]
    assert "item" in headers[0] and "price" in headers[0]
    assert all(chunk.text.splitlines()[:2] == headers for chunk in table_chunks)
    assert all(chunk.token_count <= 600 for chunk in parsed.chunks)
    actual_rows = re.findall(r"\|\s*Product (\d+)\s*\|\s*(\d+) USD\s*\|", "\n".join(chunk.text for chunk in table_chunks))
    assert actual_rows == [(str(index), str(index)) for index in range(300)]


def test_docling_preserves_pages_headings_and_rejects_partial_conversion(monkeypatch, tmp_path):
    from docling_core.types.doc import (
        BoundingBox,
        DocItemLabel,
        DoclingDocument,
        ProvenanceItem,
        Size,
    )

    from docvault.llm import parsing

    document = DoclingDocument(name="contract")
    for page in range(1, 4):
        document.add_page(page_no=page, size=Size(width=200, height=300))
    provenance = ProvenanceItem(
        page_no=3,
        charspan=(0, 25),
        bbox=BoundingBox(l=10, t=20, r=100, b=30),
    )
    heading = document.add_heading("Terms", prov=provenance)
    paragraph = document.add_text(
        label=DocItemLabel.TEXT, text="Payment is due in 30 days.", prov=provenance
    )
    parsed = parsing.chunk_docling_document(document, "contract.pdf", is_pdf=True)
    assert parsed.page_count == 3
    assert all(chunk.location["page"] == 3 for chunk in parsed.chunks)
    content_chunks = [chunk for chunk in parsed.chunks if paragraph.text in chunk.text]
    assert len(content_chunks) == 1
    assert content_chunks[0].location["headings"] == ["Terms"]
    assert all(chunk.location["spans"][0]["bbox"]["l"] == 10 for chunk in parsed.chunks)
    item_ids = {
        span["item_id"] for chunk in parsed.chunks for span in chunk.location["source_spans"]
    }
    assert heading.self_ref in item_ids and paragraph.self_ref in item_ids
    converter = SimpleNamespace(
        convert=lambda *args, **kwargs: SimpleNamespace(status="partial_success")
    )
    monkeypatch.setattr(parsing, "_get_document_converter", lambda: converter)
    with pytest.raises(ValueError, match="incomplete"):
        parse_document_file(tmp_path / "contract.pdf", "application/pdf", "contract.pdf")


class ChatLLM:
    def __init__(self, answer=None, rewrite=None):
        self.answer = answer or Answer(
            response="Payment is due in 30 days. [c1]",
            suggestions=["When does it renew?"],
            citation_ids=["c1"],
            outcome="answered",
        )
        self.rewrite = rewrite
        self.generations = 0

    async def generate_structured_response(self, schema, messages):
        assert schema is RewrittenQuestion
        return self.rewrite

    async def stream_structured_answer(self, messages, on_delta):
        self.generations += 1
        await on_delta(self.answer.response)
        return self.answer


@pytest.mark.asyncio
async def test_graph_rewrites_followups_then_retrieves_fresh_evidence():
    queries, deltas = [], []

    async def retrieve_relevant_chunks(query):
        queries.append(query)
        return [source()]

    async def delta(value):
        deltas.append(value)

    llm = ChatLLM(
        rewrite=RewrittenQuestion(
            question="When is the contract payment due?",
            needs_clarification=False,
            clarification="",
        )
    )
    answer, evidence, query = await run_document_chat_workflow(
        "When is it due?",
        [
            {"role": "user", "content": "Tell me about payment."},
        ],
        retrieve_relevant_chunks,
        llm,
        delta,
    )
    assert queries == [query] == ["When is the contract payment due?"]
    assert evidence == [source()]
    assert answer.citation_ids == ["c1"]
    assert "".join(deltas) == answer.response


@pytest.mark.asyncio
async def test_graph_without_evidence_does_not_generate_or_fake_deltas():
    llm, deltas = ChatLLM(), []

    async def retrieve_relevant_chunks(query):
        return []

    async def delta(value):
        deltas.append(value)

    answer, evidence, _ = await run_document_chat_workflow("Is there a fee?", [], retrieve_relevant_chunks, llm, delta)
    assert answer.outcome == "insufficient_evidence"
    assert evidence == [] and deltas == [] and llm.generations == 0


@pytest.mark.asyncio
async def test_graph_ambiguous_followup_skips_retrieval():
    llm = ChatLLM(
        rewrite=RewrittenQuestion(
            question="Which contract?",
            needs_clarification=True,
            clarification="Which contract do you mean?",
        )
    )

    async def retrieve_relevant_chunks(query):
        pytest.fail("Ambiguous questions must not run retrieval")

    async def delta(value):
        pytest.fail("Clarification is a final response, not a fake model stream")

    answer, _, _ = await run_document_chat_workflow(
        "What about it?", [{"role": "user", "content": "Compare contracts"}], retrieve_relevant_chunks, llm, delta
    )
    assert answer.outcome == "clarification_needed"
    assert llm.generations == 0


@pytest.mark.asyncio
async def test_graph_rejects_invented_citation_ids():
    llm = ChatLLM(
        Answer(
            response="A fee exists", suggestions=[], citation_ids=["invented"], outcome="answered"
        )
    )

    async def retrieve_relevant_chunks(query):
        return [source()]

    async def delta(value):
        pass

    with pytest.raises(InvalidCitationError):
        await run_document_chat_workflow("Fee?", [], retrieve_relevant_chunks, llm, delta)


class FakeChunk:
    def __init__(self, content=None, finish=None, usage=None):
        self.choices = (
            []
            if usage
            else [
                SimpleNamespace(
                    delta=SimpleNamespace(content=content, refusal=None),
                    finish_reason=finish,
                )
            ]
        )
        self.usage = usage

    def model_dump(self):
        return {"id": "req1", "model": "test/model", "usage": self.usage}


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_provider_streams_partial_response_before_end_and_records_usage():
    usage, deltas, requested = [], [], []

    async def record_llm_call_async(value):
        usage.append(value)

    llm = OpenRouterLLM(
        "test-key", "https://example.test/v1", "test/model", "test/embed", record_llm_call_async=record_llm_call_async
    )
    stream = FakeStream(
        [
            FakeChunk('{"response":"Payment'),
            FakeChunk(' in 30 days.","suggestions":[],"citation_ids":["c1"],"outcome":"answered"}'),
            FakeChunk(finish="stop"),
            FakeChunk(usage={"prompt_tokens": 42, "completion_tokens": 12, "cost": 0.003}),
        ]
    )

    async def create(**kwargs):
        requested.append(kwargs)
        return stream

    async def delta(value):
        assert not stream.closed
        deltas.append(value)

    llm.client.chat.completions.create = create
    try:
        answer = await llm.stream_structured_answer([{"role": "user", "content": "Payment?"}], delta)
    finally:
        await llm.close()
    assert deltas == ["Payment", " in 30 days."]
    assert answer.response == "Payment in 30 days."
    assert stream.closed
    assert requested[0]["stream"] is True
    assert requested[0]["extra_body"]["provider"]["require_parameters"] is True
    assert usage[0]["cost_usd"] == 0.003 and usage[0]["input_tokens"] == 42
    assert usage[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_provider_rejects_truncated_stream_and_closes_connection():
    llm = OpenRouterLLM("test-key", "https://example.test/v1", "test/model", "test/embed")
    stream = FakeStream([FakeChunk('{"response":"Partial'), FakeChunk(finish="length")])

    async def create(**kwargs):
        return stream

    async def delta(value):
        pass

    llm.client.chat.completions.create = create
    try:
        with pytest.raises(ProviderError, match="before completion"):
            await llm.stream_structured_answer([], delta)
    finally:
        await llm.close()
    assert stream.closed


@pytest.mark.asyncio
async def test_provider_context_limit_fails_without_truncation_or_request():
    llm = OpenRouterLLM(
        "test-key",
        "https://example.test/v1",
        "test/model",
        "test/embed",
        context_tokens=1000,
        max_output_tokens=200,
    )
    try:
        with pytest.raises(ContextLimitError):
            await llm.generate_structured_response(Answer, [{"role": "user", "content": "evidence " * 1000}])
    finally:
        await llm.close()


@pytest.mark.asyncio
async def test_embedding_batches_reorder_by_index_and_reject_bad_dimensions():
    llm = OpenRouterLLM(
        "test-key", "https://example.test/v1", "test/model", "test/embed", embedding_dimensions=2
    )
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        items = [
            SimpleNamespace(index=i, embedding=[float(i), 1.0]) for i in range(len(kwargs["input"]))
        ]
        return SimpleNamespace(
            data=list(reversed(items)), model_dump=lambda: {"usage": {"prompt_tokens": 3}}
        )

    llm.client.embeddings.create = create
    try:
        vectors = await llm.embed_texts([f"Text {i}" for i in range(65)])
        assert len(vectors) == 65 and vectors[0] == [0, 1] and vectors[63] == [63, 1]
        assert [len(request["input"]) for request in requests] == [64, 1]
        llm.embedding_dimensions = 3
        with pytest.raises(ProviderError, match="dimensions"):
            await llm.embed_texts(["Text"])
    finally:
        await llm.close()


class SummaryLLM:
    context_tokens = 4000
    max_output_tokens = 800

    def __init__(self):
        self.calls = []

    async def generate_structured_response(self, schema, messages):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        ids = [id for section in payload["sections"] for id in section["citation_ids"]]
        if schema is DimensionFinding:
            return DimensionFinding(text="Payment terms found.", status="found", citation_ids=ids)
        return DocumentInsights(
            summary="The contract defines payment terms.",
            category="Contract",
            tags=["payment"],
            key_insights=[KeyInsight(text="Payment terms are specified.", citation_ids=[ids[0]])],
            suggestions=["When is payment due?"],
            citation_ids=ids,
        )


@pytest.mark.asyncio
async def test_long_summary_processes_every_chunk_then_reduces():
    llm = SummaryLLM()
    evidence = [source(f"c{i}", "Payment details. " * 120) for i in range(9)]
    result = await generate_document_summary(llm, evidence, focus_areas=["Payment"], tone="executive")
    original_ids = {
        id
        for call in llm.calls
        for section in call["sections"]
        if "text" in section
        for id in section["citation_ids"]
    }
    assert original_ids == {item.id for item in evidence}
    assert len(llm.calls) > 1
    assert result["coverage"] == {"chunks_processed": 9, "total_chunks": 9, "complete": True}
    assert result["options"]["tone"] == "executive"


@pytest.mark.asyncio
async def test_comparison_preserves_all_selected_versions_and_reports_missing():
    llm = SummaryLLM()
    evidence = {
        "v1": [source()],
        "v2": [],
        "v3": [source("c3", version="v3")],
        "v4": [],
        "v5": [],
    }
    result = await generate_document_comparison(llm, evidence, ["Payment"])
    cells = result["rows"][0]["cells"]
    assert [cell["version_id"] for cell in cells] == list(evidence)
    assert cells[0]["status"] == "found" and cells[0]["citation_ids"] == ["c1"]
    assert cells[1]["status"] == "not_found" and cells[1]["citation_ids"] == []
    assert result["coverage"]["complete"]


@pytest.mark.asyncio
async def test_summary_rejects_citations_invented_by_provider():
    llm = SummaryLLM()

    async def generate_structured_response(schema, messages):
        return DocumentInsights(
            summary="Oops",
            category="Other",
            tags=[],
            key_insights=[],
            suggestions=[],
            citation_ids=["invented"],
        )

    llm.generate_structured_response = generate_structured_response
    with pytest.raises(InvalidCitationError):
        await generate_document_summary(llm, [source()])
