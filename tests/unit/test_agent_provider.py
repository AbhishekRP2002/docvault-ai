"""Native strict tool parsing and classified streaming exercised through the installed SDK."""

import json

import httpx
import pytest
from pydantic import ValidationError
from test_provider import PROVIDER_USAGE, ResponseStream, create_provider

from docvault.llm.config import LLMSettings
from docvault.llm.prompts import build_generation_identity
from docvault.llm.provider import ProviderError
from docvault.tools.definitions import build_agent_tool_definitions
from docvault.tools.models import (
    TOOL_INPUT_MODELS,
    AgentChatGenerationLLMResponse,
    build_grounded_agent_response_model,
)


@pytest.mark.parametrize("allowed", [[], ["original-1", "original-2"]])
def test_native_final_schema_rejects_ids_outside_actual_evidence(allowed):
    """Operational identifiers cannot become citations even if the model emits valid JSON."""
    schema = build_grounded_agent_response_model(allowed)
    payload = dict(
        response="Current processing status.",
        suggestions=[],
        citation_ids=[],
        outcome="answered",
        response_kind="workspace_answer",
    )
    assert schema.model_validate(payload).citation_ids == []
    with pytest.raises(ValidationError):
        schema.model_validate({**payload, "citation_ids": ["job-id"]})
    definition = schema.model_json_schema()["properties"]["citation_ids"]
    if allowed:
        assert definition["items"]["enum"] == allowed
        assert schema.model_validate({**payload, "citation_ids": allowed}).citation_ids == allowed
    else:
        assert definition["maxItems"] == 0


class NativeToolStream(ResponseStream):
    """Serve fragmented OpenAI tool-call SSE chunks through the SDK's real accumulator."""

    def __init__(
        self, calls=None, *, finish="tool_calls", content=None, refusal=None, early_usage=False
    ):
        """Configure a native-call or mixed response and the actual reported billing order."""
        super().__init__(
            [content] if content else [], finish=finish, refusal=refusal, usage=PROVIDER_USAGE
        )
        self.calls = calls or []
        self.early_usage = early_usage

    async def __aiter__(self):
        """Fragment arguments, preserve native indexes/IDs, and report provider usage."""
        if self.early_usage:
            yield self.event({}, usage=self.usage)
        for index, call in enumerate(self.calls):
            yield self.event(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["function"]["name"], "arguments": ""},
                        }
                    ]
                }
            )
            arguments = call["function"]["arguments"]
            halfway = len(arguments) // 2
            for part in (arguments[:halfway], arguments[halfway:]):
                yield self.event(
                    {"tool_calls": [{"index": index, "function": {"arguments": part}}]}
                )
        for content in self.contents:
            yield self.event({"content": content})
        yield self.event({"refusal": self.refusal} if self.refusal else {}, self.finish)
        if not self.early_usage:
            yield self.event({}, usage=self.usage)
        self.completed = True
        yield b"data: [DONE]\n\n"


HELP_RESPONSE = {
    "response": "I can explain DocVault.",
    "suggestions": [],
    "citation_ids": [],
    "outcome": "answered",
    "response_kind": "assistant_help",
}


async def no_delta(text):
    """Native-call-only rounds must not publish provisional answer text."""
    pytest.fail(f"Unexpected answer delta: {text}")


def native_response(stream):
    """Return native SSE bytes through httpx without replacing any SDK parsing method."""
    return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})


def native_call(name, arguments, identifier="call-1"):
    """Serialize function arguments exactly as the native wire protocol requires."""
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def test_all_tools_have_strict_required_inputs_and_hide_server_scope():
    """Strict schemas require nullable fields explicitly and cannot modify injected selection."""
    tools = build_agent_tool_definitions()
    assert {tool["function"]["name"] for tool in tools} == set(TOOL_INPUT_MODELS)
    for tool in tools:
        function = tool["function"]
        schema = function["parameters"]
        assert function["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert not {"selected_version_ids", "workspace_id", "chat_id"} & set(schema["properties"])
        assert len(function["description"].split()) < 85
    model = TOOL_INPUT_MODELS["get_selected_document_overviews"]
    assert model.model_validate({"cursor": None}).model_dump() == {"cursor": None}
    with pytest.raises(ValidationError):
        model.model_validate({})


@pytest.mark.asyncio
async def test_native_calls_keep_ids_strict_parsed_arguments_using_chat_model():
    """Use SDK native parsing, retaining the complete assistant message and real accounting."""
    requested, usage = [], []
    calls = [
        native_call("get_selected_document_overviews", {"cursor": None}),
        native_call("get_document_processing_metrics", {"scope": "selected"}, "call-2"),
    ]

    def handler(request):
        """Return two independent native calls with provider usage extras."""
        requested.append(json.loads(request.content))
        return native_response(NativeToolStream(calls))

    async def record(value):
        """Capture charged planning calls independently of answer generation."""
        usage.append(value)

    llm = create_provider(handler, record, openrouter_chat_model="test/chat")
    try:
        selection = await llm.stream_chat_turn(
            [], build_agent_tool_definitions(), no_delta, evidence_ids=[]
        )
    finally:
        await llm.close()
    assert [call.id for call in selection.tool_calls] == ["call-1", "call-2"]
    assert selection.tool_calls[0].arguments == {"cursor": None}
    assert selection.assistant_message == {
        "role": "assistant",
        "content": None,
        "tool_calls": calls,
    }
    assert requested[0]["model"] == "test/chat"
    assert "reasoning" not in requested[0]
    assert requested[0]["tool_choice"] == "auto"
    assert requested[0]["provider"]["require_parameters"]
    assert requested[0]["response_format"]["json_schema"]["strict"]
    assert usage[0]["operation"] == "tool_selection"
    assert usage[0]["status"] == "succeeded" and usage[0]["cost_usd"] == 0.003
    assert usage[0]["input_tokens"] == 42 and usage[0]["output_tokens"] == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["low", "none", "xhigh", "max"])
async def test_native_tool_reasoning_is_preserved_and_echoed_through_sdk(effort):
    details = [
        {
            "type": "reasoning.encrypted",
            "data": "opaque",
            "id": "r1",
            "format": "openai-responses-v1",
            "index": 0,
        }
    ]

    class ReasoningToolStream(NativeToolStream):
        async def __aiter__(self):
            yield self.event({"reasoning": "First ", "reasoning_details": details})
            yield self.event(
                {
                    "reasoning": "second",
                    "reasoning_details": [{"index": 0, "data": "-continuation"}],
                }
            )
            async for chunk in super().__aiter__():
                yield chunk

    calls = [native_call("get_selected_document_overviews", {"cursor": None})]
    requested, deltas = [], []

    def handler(request):
        requested.append(json.loads(request.content))
        if len(requested) == 1:
            return native_response(ReasoningToolStream(calls))
        return native_response(ResponseStream([json.dumps(HELP_RESPONSE)], usage=PROVIDER_USAGE))

    llm = create_provider(handler, openrouter_chat_reasoning_effort=effort)
    try:
        turn = await llm.stream_chat_turn(
            [], build_agent_tool_definitions(), no_delta, evidence_ids=[]
        )
        expected = [{**details[0], "data": "opaque-continuation"}]
        assert turn.assistant_message["reasoning"] == "First second"
        assert turn.assistant_message["reasoning_details"] == expected

        async def delta(text):
            deltas.append(text)

        await llm.stream_chat_turn(
            [turn.assistant_message, {"role": "tool", "tool_call_id": "call-1", "content": "{}"}],
            build_agent_tool_definitions(),
            delta,
            evidence_ids=[],
        )
    finally:
        await llm.close()
    assert all(request["reasoning"] == {"effort": effort} for request in requested)
    assert requested[1]["messages"][0]["reasoning_details"] == expected
    assert requested[1]["messages"][0]["reasoning"] == "First second"
    assert "".join(deltas) == HELP_RESPONSE["response"]


@pytest.mark.asyncio
async def test_help_can_finish_in_one_native_stream_without_a_tool():
    """A direct structured help answer is returned by the same model without a planning pass."""
    stream = ResponseStream(list(json.dumps(HELP_RESPONSE)), usage=PROVIDER_USAGE)
    llm = create_provider(lambda request: native_response(stream))
    deltas = []

    async def delta(text):
        """Capture direct answer text from the one native stream."""
        deltas.append(text)

    try:
        turn = await llm.stream_chat_turn(
            [], build_agent_tool_definitions(), delta, evidence_ids=[]
        )
    finally:
        await llm.close()
    assert turn.tool_calls == [] and turn.answer is not None
    assert turn.answer.model_dump() == HELP_RESPONSE
    assert "".join(deltas) == HELP_RESPONSE["response"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "calls",
    [
        [native_call("unknown_tool", {})],
        [native_call("retrieve_relevant_chunks", {"query": "price", "workspace_id": "forged"})],
        [native_call("get_selected_document_overviews", {})],
        [native_call("get_document_processing_metrics", {"scope": "all"})],
        [native_call("get_selected_document_overviews", {"cursor": None}, "")],
        [
            native_call("get_selected_document_overviews", {"cursor": None}),
            native_call("get_document_processing_metrics", {"scope": "selected"}),
        ],
        [],
    ],
)
async def test_invalid_calls_fail_closed_and_preserve_provider_usage(calls):
    """Unknown names, forged fields, missing args and invalid IDs never become executable calls."""
    usage = []

    async def record(value):
        """Record charges even when SDK argument validation rejects a completion."""
        usage.append(value)

    llm = create_provider(
        lambda request: native_response(NativeToolStream(calls, early_usage=True)), record
    )
    try:
        with pytest.raises(ProviderError):
            await llm.stream_chat_turn(
                [], build_agent_tool_definitions(), no_delta, evidence_ids=[]
            )
    finally:
        await llm.close()
    assert usage[0]["status"] == "failed"
    assert usage[0]["operation"] == "tool_selection"
    assert usage[0]["cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_configured_tool_context_does_not_reject_or_truncate_native_input():
    """Send complete tools/input despite a small configured planning fallback."""
    requested = []

    def handler(request):
        """Accept native input that the actual provider can fit."""
        requested.append(json.loads(request.content))
        return native_response(
            NativeToolStream([native_call("get_selected_document_overviews", {"cursor": None})])
        )

    llm = create_provider(handler, openrouter_context_tokens=1000, openrouter_max_output_tokens=100)
    try:
        await llm.stream_chat_turn(
            [{"role": "user", "content": "context " * 1000}],
            build_agent_tool_definitions(),
            no_delta,
            evidence_ids=[],
        )
    finally:
        await llm.close()
    assert requested[0]["messages"][0]["content"] == "context " * 1000
    assert len(requested[0]["tools"]) == len(TOOL_INPUT_MODELS)


@pytest.mark.asyncio
async def test_final_agent_stream_keeps_chat_model_public_fields_and_real_deltas():
    """Classified answers reuse the chat model, streaming cleanup and strict structured output."""
    content = {
        "response": "I can summarize your documents.",
        "suggestions": ["Can you summarize my selected documents?", "What are the key takeaways?"],
        "citation_ids": [],
        "outcome": "answered",
        "response_kind": "assistant_help",
    }
    stream = ResponseStream(list(json.dumps(content)), usage=PROVIDER_USAGE)
    requested, deltas = [], []

    def handler(request):
        """Serve a real SSE wire stream containing the extended structured response."""
        requested.append(json.loads(request.content))
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async def delta(text):
        """Retain provisional user-facing deltas rather than the internal decision text."""
        deltas.append(text)

    llm = create_provider(handler, openrouter_chat_model="test/chat")
    try:
        turn = await llm.stream_chat_turn(
            [], build_agent_tool_definitions(), delta, evidence_ids=[]
        )
        assert turn.answer is not None
        answer = turn.answer
    finally:
        await llm.close()
    assert isinstance(answer, AgentChatGenerationLLMResponse)
    assert answer.model_dump() == content
    assert "".join(deltas) == content["response"] and len(deltas) > 1
    assert stream.closed
    assert requested[0]["model"] == "test/chat"
    assert requested[0]["response_format"]["json_schema"]["strict"]
    assert (
        requested[0]["response_format"]["json_schema"]["schema"]["properties"]["citation_ids"][
            "maxItems"
        ]
        == 0
    )


def test_agent_identity_records_tool_contracts_and_final_prompt_without_credentials():
    """Cached agent work changes identity when its tool/final generation contract changes."""
    settings = LLMSettings(_env_file=None, openrouter_api_key="not-for-cache")
    identity = build_generation_identity(settings.generation_model("chat"), "chat")
    assert len(identity["tool_definitions"]) == len(TOOL_INPUT_MODELS)
    assert "response_kind" in identity["schema"]["properties"]
    assert "not-for-cache" not in json.dumps(identity)
    with pytest.raises(ValidationError):
        LLMSettings(_env_file=None, agent_max_tool_rounds=0)


def test_all_native_rounds_use_one_chat_configuration():
    """Only one chat model config owns both tool choice and final response generation."""
    settings = LLMSettings(
        _env_file=None,
        openrouter_chat_model="test/chat",
        openrouter_context_tokens=10000,
        openrouter_max_output_tokens=512,
        openrouter_chat_temperature=0,
        agent_max_tool_rounds=4,
    )
    assert settings.generation_model("chat").model_dump() == {
        "model": "test/chat",
        "context_tokens": 10000,
        "max_output_tokens": 512,
        "temperature": 0,
        "reasoning_effort": None,
    }
    assert settings.agent_max_tool_rounds == 4
    assert "openrouter_agent_model" not in LLMSettings.model_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal,finish", [("Declined", "stop"), (None, "length"), (None, "content_filter")]
)
async def test_native_selection_refusals_and_incomplete_output_preserve_usage(refusal, finish):
    """Refused/truncated planning requests remain failed, with reported charges retained."""
    usage = []

    async def record(value):
        """Capture accounting before SDK response parsing can reject output."""
        usage.append(value)

    llm = create_provider(
        lambda request: native_response(
            NativeToolStream(refusal=refusal, finish=finish, early_usage=True)
        ),
        record,
    )
    try:
        with pytest.raises(ProviderError):
            await llm.stream_chat_turn(
                [], build_agent_tool_definitions(), no_delta, evidence_ids=[]
            )
    finally:
        await llm.close()
    assert usage[0]["status"] == "failed" and usage[0]["cost_usd"] == 0.003


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["tool_calls", "stop"])
async def test_mixed_tool_calls_and_structured_answer_are_rejected(finish):
    """A mixed native response cannot publish completion even if both pieces validate alone."""
    usage, deltas = [], []
    stream = NativeToolStream(
        [native_call("get_selected_document_overviews", {"cursor": None})],
        finish=finish,
        content=json.dumps(HELP_RESPONSE),
        early_usage=True,
    )

    async def record(value):
        """Keep actually reported charges on invalid mixed native turns."""
        usage.append(value)

    async def delta(text):
        """Consume provisional text without treating it as successful completion."""
        deltas.append(text)

    llm = create_provider(lambda request: native_response(stream), record)
    try:
        with pytest.raises(ProviderError):
            await llm.stream_chat_turn([], build_agent_tool_definitions(), delta, evidence_ids=[])
    finally:
        await llm.close()
    assert stream.closed
    assert usage[0]["status"] == "failed" and usage[0]["cost_usd"] == 0.003


@pytest.mark.asyncio
async def test_actual_evidence_whitelist_rejects_unknown_final_citation_in_combined_stream():
    """The native structured answer schema enforces current evidence IDs before success."""
    content = {
        **HELP_RESPONSE,
        "response": "Amount [forged-id]",
        "response_kind": "document_answer",
        "citation_ids": ["forged-id"],
    }
    stream = ResponseStream(list(json.dumps(content)), usage=PROVIDER_USAGE)
    llm = create_provider(lambda request: native_response(stream))

    async def delta(text):
        """Consume provisional text; final validation must still reject the forged reference."""
        pass

    try:
        with pytest.raises(ProviderError):
            await llm.stream_chat_turn(
                [], build_agent_tool_definitions(), delta, evidence_ids=["original-id"]
            )
    finally:
        await llm.close()
    assert stream.closed
