"""Token-triggered conversation memory; original messages and source boundaries remain persisted."""

import json

from pydantic import Field

from docvault.llm.models import ConversationSummaryLLMResponse, StrictModel
from docvault.llm.prompts import CONVERSATION_SUMMARY_SYSTEM_PROMPT
from docvault.llm.provider import OpenRouterLLM, token_count

CONVERSATION_SUMMARY_THRESHOLD = 0.85


class HistoryCompactionResult(StrictModel):
    """Recent complete turns plus optional memory and its exact persisted message checkpoint."""

    history: list[dict]
    summary: ConversationSummaryLLMResponse | None = None
    compacted_message_ids: list[str] = Field(default_factory=list)


def _historical_tool_references(traces: list) -> list[dict]:
    """Keep successful server-returned identifiers/cursors, without raw content or stale receipts."""
    references: list[dict] = []
    seen: set[str] = set()
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        completed = trace.get("execution_status") == "completed"
        legacy_completed = trace.get("execution_status") is None and trace.get("status") == "ok"
        if not completed and not legacy_completed:
            continue
        reference: dict = {"historical_reference": True}
        tool = trace.get("tool")
        if isinstance(tool, str) and tool:
            reference["tool"] = tool
        for key in ("artifact_id", "document_id", "version_id"):
            identifier = trace.get(key)
            if isinstance(identifier, str) and identifier:
                reference[key] = identifier
        versions = trace.get("version_ids")
        if isinstance(versions, list):
            reference["version_ids"] = [value for value in versions if isinstance(value, str)]
        cursor = trace.get("next_cursor")
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            reference["next_cursor"] = cursor
        documents = trace.get("document_references")
        if isinstance(documents, list):
            cards = []
            for item in documents:
                if not isinstance(item, dict):
                    continue
                card = {
                    key: item[key]
                    for key in (
                        "artifact_id",
                        "document_id",
                        "version_id",
                        "filename",
                        "title",
                        "category",
                    )
                    if isinstance(item.get(key), str) and item[key]
                }
                if card:
                    cards.append(card)
            if cards:
                reference["document_references"] = cards
        if not any(
            key in reference
            for key in (
                "artifact_id",
                "document_id",
                "version_id",
                "document_references",
                "next_cursor",
            )
        ):
            continue
        identity = json.dumps(reference, sort_keys=True, ensure_ascii=False)
        if identity not in seen:
            seen.add(identity)
            references.append(reference)
    return references


def build_conversation_summary_turn(
    summary: ConversationSummaryLLMResponse, compacted_history: list[dict]
) -> dict:
    """Render memory as untrusted conversation, retaining server-issued original source boundaries."""
    boundaries: list[dict] = []
    for item in compacted_history:
        versions = item.get("version_ids", [])
        if not boundaries or boundaries[-1]["version_ids"] != versions:
            boundaries.append({"version_ids": versions, "citation_ids": [], "tool_references": []})
        boundaries[-1]["citation_ids"] = list(
            dict.fromkeys(boundaries[-1]["citation_ids"] + item.get("citation_ids", []))
        )
        traces = item.get("agent_trace", [])
        if isinstance(traces, list):
            boundary_references = boundaries[-1]["tool_references"]
            for reference in _historical_tool_references(traces):
                if reference not in boundary_references:
                    boundary_references.append(reference)
    tool_references = [
        reference for boundary in boundaries for reference in boundary["tool_references"]
    ]
    return {
        "role": "assistant",
        "content": json.dumps(
            {
                "conversation_memory": summary.summary,
                "source_boundaries": boundaries,
                "notice": "Conversation memory is not document evidence. Retrieve original sources for factual claims.",
                "tool_reference_notice": "Historical identifiers only: recheck access and live state through current tools. These are not fresh operational receipts.",
            },
            ensure_ascii=False,
        ),
        "agent_trace": tool_references,
    }


def _recent_turn_start(history: list[dict]) -> int:
    """Keep the latest complete assistant reply with its user question and any newer turns."""
    assistant_positions = [
        index for index, item in enumerate(history) if item.get("role") == "assistant"
    ]
    target = assistant_positions[-1] if assistant_positions else len(history) - 1
    users = [
        index for index, item in enumerate(history[: target + 1]) if item.get("role") == "user"
    ]
    return users[-1] if users else target


async def _summarize_older_turns(
    llm: OpenRouterLLM,
    older: list[dict],
    prior_summary: ConversationSummaryLLMResponse | None,
) -> ConversationSummaryLLMResponse:
    """Summarize ordered complete turns in model-sized batches, retaining carry-forward memory."""
    groups: list[list[dict]] = []
    for item in older:
        if not groups or item.get("role") == "user":
            groups.append([])
        groups[-1].append(
            {
                "role": item.get("role"),
                "content": item.get("content", ""),
                "version_ids": item.get("version_ids", []),
            }
        )
    context = await llm.resolve_model_context_tokens("conversation_summary")
    reservation = llm.generation_model("conversation_summary").max_output_tokens
    reservation += token_count(CONVERSATION_SUMMARY_SYSTEM_PROMPT)
    reservation += token_count(json.dumps(ConversationSummaryLLMResponse.model_json_schema())) + 128
    summary = prior_summary
    while groups:
        batch = groups.pop(0)
        while groups:
            candidate = {
                "prior_memory": summary.model_dump() if summary else None,
                "older_turns": batch + groups[0],
            }
            if token_count(json.dumps(candidate, ensure_ascii=False)) + reservation >= context:
                break
            batch += groups.pop(0)
        # A single oversized turn remains intact: the actual provider decides acceptance.
        summary = await llm.generate_structured_response(
            ConversationSummaryLLMResponse,
            [
                {"role": "system", "content": CONVERSATION_SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "prior_memory": summary.model_dump() if summary else None,
                            "older_turns": batch,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            task="conversation_summary",
        )
    if summary is None:
        raise ValueError("Conversation summarization requires older turns.")
    return summary


async def compact_chat_history(
    history: list[dict],
    llm: OpenRouterLLM,
    *,
    question: str,
    selected_documents: list[dict],
    prior_summary: ConversationSummaryLLMResponse | None = None,
    compacted_message_ids: list[str] | None = None,
    fixed_context: dict | None = None,
) -> HistoryCompactionResult:
    """Summarize only older complete turns at 85% of the actual chat context capacity.

    Include fixed prompts/tool schemas/current question in the estimate and reserve output.
    Below the threshold retain all unsummarized turns, without a fixed message-count limit.
    Incremental memory consumes only newly older turns. Invalid retry checkpoints are ignored.
    Failure leaves persisted messages untouched; a huge recent turn is sent to the provider
    intact rather than being silently truncated or rejected by a local configured cap.
    """
    if not history:
        return HistoryCompactionResult(history=[])
    ids = [item.get("message_id") for item in history]
    known_ids = {identifier for identifier in ids if isinstance(identifier, str)}
    previous_ids = list(dict.fromkeys(compacted_message_ids or []))
    if prior_summary is None or not previous_ids or not set(previous_ids).issubset(known_ids):
        prior_summary, previous_ids = None, []
    previous = set(previous_ids)
    old = [item for item in history if item.get("message_id") in previous]
    remaining = [item for item in history if item.get("message_id") not in previous]
    estimate_history = [
        {
            "role": item.get("role"),
            "content": item.get("content", ""),
            "version_ids": item.get("version_ids", []),
            "citation_ids": item.get("citation_ids", []),
        }
        for item in remaining
    ]
    if prior_summary is not None:
        estimate_history.insert(0, build_conversation_summary_turn(prior_summary, old))
    payload = {
        "history": estimate_history,
        "question": question,
        "selected_documents": selected_documents,
        "fixed_context": fixed_context or {},
    }
    context_tokens = await llm.resolve_model_context_tokens("chat")
    output_tokens = llm.generation_model("chat").max_output_tokens
    estimated_tokens = token_count(json.dumps(payload, ensure_ascii=False)) + output_tokens
    if estimated_tokens < context_tokens * CONVERSATION_SUMMARY_THRESHOLD or len(remaining) < 2:
        return HistoryCompactionResult(
            history=remaining, summary=prior_summary, compacted_message_ids=previous_ids
        )
    cutoff = _recent_turn_start(remaining)
    older, recent = remaining[:cutoff], remaining[cutoff:]
    if not older:
        return HistoryCompactionResult(
            history=remaining, summary=prior_summary, compacted_message_ids=previous_ids
        )
    if any(not isinstance(item.get("message_id"), str) for item in older):
        # A summary without persistent message identities cannot be safely checkpointed.
        return HistoryCompactionResult(
            history=remaining, summary=prior_summary, compacted_message_ids=previous_ids
        )
    summary = await _summarize_older_turns(llm, older, prior_summary)
    new_ids = [item["message_id"] for item in older]
    return HistoryCompactionResult(
        history=recent,
        summary=summary,
        compacted_message_ids=list(dict.fromkeys(previous_ids + new_ids)),
    )
