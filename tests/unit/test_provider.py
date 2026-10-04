"""Exercise the installed SDK and Pydantic parsing against deterministic HTTP/SSE responses."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from docvault.llm.config import LLMSettings
from docvault.llm.provider import ContextLimitError, OpenRouterLLM, ProviderError
from docvault.llm.types import Answer, RewrittenQuestion


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
        "usage": usage
        or {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54, "cost": 0.003},
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
        {"suggestions": ["Next?"], "response": value, "citation_ids": ["c1"], "outcome": "answered"}
    )
    stream = ResponseStream(
        list(raw),
        usage={"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54, "cost": 0.003},
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
        answer = await llm.stream_structured_answer([], delta)
    finally:
        await llm.close()
    assert "".join(deltas) == answer.response == value
    assert len(deltas) > 1
    assert stream.closed
    request = requested[0]
    assert request["stream"] and request["stream_options"]["include_usage"]
    assert request["provider"]["require_parameters"]
    assert request["response_format"]["json_schema"]["strict"]
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert usage[0]["status"] == "succeeded" and usage[0]["cost_usd"] == 0.003
    assert usage[0]["model"] == "test/actual"


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
            '{"response":"Invalid","suggestions":[],"citation_ids":[],"outcome":"oops"}',
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
            await llm.stream_structured_answer([], delta)
    finally:
        await llm.close()
    assert stream.closed
    assert usage[0]["status"] == "failed"


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
            await llm.stream_structured_answer([], delta)
    finally:
        await llm.close()
    assert stream.closed and usage[0]["status"] == "failed"


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
                        "question": "When is payment due?",
                        "needs_clarification": False,
                        "clarification": "",
                    }
                )
            ),
        )

    async def record(value):
        usage.append(value)

    llm = create_provider(
        handler,
        record,
        openrouter_rewrite_model="test/rewrite",
        openrouter_rewrite_max_output_tokens=512,
        openrouter_rewrite_temperature=0,
    )
    try:
        parsed = await llm.generate_structured_response(RewrittenQuestion, [], task="rewrite")
    finally:
        await llm.close()
    assert isinstance(parsed, RewrittenQuestion)
    request = requested[0]
    assert request["model"] == "test/rewrite" and request["max_tokens"] == 512
    assert request["temperature"] == 0
    assert request["response_format"]["json_schema"]["name"] == "RewrittenQuestion"
    assert usage[0]["status"] == "succeeded" and usage[0]["cost_usd"] == 0.003


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,refusal,finish",
    [
        ("not JSON", None, "stop"),
        (
            '{"response":"Hi","suggestions":["1","2","3","4"],"citation_ids":[],"outcome":"answered"}',
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
            await llm.generate_structured_response(Answer, [], task="summary")
    finally:
        await llm.close()
    assert usage[0]["status"] == "failed" and usage[0]["cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_task_context_overflow_fails_before_http_without_truncation():
    def handler(request):
        pytest.fail("Context rejection must happen before a provider request")

    llm = create_provider(
        handler, openrouter_rewrite_context_tokens=1000, openrouter_rewrite_max_output_tokens=200
    )
    try:
        with pytest.raises(ContextLimitError):
            await llm.generate_structured_response(
                RewrittenQuestion, [{"role": "user", "content": "evidence " * 1000}], task="rewrite"
            )
    finally:
        await llm.close()


def test_invalid_task_capacity_is_rejected_before_client_creation():
    settings = LLMSettings(
        _env_file=None,
        openrouter_api_key="test-key",
        openrouter_comparison_context_tokens=1000,
        openrouter_comparison_max_output_tokens=1000,
    )
    with pytest.raises(ValidationError, match="context must exceed"):
        OpenRouterLLM(settings)


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
            await llm.stream_structured_answer([], delta)
    finally:
        await llm.close()
