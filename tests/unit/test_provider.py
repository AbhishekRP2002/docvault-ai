"""Exercise the installed SDK and Pydantic parsing against deterministic HTTP/SSE responses."""

import asyncio
import json

import httpx
import pytest

from docvault.llm.config import LLMSettings
from docvault.llm.models import ComparisonDimensionLLMResponse, InsightsGenerationLLMResponse
from docvault.llm.provider import OpenRouterLLM, ProviderError

PROVIDER_USAGE = {
    "prompt_tokens": 42,
    "completion_tokens": 12,
    "total_tokens": 54,
    "cost": 0.003,
    "prompt_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 6, "audio_tokens": 0},
    "completion_tokens_details": {"reasoning_tokens": 3},
    "cost_details": {"upstream_inference_cost": 0.002},
}


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, contents, finish="stop", refusal=None, usage=None):
        self.contents = contents
        self.finish = finish
        self.refusal = refusal
        self.usage = usage
        self.closed = False
        self.completed = False

    async def __aiter__(self):
        for content in self.contents:
            yield self.event({"content": content})
        yield self.event({"refusal": self.refusal} if self.refusal else {}, self.finish)
        if self.usage:
            yield self.event({}, usage=self.usage)
        self.completed = True
        yield b"data: [DONE]\n\n"

    def event(self, delta, finish=None, usage=None):
        return (
            b"data: "
            + json.dumps(
                {
                    "id": "req1",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test/actual",
                    "usage": usage,
                    "choices": []
                    if usage
                    else [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            ).encode()
            + b"\n\n"
        )

    async def aclose(self):
        self.closed = True


def create_provider(handler, usage=None, **overrides):
    settings = LLMSettings(
        _env_file=None,
        openrouter_api_key="test-key",
        openrouter_base_url="https://example.test/v1",
        **overrides,
    )
    llm = OpenRouterLLM(settings, usage)
    # Inject the HTTP boundary while exercising the real SDK's parsing/stream helpers.
    llm.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def completion(content=None, *, refusal=None, finish="stop", usage=None):
    return {
        "id": "req1",
        "object": "chat.completion",
        "created": 0,
        "model": "test/actual",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
        "usage": usage if usage is not None else PROVIDER_USAGE,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        'A quote: "yes". A slash: \\.\nA line.',
        "Renewal is due. 🧾 Café हिंदी",
        'This says response: "not another field"',
    ],
)
async def test_sdk_stream_emits_stable_unicode_text_before_completion(value):
    raw = json.dumps(
        {
            "suggestions": ["Next?"],
            "response": value,
            "citation_ids": ["c1"],
            "outcome": "answered",
            "response_kind": "document_answer",
        }
    )
    stream = ResponseStream(
        list(raw),
        usage=PROVIDER_USAGE,
    )
    requested, deltas, usage = [], [], []

    def handler(request):
        requested.append(json.loads(request.content))
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async def record(value):
        usage.append(value)

    async def delta(text):
        assert not stream.closed and not stream.completed
        deltas.append(text)
        assert value.startswith("".join(deltas))

    llm = create_provider(handler, record)
    try:
        turn = await llm.stream_chat_turn([], [], delta, evidence_ids=["c1"])
        assert turn.answer is not None
        answer = turn.answer
    finally:
        await llm.close()
    assert "".join(deltas) == answer.response == value
    assert len(deltas) > 1
    assert stream.closed
    request = requested[0]
    assert request["stream"]
    assert "stream_options" not in request and "usage" not in request
    assert request["provider"]["require_parameters"]
    assert request["response_format"]["json_schema"]["strict"]
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert usage[0]["status"] == "succeeded" and usage[0]["cost_usd"] == 0.003
    assert usage[0]["model"] == "test/actual"
    assert usage[0]["input_tokens"] == 42
    assert usage[0]["output_tokens"] == 12
    assert usage[0]["cached_tokens"] == 8
    assert usage[0]["request_id"] == "req1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish,refusal,content",
    [
        ("length", None, '{"response":"Partial'),
        (None, None, '{"response":"Partial'),
        ("content_filter", None, '{"response":"Partial'),
        ("stop", "Declined", ""),
        (
            "stop",
            None,
            '{"response":"Invalid","suggestions":[],"citation_ids":[],"outcome":"oops","response_kind":"assistant_help"}',
        ),
    ],
)
async def test_sdk_stream_rejects_incomplete_refused_or_invalid_output(finish, refusal, content):
    stream = ResponseStream([content], finish, refusal)
    usage = []

    def handler(request):
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async def record(value):
        usage.append(value)

    async def delta(text):
        pass

    llm = create_provider(handler, record)
    try:
        with pytest.raises(ProviderError):
            await llm.stream_chat_turn([], [], delta, evidence_ids=["c1"])
    finally:
        await llm.close()
    assert stream.closed
    assert usage[0]["status"] == "failed"
    assert usage[0]["input_tokens"] is None
    assert usage[0]["output_tokens"] is None
    assert usage[0]["cached_tokens"] is None
    assert usage[0]["cost_usd"] is None


@pytest.mark.asyncio
async def test_stream_cancellation_closes_connection_and_records_failure():
    stream = ResponseStream(['{"response":"Payment', ' terms"}'])
    usage = []

    def handler(request):
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async def record(value):
        usage.append(value)

    async def delta(text):
        raise asyncio.CancelledError

    llm = create_provider(handler, record)
    try:
        with pytest.raises(asyncio.CancelledError):
            await llm.stream_chat_turn([], [], delta, evidence_ids=["c1"])
    finally:
        await llm.close()
    assert stream.closed and usage[0]["status"] == "failed"
    assert usage[0]["input_tokens"] is None and usage[0]["output_tokens"] is None
    assert usage[0]["cached_tokens"] is None and usage[0]["cost_usd"] is None


@pytest.mark.asyncio
async def test_sdk_parses_pydantic_model_with_task_specific_configuration():
    requested, usage = [], []

    def handler(request):
        requested.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                json.dumps(
                    {
                        "finding_text": "Payment is due in 30 days.",
                        "status": "found",
                        "citation_ids": ["c1"],
                    }
                )
            ),
        )

    async def record(value):
        usage.append(value)

    llm = create_provider(
        handler,
        record,
        openrouter_comparison_model="test/comparison",
        openrouter_comparison_max_output_tokens=512,
        openrouter_comparison_temperature=0,
    )
    try:
        parsed = await llm.generate_structured_response(
            ComparisonDimensionLLMResponse, [], task="comparison"
        )
    finally:
        await llm.close()
    assert isinstance(parsed, ComparisonDimensionLLMResponse)
    assert parsed.finding_text == "Payment is due in 30 days."
    assert parsed.status == "found"
    request = requested[0]
    assert request["model"] == "test/comparison" and request["max_tokens"] == 512
    assert request["temperature"] == 0
    assert request["response_format"]["json_schema"]["name"] == "ComparisonDimensionLLMResponse"
    assert usage[0]["status"] == "succeeded" and usage[0]["cost_usd"] == 0.003
    assert usage[0]["input_tokens"] == 42 and usage[0]["output_tokens"] == 12
    assert usage[0]["cached_tokens"] == 8 and usage[0]["request_id"] == "req1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,refusal,finish",
    [
        ("not JSON", None, "stop"),
        (
            '{"summary":"Hi","category":"General","tags":[],"key_insights":[],"suggestions":["1","2","3","4"],"citation_ids":["c1"]}',
            None,
            "stop",
        ),
        (None, "Declined", "stop"),
        (None, None, "length"),
        (None, None, "content_filter"),
    ],
)
async def test_sdk_parse_failures_preserve_provider_usage(content, refusal, finish):
    usage = []

    def handler(request):
        return httpx.Response(200, json=completion(content, refusal=refusal, finish=finish))

    async def record(value):
        usage.append(value)

    llm = create_provider(handler, record)
    try:
        with pytest.raises(ProviderError):
            await llm.generate_structured_response(
                InsightsGenerationLLMResponse, [], task="summary"
            )
    finally:
        await llm.close()
    assert usage[0]["status"] == "failed" and usage[0]["cost_usd"] == 0.003
    assert usage[0]["input_tokens"] == 42 and usage[0]["output_tokens"] == 12
    assert usage[0]["cached_tokens"] == 8


@pytest.mark.asyncio
async def test_configured_context_does_not_reject_or_truncate_generation_input():
    requested = []

    def handler(request):
        """Accept complete input beyond a configured planning fallback as the real provider may."""
        requested.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=completion(
                json.dumps(
                    {
                        "finding_text": "The document describes payment.",
                        "status": "found",
                        "citation_ids": ["c1"],
                    }
                )
            ),
        )

    llm = create_provider(
        handler,
        openrouter_comparison_context_tokens=1000,
        openrouter_comparison_max_output_tokens=200,
    )
    try:
        await llm.generate_structured_response(
            ComparisonDimensionLLMResponse,
            [{"role": "user", "content": "evidence " * 1000}],
            task="comparison",
        )
    finally:
        await llm.close()
    assert requested[0]["messages"][0]["content"] == "evidence " * 1000


async def test_configured_context_reservation_does_not_override_actual_provider_capacity():
    settings = LLMSettings(
        _env_file=None,
        openrouter_api_key="test-key",
        openrouter_comparison_context_tokens=1000,
        openrouter_comparison_max_output_tokens=1000,
    )
    llm = OpenRouterLLM(settings)
    await llm.close()


@pytest.mark.asyncio
async def test_empty_stream_fails_as_provider_error():
    def handler(request):
        return httpx.Response(
            200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    async def delta(text):
        pytest.fail("An empty stream cannot emit answer text")

    llm = create_provider(handler)
    try:
        with pytest.raises(ProviderError, match="no completion"):
            await llm.stream_chat_turn([], [], delta, evidence_ids=["c1"])
    finally:
        await llm.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "provider_usage,expected",
    [
        (None, (None, None, None, None)),
        (
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 0},
                "cost": 0,
            },
            (0, 0, 0, 0),
        ),
        (
            {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
            (42, 12, None, None),
        ),
    ],
)
async def test_usage_distinguishes_unreported_values_from_reported_zero(
    streaming, provider_usage, expected
):
    """Preserve unknown accounting and true zero charges through full JSON and final SSE parsing."""
    content = json.dumps(
        {
            "response": "Answer",
            "suggestions": [],
            "citation_ids": ["c1"],
            "outcome": "answered",
            "response_kind": "document_answer",
        }
    )
    usage = []

    def handler(request):
        """Return the selected usage representation at the actual SDK HTTP boundary."""
        if streaming:
            return httpx.Response(
                200,
                stream=ResponseStream([content], usage=provider_usage),
                headers={"content-type": "text/event-stream"},
            )
        response = completion(
            json.dumps({"finding_text": "Answer", "status": "found", "citation_ids": ["c1"]})
        )
        response["usage"] = provider_usage
        return httpx.Response(200, json=response)

    async def record(value):
        """Capture the same accounting payload used by durable LLMCall persistence."""
        usage.append(value)

    async def delta(text):
        """Consume provisional text without adding accounting side effects."""
        pass

    llm = create_provider(handler, record)
    try:
        if streaming:
            await llm.stream_chat_turn([], [], delta, evidence_ids=["c1"])
        else:
            await llm.generate_structured_response(
                ComparisonDimensionLLMResponse, [], task="comparison"
            )
    finally:
        await llm.close()
    assert len(usage) == 1 and usage[0]["status"] == "succeeded"
    assert (
        tuple(
            usage[0][key] for key in ("input_tokens", "output_tokens", "cached_tokens", "cost_usd")
        )
        == expected
    )


@pytest.mark.asyncio
async def test_embedding_batches_record_sdk_usage_and_unknown_cost_separately():
    """Preserve embedding prompt/cost extras per batch and use zero completion tokens only."""
    requested, usage = [], []

    def handler(request):
        """Return two ordered embeddings batches with known and unreported provider costs."""
        body = json.loads(request.content)
        requested.append(body)
        batch_number = len(requested)
        provider_usage: dict[str, object] = {
            "prompt_tokens": len(body["input"]) * 3,
            "total_tokens": len(body["input"]) * 3,
        }
        if batch_number == 1:
            provider_usage.update({"cost": 0.0001, "prompt_tokens_details": {"cached_tokens": 0}})
        return httpx.Response(
            200,
            json={
                "id": f"embedding-{batch_number}",
                "object": "list",
                "model": "test/actual-embedding",
                "data": [
                    {"object": "embedding", "index": index, "embedding": [batch_number, index]}
                    for index in reversed(range(len(body["input"])))
                ],
                "usage": provider_usage,
            },
        )

    async def record(value):
        """Capture the provider accounting emitted once per embeddings HTTP response."""
        usage.append(value)

    llm = create_provider(
        handler,
        record,
        embedding_dimensions=2,
        openrouter_embedding_max_batch_inputs=2,
        openrouter_embedding_model="test/requested-embedding",
    )
    try:
        vectors = await llm.embed_texts(["one", "two", "three"])
    finally:
        await llm.close()
    assert vectors == [[1, 0], [1, 1], [2, 0]]
    assert [body["input"] for body in requested] == [["one", "two"], ["three"]]
    assert all(body["model"] == "test/requested-embedding" for body in requested)
    assert len(usage) == 2
    assert [call["input_tokens"] for call in usage] == [6, 3]
    assert [call["output_tokens"] for call in usage] == [0, 0]
    assert [call["cost_usd"] for call in usage] == [0.0001, None]
    assert [call["cached_tokens"] for call in usage] == [0, None]
    assert [call["request_id"] for call in usage] == ["embedding-1", "embedding-2"]
    assert all(call["model"] == "test/actual-embedding" for call in usage)
    assert all(call["operation"] == "embedding" and call["status"] == "succeeded" for call in usage)


@pytest.mark.asyncio
async def test_invalid_embedding_dimensions_preserve_reported_usage():
    """Record returned embedding charges even when a vector fails local validation."""
    usage = []

    def handler(request):
        """Return a charged embedding response with an incorrect vector dimension."""
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "test/embedding",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3, "cost": 0.0001},
            },
        )

    async def record(value):
        """Capture billing information independently from the vector validation outcome."""
        usage.append(value)

    llm = create_provider(handler, record, embedding_dimensions=2)
    try:
        with pytest.raises(ProviderError, match="dimensions"):
            await llm.embed_texts(["one"])
    finally:
        await llm.close()
    assert len(usage) == 1 and usage[0]["status"] == "failed"
    assert usage[0]["input_tokens"] == 3 and usage[0]["output_tokens"] == 0
    assert usage[0]["cost_usd"] == 0.0001


@pytest.mark.asyncio
async def test_model_context_metadata_is_cached_across_tasks_and_clients(monkeypatch):
    """Actual model capacity overrides a configured fallback without recording generation usage."""
    monkeypatch.setattr("docvault.llm.provider.MODEL_CONTEXT_CACHE", {})
    requested, usage = [], []

    def handler(request):
        """Return the official OpenRouter context metadata via the installed SDK models API."""
        requested.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "test/actual-model",
                        "created": 0,
                        "object": "model",
                        "owned_by": "test",
                        "context_length": 1000000,
                    }
                ]
            },
        )

    async def record(value):
        """Fail if unbilled metadata requests are recorded as generation usage."""
        usage.append(value)

    options = {
        "openrouter_chat_model": "test/actual-model",
        "openrouter_comparison_model": "test/actual-model",
        "openrouter_context_tokens": 1000,
        "openrouter_comparison_context_tokens": 1000,
    }
    first, second = (
        create_provider(handler, record, **options),
        create_provider(handler, record, **options),
    )
    try:
        assert await first.resolve_model_context_tokens("chat") == 1000000
        assert await first.resolve_model_context_tokens("comparison") == 1000000
        assert await second.resolve_model_context_tokens("chat") == 1000000
    finally:
        await first.close()
        await second.close()
    assert requested == ["/v1/models"] and usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": None}),
        httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "test/actual-model",
                        "created": 0,
                        "object": "model",
                        "owned_by": "test",
                        "context_length": True,
                    }
                ]
            },
        ),
    ],
)
async def test_model_metadata_unavailable_uses_configured_planning_fallback(monkeypatch, response):
    """Unavailable/invalid metadata does not create a local generation input rejection."""
    monkeypatch.setattr("docvault.llm.provider.MODEL_CONTEXT_CACHE", {})
    llm = create_provider(
        lambda request: response,
        openrouter_chat_model="test/actual-model",
        openrouter_context_tokens=7777,
    )
    try:
        assert await llm.resolve_model_context_tokens("chat") == 7777
    finally:
        await llm.close()
