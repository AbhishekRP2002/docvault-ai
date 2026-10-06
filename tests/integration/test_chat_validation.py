"""Check nullable chat boundaries against isolated real database schemas."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support import require_persisted_row
from test_api import api as api
from test_api import create_chat, ready_source
from test_api import test_database_url as test_database_url

from docvault.chat import (
    generate_assistant_response,
    require_message,
    reserve_assistant_response,
    stream_message_events,
)
from docvault.db import session
from docvault.errors import AppError
from docvault.models import Chat, Message

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("content", [None, "", " \n\t "])
def test_empty_question_is_rejected_without_persisting_a_turn(api, content):
    chat_id = create_chat(api, [ready_source()])
    with pytest.raises(AppError) as failure:
        reserve_assistant_response(chat_id, content, str(uuid4()))
    assert (failure.value.status, failure.value.code) == (422, "invalid_message")
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        assert require_persisted_row(db, Chat, chat_id).title == "New chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_kind", ["null", "missing", "wrong_chat", "wrong_role"])
async def test_invalid_parent_fails_before_provider_work_and_persists_failure(api, parent_kind):
    source = ready_source()
    chat_id = create_chat(api, [source])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, db.begin():
        message = require_persisted_row(db, Message, message_id)
        if parent_kind == "null":
            message.parent_id = None
        elif parent_kind == "missing":
            message.parent_id = str(uuid4())
        else:
            parent_chat = create_chat(api, [source]) if parent_kind == "wrong_chat" else chat_id
            parent = Message(
                chat_id=parent_chat,
                role="user" if parent_kind == "wrong_chat" else "assistant",
                content="Earlier message",
            )
            db.add(parent)
            db.flush()
            message.parent_id = parent.id
    events = []

    async def record_event(name, payload):
        events.append((name, payload))

    await generate_assistant_response(message_id, record_event)
    assert api.llm.calls == []
    with session() as db:
        message = require_persisted_row(db, Message, message_id)
        assert message.status == "failed" and message.error
        assert message.content == ""
    assert len(events) == 1
    assert events[0][0] == "message.failed"
    assert events[0][1]["message"]["status"] == "failed"


@pytest.mark.asyncio
async def test_legacy_attempt_without_request_key_generates_a_persisted_reply(api):
    chat_id = create_chat(api, [ready_source()])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, db.begin():
        require_persisted_row(db, Message, message_id).request_key = None
    events = []

    async def record_event(name, payload):
        events.append((name, payload))

    await generate_assistant_response(message_id, record_event)
    with session() as db:
        message = require_persisted_row(db, Message, message_id)
        assert message.status == "complete"
        assert "30 days" in message.content
        assert message.citations
    assert events[-1][0] == "answer.completed"
    assert len(api.llm.calls) == 1


def test_retry_without_an_original_user_turn_rejects_without_another_attempt(api):
    chat_id = create_chat(api, [ready_source()])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, db.begin():
        message = require_persisted_row(db, Message, message_id)
        assert message.parent_id is not None
        parent = require_persisted_row(db, Message, message.parent_id)
        message.status = "failed"
        db.delete(parent)
    with pytest.raises(AppError) as failure:
        reserve_assistant_response(chat_id, None, str(uuid4()), retry_of=message_id)
    assert (failure.value.status, failure.value.code) == (409, "retry_latest_turn")
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1


@pytest.mark.asyncio
async def test_missing_message_is_rejected_by_lookup_and_sse_before_start_event(api):
    missing_id = str(uuid4())
    with session() as db, pytest.raises(AppError) as lookup:
        require_message(db, missing_id)
    assert lookup.value.status == 404
    stream = stream_message_events(missing_id)
    with pytest.raises(AppError) as failure:
        await anext(stream)
    assert (failure.value.status, failure.value.code) == (404, "message_not_found")


def test_message_lookup_rejects_another_chat_scope(api):
    source = ready_source()
    chat_id = create_chat(api, [source])
    other_id = create_chat(api, [source])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, pytest.raises(AppError) as failure:
        require_message(db, message_id, other_id)
    assert failure.value.status == 404


def install_agent_script(api, monkeypatch, rounds, final):
    """Override native model decisions while preserving real tools, database writes and response schemas."""
    import json

    from test_api import read_native_chat_payload

    from docvault.tools.models import AgentChatGenerationLLMResponse, ChatAgentTurn

    decisions = iter(rounds)

    async def chat_turn(messages, tools, on_delta, *, evidence_ids):
        """One active chat method decides between native calls and a strict final reply."""
        calls = next(decisions)
        assistant = {"role": "assistant", "content": None}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": item.id,
                    "type": "function",
                    "function": {"name": item.name, "arguments": json.dumps(item.arguments)},
                }
                for item in calls
            ]
            return ChatAgentTurn(assistant_message=assistant, tool_calls=calls)
        payload = read_native_chat_payload(messages)
        api.llm.calls.append(payload)
        result = AgentChatGenerationLLMResponse.model_validate(
            final(payload) if callable(final) else final
        )
        await on_delta(result.response)
        return ChatAgentTurn(
            assistant_message={"role": "assistant", "content": result.model_dump_json()},
            tool_calls=[],
            answer=result,
        )

    monkeypatch.setattr(api.llm, "stream_chat_turn", chat_turn)


def test_help_completes_without_retrieval_or_fake_source_citations(api, monkeypatch):
    from test_api import ask

    from docvault.tools import runtime as document_tools

    async def reject_retrieval(*args, **kwargs):
        """Fail if a help turn unnecessarily invokes passage retrieval."""
        pytest.fail("Help must not run document retrieval")

    monkeypatch.setattr(document_tools, "retrieve_relevant_chunks", reject_retrieval)
    install_agent_script(
        api,
        monkeypatch,
        [[]],
        {
            "response": "I can summarize selected documents and find cited facts.",
            "suggestions": [],
            "citation_ids": [],
            "outcome": "answered",
            "response_kind": "assistant_help",
        },
    )
    chat_id = create_chat(api, [ready_source()])
    result = ask(api, chat_id, "How can you help me?").json()
    assert result["status"] == "complete" and result["citations"] == []
    assert "response_kind" not in result and result["agent_trace"] == []
    with session() as db:
        assert require_persisted_row(db, Message, result["id"]).agent_trace == []


@pytest.mark.parametrize(
    "question", ["Summarise this PDF", "What are the key takeaways?", "What is in this doc?"]
)
def test_generic_summary_uses_original_cited_overview_and_persists_compact_receipt(
    api, monkeypatch, question
):
    from test_api import ask

    from docvault.models import Version
    from docvault.tools import runtime as document_tools
    from docvault.tools.models import AgentToolCall

    source = ready_source()
    with session() as db, db.begin():
        version = require_persisted_row(db, Version, source.version)
        version.insight_status = "ready"
        version.insights = {
            "summary": "Payment is due in 30 days.",
            "category": "Contract",
            "tags": ["payment"],
            "key_insights": [
                {"text": "Payment is due in 30 days.", "citation_ids": [source.chunk]}
            ],
            "citation_ids": [source.chunk],
            "coverage": {"complete": True},
        }

    async def reject_retrieval(*args, **kwargs):
        """Fail if a generic overview request is incorrectly sent through similarity search."""
        pytest.fail("Generic summaries must not need a matching similarity score")

    monkeypatch.setattr(document_tools, "retrieve_relevant_chunks", reject_retrieval)
    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="overview-call",
                    name="get_selected_document_overviews",
                    arguments={"cursor": None},
                )
            ],
            [],
        ],
        {
            "response": f"Payment is due in 30 days. [{source.chunk}]",
            "suggestions": [],
            "citation_ids": [source.chunk],
            "outcome": "answered",
            "response_kind": "document_answer",
        },
    )
    result = ask(api, create_chat(api, [source]), question).json()
    assert result["status"] == "complete" and result["citations"][0]["chunk_id"] == source.chunk
    with session() as db:
        trace = require_persisted_row(db, Message, result["id"]).agent_trace
        assert len(trace) == 1 and trace[0]["tool"] == "get_selected_document_overviews"
        assert trace[0]["evidence_ids"] == [source.chunk]
        assert trace[0]["document_references"][0]["version_id"] == source.version
        assert "summary" not in trace[0]["document_references"][0]
    # A fresh history read reconstructs the result from PostgreSQL, not an in-memory graph.
    assert api.client.get(f"/v1/chats/{result['chat_id']}/messages/{result['id']}").json() == result


def test_ready_source_with_failed_insights_falls_back_to_bounded_overview_samples(api, monkeypatch):
    from test_api import ask

    from docvault.models import Version
    from docvault.tools.models import AgentToolCall

    source = ready_source()
    with session() as db, db.begin():
        require_persisted_row(db, Version, source.version).insight_status = "failed"
    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="overview",
                    name="get_selected_document_overviews",
                    arguments={"cursor": None},
                )
            ],
            [],
        ],
        {
            "response": f"Payment is due in 30 days. [{source.chunk}]",
            "suggestions": [],
            "citation_ids": [source.chunk],
            "outcome": "answered",
            "response_kind": "document_answer",
        },
    )
    result = ask(api, create_chat(api, [source]), "Summarise this PDF").json()
    assert result["status"] == "complete" and result["citations"][0]["chunk_id"] == source.chunk
    with session() as db:
        trace = require_persisted_row(db, Message, result["id"]).agent_trace
        assert [item["tool"] for item in trace] == ["get_selected_document_overviews"]
    assert api.llm.calls[0]["observations"][0]["content"]["items"][0]["status"] == "failed"


def test_forged_unselected_version_returns_safe_error_without_reading_other_source(
    api, monkeypatch
):
    from test_api import ask

    from docvault.tools.models import AgentToolCall

    source, other = ready_source(), ready_source("private.txt", "Unselected confidential clause.")
    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="forged",
                    name="get_document_processing_diagnostics",
                    arguments={"version_id": other.version},
                )
            ],
            [],
        ],
        {
            "response": "Please select that document before I summarize it.",
            "suggestions": [],
            "citation_ids": [],
            "outcome": "clarification_needed",
            "response_kind": "clarification",
        },
    )
    result = ask(api, create_chat(api, [source]), "Read private.txt").json()
    assert result["status"] == "complete" and result["citations"] == []
    assert api.llm.calls[0]["evidence"] == []
    with session() as db:
        trace = require_persisted_row(db, Message, result["id"]).agent_trace
        assert trace[0]["status"] == "error" and trace[0]["error_code"] == "source_out_of_scope"


def test_grounded_document_answer_cache_reuses_only_matching_sources_and_records_provenance(api):
    from test_api import ask

    source = ready_source()
    first = ask(api, create_chat(api, [source])).json()
    second = ask(api, create_chat(api, [source])).json()
    assert first["status"] == second["status"] == "complete"
    assert first["citations"] == second["citations"] and len(api.llm.calls) == 1
    with session() as db:
        trace = require_persisted_row(db, Message, second["id"]).agent_trace
        assert trace[0]["answer_cache_reused"] is True
        assert trace[0]["evidence_ids"] == [source.chunk]


def test_processing_counts_are_fresh_and_never_reused_from_completed_answer_cache(api, monkeypatch):
    from test_api import ask

    from docvault.tools.models import AgentToolCall

    source = ready_source()

    def metrics_call(identifier):
        """Create a native current-workspace count request with a unique call ID."""
        return AgentToolCall(
            id=identifier,
            name="get_document_processing_metrics",
            arguments={"scope": "workspace"},
        )

    def final(payload):
        """Read the actual persisted document count from this invocation's fresh tool receipt."""
        count = payload["observations"][0]["content"]["counts"]["total"]
        return {
            "response": f"There are {count} documents.",
            "suggestions": [],
            "citation_ids": [],
            "outcome": "answered",
            "response_kind": "workspace_answer",
        }

    install_agent_script(
        api, monkeypatch, [[metrics_call("first")], [], [metrics_call("second")], []], final
    )
    first = ask(api, create_chat(api, [source]), "How many documents are in the workspace?").json()
    ready_source("new.txt")
    second = ask(api, create_chat(api, [source]), "How many documents are in the workspace?").json()
    assert (
        first["content"] == "There are 1 documents."
        and second["content"] == "There are 2 documents."
    )
    assert first["status"] == second["status"] == "complete" and len(api.llm.calls) == 2
    with session() as db:
        assert (
            "answer_cache_reused"
            not in require_persisted_row(db, Message, second["id"]).agent_trace[0]
        )


def test_formatting_followup_reloads_original_citations_and_preserves_no_tool_flow(
    api, monkeypatch
):
    """Reformat a prior answer using original rows, even when its stored prose is inaccurate."""
    from test_api import ask

    from docvault.tools.models import AgentToolCall

    source = ready_source()
    chat_id = create_chat(api, [source])

    def final(payload):
        """Return the original source's term rather than trusting the previous answer's prose."""
        assert payload["evidence"][0]["text"] == source.text
        return {
            "response": f"| Payment term | 30 days | [{source.chunk}]",
            "suggestions": [],
            "citation_ids": [source.chunk],
            "outcome": "answered",
            "response_kind": "document_answer",
        }

    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="first", name="retrieve_relevant_chunks", arguments={"query": "Payment term"}
                )
            ],
            [],
            [],
        ],
        final,
    )
    first = ask(api, chat_id, "What is the payment term?").json()
    assert first["status"] == "complete"
    with session() as db, db.begin():
        require_persisted_row(
            db, Message, first["id"]
        ).content = "Payment is in 99 days (incorrect prior prose)."
    second = ask(api, chat_id, "Put that term into a compact table").json()
    assert second["status"] == "complete" and "30 days" in second["content"]
    assert "99 days" not in second["content"] and len(api.llm.calls) == 2
    with session() as db:
        trace = require_persisted_row(db, Message, second["id"]).agent_trace
        assert len(trace) == 1 and trace[0]["tool"] == "reuse_previous_citation_evidence"
        assert trace[0]["source_message_id"] == first["id"]
        assert trace[0]["evidence_ids"] == [source.chunk]
        assert trace[0]["server_evidence_reuse"] is True


def test_selection_change_prevents_old_citation_reuse_for_formatting(api, monkeypatch):
    """A no-tool response cannot use old source IDs after the selected document changes."""
    from test_api import ask

    from docvault.tools.models import AgentToolCall

    source, replacement = ready_source(), ready_source("new.txt", "Payment is due in 90 days.")
    chat_id = create_chat(api, [source])
    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="first", name="retrieve_relevant_chunks", arguments={"query": "Payment term"}
                )
            ],
            [],
            [],
        ],
        {
            "response": f"Payment is due in 30 days. [{source.chunk}]",
            "suggestions": [],
            "citation_ids": [source.chunk],
            "outcome": "answered",
            "response_kind": "document_answer",
        },
    )
    first = ask(api, chat_id).json()
    assert first["status"] == "complete"
    assert (
        api.client.patch(
            f"/v1/chats/{chat_id}", json={"version_ids": [replacement.version]}
        ).status_code
        == 200
    )
    second = ask(api, chat_id, "Put that term into a table").json()
    assert second["status"] == "failed" and second["citations"] == []
    assert api.llm.calls[-1]["evidence"] == []
    with session() as db:
        assert require_persisted_row(db, Message, second["id"]).agent_trace == []


def test_sse_tool_progress_is_live_upserted_and_persisted_without_source_text(api):
    """The API exposes one evolving compact step per native call before final completion."""
    import json

    from test_api import ask

    source = ready_source()
    chat_id = create_chat(api, [source])
    response = ask(api, chat_id, accept="text/event-stream")
    assert response.status_code == 200
    events = []
    for frame in response.text.split("\n\n"):
        lines = frame.splitlines()
        if lines and lines[0].startswith("event: "):
            events.append(
                (
                    lines[0][7:],
                    json.loads("\n".join(line[6:] for line in lines if line.startswith("data: "))),
                )
            )
    updates = [payload["trace"] for name, payload in events if name == "tool.updated"]
    assert [trace["execution_status"] for trace in updates] == ["pending", "running", "completed"]
    assert len({trace["tool_call_id"] for trace in updates}) == 1
    assert all("text" not in trace and "passages" not in trace for trace in updates)
    assert events[-1][0] == "answer.completed"
    final = events[-1][1]["message"]
    assert final["agent_trace"] == [updates[-1]]
    assert final["agent_trace"][0]["evidence_ids"] == [source.chunk]
    assert api.client.get(f"/v1/chats/{chat_id}/messages/{final['id']}").json() == final


def test_empty_retrieval_recovers_through_native_overview_without_relaxing_source_scope(
    api, monkeypatch
):
    """A factual turn can use bounded cited overview support when strict passage search is empty."""
    from test_api import ask

    from docvault.tools import runtime as document_tools
    from docvault.tools.models import AgentToolCall

    source = ready_source()

    async def empty_retrieval(*args, **kwargs):
        """Reproduce a valid source whose query has no admitted semantic/lexical matches."""
        return []

    monkeypatch.setattr(document_tools, "retrieve_relevant_chunks", empty_retrieval)

    def model_answer(payload):
        """Model first abstains on actual empty receipts, then cites the overview's original support."""
        supported = bool(payload["evidence"])
        return {
            "response": f"Payment is due in 30 days. [{source.chunk}]"
            if supported
            else "No matching evidence yet.",
            "suggestions": [],
            "citation_ids": [source.chunk] if supported else [],
            "outcome": "answered" if supported else "insufficient_evidence",
            "response_kind": "document_answer",
        }

    install_agent_script(
        api,
        monkeypatch,
        [
            [
                AgentToolCall(
                    id="retrieve",
                    name="retrieve_relevant_chunks",
                    arguments={"query": "Payment term"},
                )
            ],
            [],
            [
                AgentToolCall(
                    id="overview",
                    name="get_selected_document_overviews",
                    arguments={"cursor": None},
                )
            ],
            [],
        ],
        model_answer,
    )
    result = ask(api, create_chat(api, [source]), "What is the payment term?").json()
    assert result["status"] == "complete" and result["citations"][0]["chunk_id"] == source.chunk
    assert api.llm.calls[-1]["observations"][0]["content"]["passages"] == []
    assert [trace["tool"] for trace in result["agent_trace"]] == [
        "retrieve_relevant_chunks",
        "get_selected_document_overviews",
    ]
    assert result["agent_trace"][0]["evidence_ids"] == []
    assert result["agent_trace"][1]["evidence_ids"] == [source.chunk]
