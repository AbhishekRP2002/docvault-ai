import json
from types import SimpleNamespace

import pytest
from openai.types import CreateEmbeddingResponse
from pydantic import ValidationError

from docvault.llm.config import GenerationModelConfig, LLMSettings, LLMTask
from docvault.llm.graphs import (
    InvalidCitationError,
    run_document_chat_workflow,
)
from docvault.llm.insights import (
    generate_document_comparison,
    generate_document_summary,
)
from docvault.llm.models import (
    ChatGenerationLLMResponse,
    CitedKeyInsight,
    ComparisonDimensionLLMResponse,
    Evidence,
    InputQueryRewriteLLMResponse,
    InsightsGenerationLLMResponse,
)
from docvault.llm.provider import (
    DeltaCallback,
    OpenRouterLLM,
    ProviderError,
    Schema,
    token_count,
)
from docvault.parsing import (
    parse_document_file,
    parse_text_document,
)


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


def test_answer_contract_and_strict_schema():
    with pytest.raises(ValidationError):
        ChatGenerationLLMResponse(
            response="Hi", suggestions=["a", "b", "c", "d"], citation_ids=[], outcome="answered"
        )
    schema = ChatGenerationLLMResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
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
    assert all(chunk.token_count <= 750 for chunk in parsed.chunks)
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

    from docling_core.types.doc.document import DoclingDocument
    from docling_core.types.doc.items.table.table_data import TableCell, TableData

    from docvault.parsing import chunk_docling_document

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
    assert all(chunk.token_count <= 750 for chunk in parsed.chunks)
    actual_rows = re.findall(
        r"\|\s*Product (\d+)\s*\|\s*(\d+) USD\s*\|", "\n".join(chunk.text for chunk in table_chunks)
    )
    assert actual_rows == [(str(index), str(index)) for index in range(300)]


def test_docling_preserves_pages_headings_and_rejects_partial_conversion(monkeypatch, tmp_path):
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.common.reference import ProvenanceItem
    from docling_core.types.doc.document import DoclingDocument
    from docling_core.types.doc.labels import DocItemLabel

    from docvault import parsing

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


class ChatLLM(OpenRouterLLM):
    def __init__(
        self,
        answer: ChatGenerationLLMResponse | None = None,
        input_query_rewrite: InputQueryRewriteLLMResponse | None = None,
    ):
        """Configure deterministic chat and rewrite results without a provider connection."""
        self.answer = answer or ChatGenerationLLMResponse(
            response="Payment is due in 30 days. [c1]",
            suggestions=["When does it renew?"],
            citation_ids=["c1"],
            outcome="answered",
        )
        self.input_query_rewrite = input_query_rewrite
        self.generations = 0

    async def generate_structured_response(
        self, schema: type[Schema], messages: list[dict], *, task: LLMTask
    ) -> Schema:
        """Validate the configured rewrite against the workflow-requested schema."""
        assert schema is InputQueryRewriteLLMResponse
        assert task == "input_query_rewrite"
        assert self.input_query_rewrite is not None
        return schema.model_validate(self.input_query_rewrite.model_dump())

    async def stream_structured_answer(
        self, messages: list[dict], on_delta: DeltaCallback
    ) -> ChatGenerationLLMResponse:
        """Emit the configured answer once and return the same validated response."""
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
        input_query_rewrite=InputQueryRewriteLLMResponse(
            standalone_question="When is the contract payment due?",
            needs_clarification=False,
            clarification_question="",
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

    answer, evidence, _ = await run_document_chat_workflow(
        "Is there a fee?", [], retrieve_relevant_chunks, llm, delta
    )
    assert answer.outcome == "insufficient_evidence"
    assert evidence == [] and deltas == [] and llm.generations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clarification_question,expected_response",
    [
        ("Which contract do you mean?", "Which contract do you mean?"),
        ("", "Which document or detail do you mean?"),
    ],
)
async def test_graph_ambiguous_followup_skips_retrieval(clarification_question, expected_response):
    llm = ChatLLM(
        input_query_rewrite=InputQueryRewriteLLMResponse(
            standalone_question="Which contract?",
            needs_clarification=True,
            clarification_question=clarification_question,
        )
    )

    async def retrieve_relevant_chunks(query):
        pytest.fail("Ambiguous questions must not run retrieval")

    async def delta(value):
        pytest.fail("Clarification is a final response, not a fake model stream")

    answer, _, _ = await run_document_chat_workflow(
        "What about it?",
        [{"role": "user", "content": "Compare contracts"}],
        retrieve_relevant_chunks,
        llm,
        delta,
    )
    assert answer.outcome == "clarification_needed"
    assert answer.response == expected_response
    assert llm.generations == 0


@pytest.mark.asyncio
async def test_graph_rejects_invented_citation_ids():
    llm = ChatLLM(
        ChatGenerationLLMResponse(
            response="A fee exists", suggestions=[], citation_ids=["invented"], outcome="answered"
        )
    )

    async def retrieve_relevant_chunks(query):
        return [source()]

    async def delta(value):
        pass

    with pytest.raises(InvalidCitationError):
        await run_document_chat_workflow("Fee?", [], retrieve_relevant_chunks, llm, delta)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [64, 16])
async def test_embedding_batches_reorder_by_index_and_reject_bad_dimensions(
    batch_size, monkeypatch
):
    llm = OpenRouterLLM(
        LLMSettings(
            _env_file=None,
            openrouter_api_key="test-key",
            embedding_dimensions=2,
            openrouter_embedding_max_batch_inputs=batch_size,
        )
    )
    requests = []

    async def create(**kwargs):
        requests.append(kwargs)
        items = [
            {"object": "embedding", "index": i, "embedding": [float(i), 1.0]}
            for i in range(len(kwargs["input"]))
        ]
        return CreateEmbeddingResponse.model_validate(
            {
                "object": "list",
                "model": "test/embedding",
                "data": list(reversed(items)),
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            }
        )

    monkeypatch.setattr(llm.client.embeddings, "create", create)
    try:
        vectors = await llm.embed_texts([f"Text {i}" for i in range(65)])
        assert vectors == [[float(index % batch_size), 1.0] for index in range(65)]
        assert [len(request["input"]) for request in requests] == [batch_size] * (
            65 // batch_size
        ) + [65 % batch_size]
        llm.embedding_configuration = llm.embedding_configuration.model_copy(
            update={"dimensions": 3}
        )
        with pytest.raises(ProviderError, match="dimensions"):
            await llm.embed_texts(["Text"])
    finally:
        await llm.close()


class SummaryLLM(OpenRouterLLM):
    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Use a small validated context to exercise recursive map and reduction."""
        return GenerationModelConfig(
            model=f"test/{task}", context_tokens=4000, max_output_tokens=800
        )

    def __init__(self):
        """Track each generated document-analysis request."""
        self.calls = []

    async def generate_structured_response(
        self, schema: type[Schema], messages: list[dict], *, task: LLMTask
    ) -> Schema:
        """Validate deterministic findings against the requested analysis response schema."""
        assert task == ("comparison" if schema is ComparisonDimensionLLMResponse else "summary")
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        ids = [id for section in payload["sections"] for id in section["citation_ids"]]
        if task == "comparison":
            result = ComparisonDimensionLLMResponse(
                finding_text="Payment terms found.", status="found", citation_ids=ids
            )
        else:
            result = InsightsGenerationLLMResponse(
                summary="The contract defines payment terms.",
                category="Contract",
                tags=["payment"],
                key_insights=[
                    CitedKeyInsight(
                        insight_text="Payment terms are specified.", citation_ids=[ids[0]]
                    )
                ],
                suggestions=["When is payment due?"],
                citation_ids=ids,
            )
        return schema.model_validate(result.model_dump())


@pytest.mark.asyncio
async def test_long_summary_processes_every_chunk_then_reduces():
    llm = SummaryLLM()
    evidence = [source(f"c{i}", "Payment details. " * 120) for i in range(9)]
    result = await generate_document_summary(
        llm, evidence, focus_areas=["Payment"], tone="executive"
    )
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
    assert result["key_insights"] == [
        {"text": "Payment terms are specified.", "citation_ids": ["c0"]}
    ]


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
    assert cells[0]["text"] == "Payment terms found."
    assert cells[1]["text"] == "No supporting information was found."
    assert all("finding_text" not in cell for cell in cells)
    assert result["coverage"]["complete"]


@pytest.mark.asyncio
async def test_summary_rejects_citations_invented_by_provider(monkeypatch):
    llm = SummaryLLM()

    async def generate_structured_response(schema, messages, *, task):
        return InsightsGenerationLLMResponse(
            summary="Oops",
            category="Other",
            tags=[],
            key_insights=[],
            suggestions=[],
            citation_ids=["invented"],
        )

    monkeypatch.setattr(llm, "generate_structured_response", generate_structured_response)
    with pytest.raises(InvalidCitationError):
        await generate_document_summary(llm, [source()])
