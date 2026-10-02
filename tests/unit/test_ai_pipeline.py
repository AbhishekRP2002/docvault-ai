import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from docvault.ai.graphs import InvalidCitationError, RewrittenQuestion, run_chat
from docvault.ai.insights import DimensionFinding, DocumentInsights, KeyInsight, compare, summarize
from docvault.ai.parsing import Block, chunk_blocks, parse_file, parse_text, split_spans
from docvault.ai.provider import (
    ContextLimitError,
    OpenRouterAI,
    ProviderError,
    response_prefix,
    strict_schema,
    token_count,
)
from docvault.ai.types import Answer, Evidence


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
        prefix = response_prefix(raw[:index])
        assert prefix.startswith(previous)
        assert value.startswith(prefix)
        observed_before_completion |= bool(prefix) and index < len(raw)
        previous = prefix
    assert previous == value
    assert observed_before_completion


def test_answer_contract_and_strict_schema():
    with pytest.raises(ValidationError):
        Answer(response="Hi", suggestions=["a", "b", "c", "d"], citation_ids=[], outcome="answered")
    schema = strict_schema(Answer)["json_schema"]
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
    spans = split_spans(text, 97)
    assert "".join(text[a:b] for a, b in spans) == text
    assert all(token_count(text[a:b]) <= 97 for a, b in spans)
    parsed = parse_text(text, "terms.txt")
    assert parsed.text == text
    assert "".join(chunk.text for chunk in parsed.chunks) == text
    assert all(chunk.token_count <= 600 for chunk in parsed.chunks)
    for chunk in parsed.chunks:
        assert text[chunk.location["char_start"] : chunk.location["char_end"]] == chunk.text


def test_txt_citations_refer_to_exact_lines_and_characters(tmp_path):
    text = "# Terms\nPayment due in 30 days.\n\nRenewal is annual.\n"
    path = tmp_path / "terms.txt"
    path.write_text(text)
    parsed = parse_file(path, "text/plain", "terms.txt")
    assert parsed.chunks[0].location["line_start"] == 1
    assert parsed.chunks[-1].location["line_start"] == 4
    for chunk in parsed.chunks:
        assert text[chunk.location["char_start"] : chunk.location["char_end"]] == chunk.text
    with pytest.raises(ValueError, match="No readable"):
        parse_text("\n  \n", "empty.txt")
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="UTF-8"):
        parse_file(path, "text/plain", "bad.txt")


def test_tables_repeat_headers_and_preserve_all_rows():
    header = "| item | price |\n| --- | --- |\n"
    rows = [f"| Product {index} | {index} USD |\n" for index in range(300)]
    chunks = chunk_blocks(
        [Block(header + "".join(rows), {"item_id": "table1"}, ("Pricing",), True)], "quote.pdf"
    )
    assert len(chunks) > 1
    assert all(chunk.text.startswith(header) for chunk in chunks)
    assert all(chunk.token_count <= 600 for chunk in chunks)
    assert "".join(chunk.text[len(header) :] for chunk in chunks) == "".join(rows)
    assert chunks[-1].location["table_row_end"] == 302


def test_docling_preserves_pages_headings_and_rejects_partial_conversion(monkeypatch, tmp_path):
    from docvault.ai import parsing

    provenance = SimpleNamespace(
        page_no=3,
        charspan=(0, 24),
        bbox=SimpleNamespace(model_dump=lambda **kwargs: {"l": 10, "t": 20, "r": 100, "b": 30}),
    )
    heading = SimpleNamespace(
        label="section_header", text="Terms", self_ref="#/texts/0", prov=[provenance], level=1
    )
    paragraph = SimpleNamespace(
        label="text", text="Payment is due in 30 days.", self_ref="#/texts/1", prov=[provenance]
    )
    document = SimpleNamespace(
        iterate_items=lambda: [(heading, 1), (paragraph, 1)],
        pages={1: {}, 2: {}, 3: {}},
        export_to_markdown=lambda: "# Terms\nPayment is due in 30 days.",
    )
    monkeypatch.setattr(parsing.importlib.metadata, "version", lambda name: "test")
    parsed = parsing.parse_docling_document(document, "contract.pdf", is_pdf=True)
    assert parsed.page_count == 3
    assert len(parsed.chunks) == 1
    assert parsed.chunks[0].location["page"] == 3
    assert parsed.chunks[0].location["headings"] == ["Terms"]
    assert parsed.chunks[0].location["spans"][0]["bbox"]["l"] == 10
    source_spans = parsed.chunks[0].location["source_spans"]
    assert [span["item_id"] for span in source_spans] == ["#/texts/0", "#/texts/1"]
    assert parsed.chunks[0].text[source_spans[1]["text_char_start"] :] == paragraph.text
    converter = SimpleNamespace(
        convert=lambda *args, **kwargs: SimpleNamespace(status="partial_success")
    )
    monkeypatch.setattr(parsing, "_converter", lambda: converter)
    with pytest.raises(ValueError, match="incomplete"):
        parse_file(tmp_path / "contract.pdf", "application/pdf", "contract.pdf")


class ChatAI:
    def __init__(self, answer=None, rewrite=None):
        self.answer = answer or Answer(
            response="Payment is due in 30 days. [c1]",
            suggestions=["When does it renew?"],
            citation_ids=["c1"],
            outcome="answered",
        )
        self.rewrite = rewrite
        self.generations = 0

    async def structured(self, schema, messages):
        assert schema is RewrittenQuestion
        return self.rewrite

    async def stream_answer(self, messages, on_delta):
        self.generations += 1
        await on_delta(self.answer.response)
        return self.answer


@pytest.mark.asyncio
async def test_graph_rewrites_followups_then_retrieves_fresh_evidence():
    queries, deltas = [], []

    async def retrieve(query):
        queries.append(query)
        return [source()]

    async def delta(value):
        deltas.append(value)

    ai = ChatAI(
        rewrite=RewrittenQuestion(
            question="When is the contract payment due?",
            needs_clarification=False,
            clarification="",
        )
    )
    answer, evidence, query = await run_chat(
        "When is it due?",
        [
            {"role": "user", "content": "Tell me about payment."},
        ],
        retrieve,
        ai,
        delta,
    )
    assert queries == [query] == ["When is the contract payment due?"]
    assert evidence == [source()]
    assert answer.citation_ids == ["c1"]
    assert "".join(deltas) == answer.response


@pytest.mark.asyncio
async def test_graph_without_evidence_does_not_generate_or_fake_deltas():
    ai, deltas = ChatAI(), []

    async def retrieve(query):
        return []

    async def delta(value):
        deltas.append(value)

    answer, evidence, _ = await run_chat("Is there a fee?", [], retrieve, ai, delta)
    assert answer.outcome == "insufficient_evidence"
    assert evidence == [] and deltas == [] and ai.generations == 0


@pytest.mark.asyncio
async def test_graph_ambiguous_followup_skips_retrieval():
    ai = ChatAI(
        rewrite=RewrittenQuestion(
            question="Which contract?",
            needs_clarification=True,
            clarification="Which contract do you mean?",
        )
    )

    async def retrieve(query):
        pytest.fail("Ambiguous questions must not run retrieval")

    async def delta(value):
        pytest.fail("Clarification is a final response, not a fake model stream")

    answer, _, _ = await run_chat(
        "What about it?", [{"role": "user", "content": "Compare contracts"}], retrieve, ai, delta
    )
    assert answer.outcome == "clarification_needed"
    assert ai.generations == 0


@pytest.mark.asyncio
async def test_graph_rejects_invented_citation_ids():
    ai = ChatAI(
        Answer(
            response="A fee exists", suggestions=[], citation_ids=["invented"], outcome="answered"
        )
    )

    async def retrieve(query):
        return [source()]

    async def delta(value):
        pass

    with pytest.raises(InvalidCitationError):
        await run_chat("Fee?", [], retrieve, ai, delta)


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

    async def on_usage(value):
        usage.append(value)

    ai = OpenRouterAI(
        "test-key", "https://example.test/v1", "test/model", "test/embed", on_usage=on_usage
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

    ai.client.chat.completions.create = create
    try:
        answer = await ai.stream_answer([{"role": "user", "content": "Payment?"}], delta)
    finally:
        await ai.close()
    assert deltas == ["Payment", " in 30 days."]
    assert answer.response == "Payment in 30 days."
    assert stream.closed
    assert requested[0]["stream"] is True
    assert requested[0]["extra_body"]["provider"]["require_parameters"] is True
    assert usage[0]["cost_usd"] == 0.003 and usage[0]["input_tokens"] == 42
    assert usage[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_provider_rejects_truncated_stream_and_closes_connection():
    ai = OpenRouterAI("test-key", "https://example.test/v1", "test/model", "test/embed")
    stream = FakeStream([FakeChunk('{"response":"Partial'), FakeChunk(finish="length")])

    async def create(**kwargs):
        return stream

    async def delta(value):
        pass

    ai.client.chat.completions.create = create
    try:
        with pytest.raises(ProviderError, match="before completion"):
            await ai.stream_answer([], delta)
    finally:
        await ai.close()
    assert stream.closed


@pytest.mark.asyncio
async def test_provider_context_limit_fails_without_truncation_or_request():
    ai = OpenRouterAI(
        "test-key",
        "https://example.test/v1",
        "test/model",
        "test/embed",
        context_tokens=1000,
        max_output_tokens=200,
    )
    try:
        with pytest.raises(ContextLimitError):
            await ai.structured(Answer, [{"role": "user", "content": "evidence " * 1000}])
    finally:
        await ai.close()


@pytest.mark.asyncio
async def test_embedding_batches_reorder_by_index_and_reject_bad_dimensions():
    ai = OpenRouterAI(
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

    ai.client.embeddings.create = create
    try:
        vectors = await ai.embed([f"Text {i}" for i in range(65)])
        assert len(vectors) == 65 and vectors[0] == [0, 1] and vectors[63] == [63, 1]
        assert [len(request["input"]) for request in requests] == [64, 1]
        ai.embedding_dimensions = 3
        with pytest.raises(ProviderError, match="dimensions"):
            await ai.embed(["Text"])
    finally:
        await ai.close()


class SummaryAI:
    context_tokens = 4000
    max_output_tokens = 800

    def __init__(self):
        self.calls = []

    async def structured(self, schema, messages):
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
    ai = SummaryAI()
    evidence = [source(f"c{i}", "Payment details. " * 120) for i in range(9)]
    result = await summarize(ai, evidence, focus_areas=["Payment"], tone="executive")
    original_ids = {
        id
        for call in ai.calls
        for section in call["sections"]
        if "text" in section
        for id in section["citation_ids"]
    }
    assert original_ids == {item.id for item in evidence}
    assert len(ai.calls) > 1
    assert result["coverage"] == {"chunks_processed": 9, "total_chunks": 9, "complete": True}
    assert result["options"]["tone"] == "executive"


@pytest.mark.asyncio
async def test_comparison_preserves_all_selected_versions_and_reports_missing():
    ai = SummaryAI()
    evidence = {
        "v1": [source()],
        "v2": [],
        "v3": [source("c3", version="v3")],
        "v4": [],
        "v5": [],
    }
    result = await compare(ai, evidence, ["Payment"])
    cells = result["rows"][0]["cells"]
    assert [cell["version_id"] for cell in cells] == list(evidence)
    assert cells[0]["status"] == "found" and cells[0]["citation_ids"] == ["c1"]
    assert cells[1]["status"] == "not_found" and cells[1]["citation_ids"] == []
    assert result["coverage"]["complete"]


@pytest.mark.asyncio
async def test_summary_rejects_citations_invented_by_provider():
    ai = SummaryAI()

    async def structured(schema, messages):
        return DocumentInsights(
            summary="Oops",
            category="Other",
            tags=[],
            key_insights=[],
            suggestions=[],
            citation_ids=["invented"],
        )

    ai.structured = structured
    with pytest.raises(InvalidCitationError):
        await summarize(ai, [source()])
