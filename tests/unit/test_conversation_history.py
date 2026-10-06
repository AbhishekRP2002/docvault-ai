"""Conversation compaction preserves recent sources and summarizes only at actual model pressure."""

import json
import math
from copy import deepcopy

import pytest

from docvault.llm.config import GenerationModelConfig, LLMTask
from docvault.llm.history import build_conversation_summary_turn, compact_chat_history
from docvault.llm.models import ConversationSummaryLLMResponse
from docvault.llm.provider import OpenRouterLLM, ProviderError, Schema, token_count


class HistoryLLM(OpenRouterLLM):
    """Separate configured fallback and actual capacities without any live provider calls."""

    def __init__(self, capacity=10000, fail=False, output_tokens=100, summary_capacity=12000):
        """Track structured summaries and allow a deterministic charged-provider failure."""
        self.capacity = capacity
        self.fail = fail
        self.calls = []
        self.output_tokens = output_tokens
        self.summary_capacity = summary_capacity
        self.context_requests: list[LLMTask] = []
        self.configuration_requests: list[LLMTask] = []

    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Configured fallback is intentionally smaller than output; actual metadata governs memory."""
        self.configuration_requests.append(task)
        return GenerationModelConfig(
            model=f"fake/{task}",
            context_tokens=50,
            max_output_tokens=self.output_tokens if task == "chat" else 100,
        )

    async def resolve_model_context_tokens(self, task: LLMTask) -> int:
        """Return actual metadata capacity rather than configured local caps."""
        self.context_requests.append(task)
        return self.summary_capacity if task == "conversation_summary" else self.capacity

    async def generate_structured_response(
        self, schema: type[Schema], messages: list[dict], *, task: LLMTask
    ) -> Schema:
        """Return strict conversation memory while retaining exact input for scope assertions."""
        assert schema is ConversationSummaryLLMResponse and task == "conversation_summary"
        self.calls.append(messages)
        if self.fail:
            raise ProviderError("The LLM provider could not be reached.")
        return schema.model_validate(
            {"summary": "The user compared Acme and Atlas. Cancellation remains unresolved."}
        )


def conversation(turns=2, words=5):
    """Build paired chronological messages with distinct original citations and source changes."""
    result = []
    for index in range(turns):
        for role in ("user", "assistant"):
            result.append(
                {
                    "message_id": f"{index}-{role}",
                    "role": role,
                    "content": f"{role} says " + "context " * words,
                    "version_ids": [f"version-{index % 2}"],
                    "citation_ids": [f"source-{index}"] if role == "assistant" else [],
                    "agent_trace": [{"unrelated_duplicate_evidence": "ignored"}],
                }
            )
    return result


def projected_tokens(history, fixed_context=None):
    """Calculate the same payload estimate, without persisted trace duplication."""
    return (
        token_count(
            json.dumps(
                {
                    "history": [
                        {
                            key: item[key]
                            for key in ("role", "content", "version_ids", "citation_ids")
                        }
                        for item in history
                    ],
                    "question": "What does it mean?",
                    "selected_documents": [],
                    "fixed_context": fixed_context or {},
                },
                ensure_ascii=False,
            )
        )
        + 100
    )


@pytest.mark.asyncio
async def test_below_threshold_keeps_more_than_twenty_turns_without_a_summary_call():
    """A fixed message-count window no longer discards older conversation."""
    history, llm = conversation(15), HistoryLLM(capacity=20000)
    result = await compact_chat_history(
        history, llm, question="What does it mean?", selected_documents=[]
    )
    assert result.history == history and len(result.history) == 30
    assert result.summary is None and result.compacted_message_ids == [] and llm.calls == []
    assert llm.context_requests == ["chat"]
    assert llm.configuration_requests == ["chat"]


@pytest.mark.asyncio
async def test_summary_model_capacity_does_not_trigger_chat_memory_pressure():
    """Only the current chat model's actual context determines whether older turns need memory."""
    history, llm = conversation(), HistoryLLM(capacity=20000, summary_capacity=1)
    result = await compact_chat_history(
        history, llm, question="What does it mean?", selected_documents=[]
    )
    assert result.history == history and result.summary is None and llm.calls == []
    assert llm.context_requests == ["chat"]


@pytest.mark.asyncio
async def test_chat_output_reservation_alone_can_trigger_conversation_summary():
    """A larger configured chat reply reduces input headroom independently of the memory model."""
    history = conversation()
    llm = HistoryLLM(capacity=2000, output_tokens=2000)
    result = await compact_chat_history(
        history, llm, question="What does it mean?", selected_documents=[]
    )
    assert result.history == history[-2:] and result.summary is not None
    assert llm.context_requests == ["chat", "conversation_summary"]
    assert llm.configuration_requests == ["chat", "conversation_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("crosses_threshold", [False, True])
async def test_eighty_five_percent_threshold_uses_actual_metadata_and_reserves_output(
    crosses_threshold,
):
    """Memory triggers at the threshold, never from a reduced configured context fallback."""
    history = conversation()
    quotient = projected_tokens(history) / 0.85
    llm = HistoryLLM(
        capacity=math.floor(quotient) if crosses_threshold else math.ceil(quotient) + 1
    )
    result = await compact_chat_history(
        history, llm, question="What does it mean?", selected_documents=[]
    )
    assert bool(llm.calls) == crosses_threshold
    if crosses_threshold:
        assert result.history == history[-2:]
        assert result.compacted_message_ids == [item["message_id"] for item in history[:-2]]
    else:
        assert result.history == history


@pytest.mark.asyncio
async def test_fixed_tool_prompt_and_schema_overhead_can_trigger_compaction():
    """Do not assess history independently of the actual prompts and tool contracts."""
    history, llm = conversation(), HistoryLLM(capacity=2000)
    result = await compact_chat_history(
        history,
        llm,
        question="What does it mean?",
        selected_documents=[],
        fixed_context={"tool_catalog": "metadata " * 2000},
    )
    assert len(llm.calls) == 1 and result.history == history[-2:]


@pytest.mark.asyncio
async def test_checkpoint_incrementally_summarizes_new_older_turns_and_preserves_original_boundaries():
    """Checkpoint IDs exclude already summarized turns without inventing source/version references."""
    history, llm = conversation(3, 400), HistoryLLM(capacity=1000)
    checkpoint = ConversationSummaryLLMResponse(summary="Earlier user compared Acme and Atlas.")
    previous_ids = [item["message_id"] for item in history[:2]]
    result = await compact_chat_history(
        history,
        llm,
        question="What does it mean?",
        selected_documents=[],
        prior_summary=checkpoint,
        compacted_message_ids=previous_ids,
    )
    payload = json.loads(llm.calls[0][-1]["content"])
    assert payload["prior_memory"] == checkpoint.model_dump()
    assert [item["content"] for item in payload["older_turns"]] == [
        item["content"] for item in history[2:4]
    ]
    assert result.history == history[-2:]
    assert result.compacted_message_ids == [item["message_id"] for item in history[:-2]]
    assert result.summary is not None
    memory = json.loads(build_conversation_summary_turn(result.summary, history[:-2])["content"])
    assert memory["source_boundaries"] == [
        {"version_ids": ["version-0"], "citation_ids": ["source-0"], "tool_references": []},
        {"version_ids": ["version-1"], "citation_ids": ["source-1"], "tool_references": []},
    ]
    assert "not document evidence" in memory["notice"]
    assert "unverified" in llm.calls[0][0]["content"] or "not present" in llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_retry_ignores_checkpoint_containing_future_message_ids():
    """A checkpoint from later conversation cannot erase earlier retry input."""
    history, llm = conversation(), HistoryLLM(capacity=100000)
    result = await compact_chat_history(
        history,
        llm,
        question="What does it mean?",
        selected_documents=[],
        prior_summary=ConversationSummaryLLMResponse(summary="Future conversation"),
        compacted_message_ids=["not-yet-visible"],
    )
    assert (
        result.history == history and result.summary is None and result.compacted_message_ids == []
    )


@pytest.mark.asyncio
async def test_huge_recent_reply_is_kept_intact_even_if_it_alone_exceeds_threshold():
    """An application history policy cannot truncate the newest source-backed reply."""
    history, llm = conversation(1, 3000), HistoryLLM(capacity=1000)
    result = await compact_chat_history(
        history, llm, question="What does it mean?", selected_documents=[]
    )
    assert result.history == history and llm.calls == []


@pytest.mark.asyncio
async def test_summary_failure_preserves_original_history_and_checkpoint_inputs():
    """Failed memory generation cannot mutate persisted turns or an existing checkpoint."""
    history, llm = conversation(3, 500), HistoryLLM(capacity=1000, fail=True)
    before = deepcopy(history)
    checkpoint = ConversationSummaryLLMResponse(summary="Original summary")
    ids = [item["message_id"] for item in history[:2]]
    with pytest.raises(ProviderError):
        await compact_chat_history(
            history,
            llm,
            question="What does it mean?",
            selected_documents=[],
            prior_summary=checkpoint,
            compacted_message_ids=ids,
        )
    assert history == before and checkpoint.summary == "Original summary"
    assert ids == [item["message_id"] for item in history[:2]]


def test_summary_memory_preserves_validated_catalog_and_artifact_referents_without_stale_receipts():
    """LLM prose cannot replace server-issued referents, and tool result bodies/counts stay out."""
    history = conversation(1)
    successful = {
        "tool": "search_documents",
        "execution_status": "completed",
        "status": "ok",
        "document_references": [
            {
                "document_id": "document-first",
                "version_id": "version-first",
                "filename": "first.pdf",
                "status": "ready",
                "raw_body": "not memory",
            },
            {
                "document_id": "document-second",
                "version_id": "version-second",
                "filename": "second.pdf",
            },
        ],
        "next_cursor": 2,
        "counts": {"ready": 4},
        "observed_at": "old timestamp",
        "arguments": {"artifact_id": "model-forged-input"},
    }
    artifact = {
        "tool": "get_document_analysis_result",
        "execution_status": "completed",
        "status": "ok",
        "artifact_id": "server-artifact",
        "artifact_status": "pending",
        "preview": "omit full preview",
    }
    failed = {
        "tool": "get_document_analysis_result",
        "execution_status": "failed",
        "status": "error",
        "artifact_id": "forged-rejected-artifact",
    }
    history[-1]["agent_trace"] = [successful, successful, artifact, failed]
    summary = ConversationSummaryLLMResponse(
        summary="A previous answer mentioned forged-artifact and unverified amounts."
    )
    turn = build_conversation_summary_turn(summary, history)
    memory = json.loads(turn["content"])
    references = memory["source_boundaries"][0]["tool_references"]
    assert references == turn["agent_trace"]
    assert len(references) == 2
    assert references[0]["next_cursor"] == 2
    assert [card["document_id"] for card in references[0]["document_references"]] == [
        "document-first",
        "document-second",
    ]
    assert references[1]["artifact_id"] == "server-artifact"
    assert all(reference["historical_reference"] is True for reference in references)
    assert "not fresh operational receipts" in memory["tool_reference_notice"]
    serialized = json.dumps(references)
    assert not any(
        value in serialized
        for value in (
            "counts",
            "old timestamp",
            "not memory",
            "model-forged-input",
            "forged-artifact",
            "forged-rejected-artifact",
            "omit full preview",
        )
    )
