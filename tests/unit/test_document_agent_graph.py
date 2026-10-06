"""Deterministic conditional tool-loop contracts; no database or provider calls."""

import asyncio
import json
from typing import TYPE_CHECKING, cast

import pytest

from docvault.llm.graphs import (
    InvalidCitationError,
    run_document_agent_workflow,
    validate_document_agent_answer,
)
from docvault.llm.models import Evidence
from docvault.llm.provider import OpenRouterLLM
from docvault.tools.models import (
    AgentChatGenerationLLMResponse,
    AgentToolCall,
    ChatAgentTurn,
    ToolExecutionResult,
)

if TYPE_CHECKING:
    from docvault.tools.runtime import DocumentToolRuntime


def source(identifier="source-1", version_id="version-1"):
    """Build an original server-issued passage within the default selected scope."""
    return Evidence(
        id=identifier,
        document_id="document-1",
        version_id=version_id,
        filename="terms.txt",
        version_number=1,
        text="Payment is due within 30 days.",
        location={"page": 1},
    )


def answer(response="Payment is due within 30 days. [source-1]", **overrides):
    """Validate a deterministic final answer using the production internal schema."""
    return AgentChatGenerationLLMResponse.model_validate(
        {
            "response": response,
            "suggestions": [],
            "citation_ids": ["source-1"],
            "outcome": "answered",
            "response_kind": "document_answer",
            **overrides,
        }
    )


def call(name, arguments=None, identifier="call-1"):
    """Validate a native tool-call record without relaxing its required call identity."""
    return AgentToolCall(id=identifier, name=name, arguments=arguments or {})


class ScriptedAgent(OpenRouterLLM):
    """Return scripted native decisions and schema-valid final results without an SDK client."""

    def __init__(self, rounds, final=None):
        """Capture each complete transcript and the final-generation payload."""
        self.rounds = iter(rounds)
        self.final = final or answer()
        self.transcripts = []
        self.final_payload = None
        self.generations = 0
        self.trace_updates = []

    async def stream_chat_turn(self, messages, tools, on_delta, *, evidence_ids):
        """One scripted native call returns either tools or a final structured reply."""
        self.transcripts.append(messages)
        requested = next(self.rounds) if tools else []
        assistant = {"role": "assistant", "content": None}
        if requested:
            assistant["tool_calls"] = [
                {
                    "id": item.id,
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": json.dumps(item.arguments),
                    },
                }
                for item in requested
            ]
            return ChatAgentTurn(assistant_message=assistant, tool_calls=requested)
        return await self._answer_turn(messages, on_delta, evidence_ids)

    async def _answer_turn(self, messages, on_delta, evidence_ids):
        """Emit only the final model response and retain its real native input context."""
        self.generations += 1
        self.final_payload = read_native_chat_payload(messages)
        await on_delta(self.final.response)
        return ChatAgentTurn(
            assistant_message={"role": "assistant", "content": self.final.model_dump_json()},
            tool_calls=[],
            answer=self.final,
        )


def read_native_chat_payload(messages):
    """Project actual native receipts for deterministic assertions, without a second generation request."""
    payload = json.loads(next(item["content"] for item in messages if item["role"] == "user"))
    payload["observations"] = []
    evidence = {item["id"]: item for item in payload.get("evidence", [])}
    names = {
        call["id"]: call["function"]["name"]
        for message in messages
        for call in message.get("tool_calls", [])
    }
    for message in messages:
        if message["role"] == "tool":
            content = json.loads(message["content"])
            payload["observations"].append(
                {"tool": names[message["tool_call_id"]], "content": content}
            )
            evidence.update({item["id"]: item for item in content.get("passages", [])})
        elif message["role"] == "user" and message is not messages[1]:
            payload.update(json.loads(message["content"]))
    payload["evidence"] = list(evidence.values())
    return payload


class ScriptedRuntime:
    """Test-only scoped runtime with explicit tool-result and concurrency observations."""

    def __init__(self, results):
        """Associate result records with tool names and retain captured selected IDs."""
        self.version_ids = ["version-1"]
        self.results = results
        self.calls = []
        self.running = self.max_running = 0
        self.delay: float = 0.0
        self.previous_reads = []
        self.previous_sources = [source()]

    def metadata(self):
        """Return identities without loading source text into greetings and capability queries."""
        return [{"version_id": "version-1", "document_id": "document-1", "filename": "terms.txt"}]

    async def read_previous_citation_evidence(self, citation_ids, version_ids):
        """Reload only server-issued original IDs whose captured snapshot still matches."""
        self.previous_reads.append((citation_ids, version_ids))
        if set(version_ids) != set(self.version_ids):
            return []
        found = [item for item in self.previous_sources if item.id in citation_ids]
        if {item.id for item in found} != set(citation_ids):
            raise ValueError("Unavailable previous source references")
        return found

    async def execute(self, name, arguments):
        """Return configured fresh receipts and measure active independent reads."""
        self.calls.append((name, arguments))
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(self.delay)
            result = self.results[name]
            if result.evidence:
                return result.model_copy(
                    update={
                        "content": {
                            **result.content,
                            "passages": [item.model_dump() for item in result.evidence],
                        }
                    }
                )
            return result
        finally:
            self.running -= 1


async def run(llm, runtime, question="When is payment due?", history=None, **kwargs):
    """Collect provisional deltas and persisted traces at the graph's caller boundaries."""
    deltas, traces = [], []

    async def on_delta(value):
        """Capture each provisional text delta without treating it as persisted success."""
        deltas.append(value)

    async def on_trace(value):
        """Capture each compact receipt for assertions about persistence ordering."""
        llm.trace_updates.append(value)
        if value.get("execution_status") in {"completed", "failed"} or value.get("status") in {
            "ok",
            "error",
        }:
            traces.append(value)

    result = await run_document_agent_workflow(
        question,
        history or [],
        cast("DocumentToolRuntime", runtime),
        llm,
        on_delta,
        on_trace,
        **kwargs,
    )
    return result, deltas, traces


@pytest.mark.asyncio
async def test_help_uses_zero_tools_and_has_no_fabricated_citations():
    llm = ScriptedAgent(
        [[]],
        answer(
            "I can summarize selected documents and find cited facts.",
            response_kind="assistant_help",
            citation_ids=[],
        ),
    )
    runtime = ScriptedRuntime({})
    result, deltas, traces = await run(llm, runtime, "How can you help me?")
    assert runtime.calls == [] and traces == [] and result[1] == []
    assert deltas == [result[0].response]
    assert llm.final_payload is not None
    assert llm.final_payload["evidence"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question", ["Summarise this PDF", "What are the key takeaways?", "What is in this doc?"]
)
async def test_generic_questions_read_overviews_without_passage_search(question):
    llm = ScriptedAgent([[call("get_selected_document_overviews", {"cursor": None})], []])
    runtime = ScriptedRuntime(
        {
            "get_selected_document_overviews": ToolExecutionResult(
                content={
                    "status": "ok",
                    "items": [
                        {
                            "version_id": "version-1",
                            "document_id": "document-1",
                            "filename": "terms.txt",
                            "summary": "Payment terms",
                            "citation_ids": ["source-1"],
                        }
                    ],
                },
                scope="document",
                evidence=[source()],
            )
        }
    )
    result, _, traces = await run(llm, runtime, question)
    assert [name for name, _ in runtime.calls] == ["get_selected_document_overviews"]
    assert result[0].citation_ids == ["source-1"]
    assert "summary" not in traces[0]["document_references"][0]
    assert "Payment is due" not in json.dumps(traces)
    messages = llm.transcripts[1]
    assert messages[-2]["tool_calls"][0]["id"] == messages[-1]["tool_call_id"] == "call-1"


@pytest.mark.asyncio
async def test_failed_insights_can_fall_back_to_bounded_original_samples_in_the_overview_tool():
    llm = ScriptedAgent([[call("get_selected_document_overviews", {"cursor": None})], []])
    runtime = ScriptedRuntime(
        {
            "get_selected_document_overviews": ToolExecutionResult(
                content={
                    "status": "ok",
                    "items": [{"status": "failed", "version_id": "version-1"}],
                    "coverage": {"complete": False, "sampled": True},
                },
                scope="document",
                evidence=[source()],
            )
        }
    )
    result, _, traces = await run(llm, runtime, "Summarise this document")
    assert result[0].outcome == "answered" and len(traces) == 1
    assert [name for name, _ in runtime.calls] == ["get_selected_document_overviews"]
    assert llm.final_payload is not None
    assert llm.final_payload["observations"][0]["content"]["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_followup_receives_prior_ordered_referents_but_retrieves_fresh_evidence():
    retrieval = call(
        "retrieve_relevant_chunks", {"query": "When is the terms document payment due?"}
    )
    llm = ScriptedAgent([[retrieval], []])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"},
                scope="document",
                evidence=[source()],
                query=retrieval.arguments["query"],
            )
        }
    )
    history = [
        {
            "role": "assistant",
            "content": "Two documents found",
            "agent_trace": [
                {
                    "tool": "search_documents",
                    "document_references": [
                        {"version_id": "version-2", "filename": "first.txt"},
                        {"version_id": "version-1", "filename": "terms.txt"},
                    ],
                }
            ],
        }
    ]
    result, _, _ = await run(llm, runtime, "What about payment in the second one?", history)
    planning_input = json.loads(llm.transcripts[0][-1]["content"])
    assert (
        planning_input["previous_tool_references"][0]["tools"][0]["document_references"][1][
            "filename"
        ]
        == "terms.txt"
    )
    assert result[2] == retrieval.arguments["query"]
    assert llm.final_payload is not None
    assert llm.final_payload["evidence"][0]["text"] == source().text


@pytest.mark.asyncio
async def test_ambiguous_followup_clarifies_without_reading_or_inventing_sources():
    llm = ScriptedAgent(
        [[]],
        answer(
            "Do you mean the policy or proposal?",
            citation_ids=[],
            response_kind="clarification",
            outcome="clarification_needed",
        ),
    )
    runtime = ScriptedRuntime({})
    result, _, _ = await run(
        llm,
        runtime,
        "What does it cost?",
        [
            {"role": "user", "content": "Compare the policy and proposal"},
        ],
    )
    assert result[0].outcome == "clarification_needed" and runtime.calls == []


@pytest.mark.asyncio
async def test_parallel_reads_have_matching_results_and_fresh_workspace_receipts():
    llm = ScriptedAgent(
        [
            [
                call("get_document_processing_metrics", {"scope": "workspace"}),
                call("search_documents", {"query": "renewals", "cursor": None}, "call-2"),
            ],
            [],
        ],
        answer("There are two ready documents.", citation_ids=[], response_kind="workspace_answer"),
    )
    runtime = ScriptedRuntime(
        {
            name: ToolExecutionResult(
                content={"status": "ok", "counts": {"ready": 2}},
                scope="workspace",
            )
            for name in ("get_document_processing_metrics", "search_documents")
        }
    )
    runtime.delay = 0.02
    _, _, traces = await run(llm, runtime, "What documents are ready about renewals?")
    assert runtime.max_running == 2 and [item["tool_call_id"] for item in traces] == [
        "call-1",
        "call-2",
    ]
    assert [item["tool_call_id"] for item in llm.transcripts[1][-2:]] == ["call-1", "call-2"]


@pytest.mark.asyncio
async def test_pending_analysis_is_read_without_creating_or_claiming_a_completed_artifact():
    llm = ScriptedAgent(
        [[call("get_document_analysis_result", {"artifact_id": "artifact-1"})], []],
        answer(
            "Your saved analysis is pending.", citation_ids=[], response_kind="workspace_answer"
        ),
    )
    runtime = ScriptedRuntime(
        {
            "get_document_analysis_result": ToolExecutionResult(
                content={"status": "ok", "artifact_id": "artifact-1", "artifact_status": "pending"},
                scope="operational",
            )
        }
    )
    _, _, traces = await run(llm, runtime)
    assert runtime.calls == [("get_document_analysis_result", {"artifact_id": "artifact-1"})]
    assert traces[0]["artifact_status"] == "pending"
    assert llm.final_payload is not None
    assert llm.final_payload["observations"][0]["content"]["artifact_status"] == "pending"


@pytest.mark.asyncio
async def test_unknown_tool_error_does_not_become_a_successful_workspace_receipt():
    llm = ScriptedAgent(
        [[call("invented_tool")], []],
        answer(
            "Two jobs are running.",
            citation_ids=[],
            response_kind="workspace_answer",
        ),
    )
    runtime = ScriptedRuntime(
        {
            "invented_tool": ToolExecutionResult(
                content={"status": "error", "error": {"code": "unknown_tool"}},
                scope="operational",
            )
        }
    )
    with pytest.raises(InvalidCitationError, match="fresh successful"):
        await run(llm, runtime)


@pytest.mark.asyncio
async def test_tool_evidence_cannot_expand_the_captured_selected_scope():
    llm = ScriptedAgent([[call("retrieve_relevant_chunks", {"query": "payment"})], []])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"},
                scope="document",
                evidence=[source(version_id="unselected")],
            )
        }
    )
    with pytest.raises(InvalidCitationError, match="outside the selected"):
        await run(llm, runtime)
    assert llm.generations == 0


@pytest.mark.parametrize(
    "bad_response,citations",
    [
        ("Invented amount [invented]", ["source-1"]),
        ("Payment is due [source-1] [invented]", ["source-1"]),
        ("Payment is due without an inline reference", ["source-1"]),
        ("Payment is due [source-1]", []),
    ],
)
def test_inline_citations_must_match_the_original_structured_list(bad_response, citations):
    with pytest.raises(InvalidCitationError):
        validate_document_agent_answer(answer(bad_response, citation_ids=citations), [source()], [])


def test_help_cannot_bypass_document_grounding_or_claim_fresh_workspace_facts():
    with pytest.raises(InvalidCitationError):
        validate_document_agent_answer(answer(response_kind="assistant_help"), [source()], [])
    with pytest.raises(InvalidCitationError):
        validate_document_agent_answer(
            answer("Two jobs are running", citation_ids=[], response_kind="workspace_answer"),
            [],
            [],
        )


def test_markdown_checkboxes_are_not_interpreted_as_citation_ids():
    result = validate_document_agent_answer(
        answer("- [x] Payment due in 30 days. [source-1]"), [source()], []
    )
    assert result.citation_ids == ["source-1"]


@pytest.mark.asyncio
async def test_round_limit_and_duplicate_native_call_ids_stop_before_final_generation():
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"}, scope="document", evidence=[source()]
            )
        }
    )
    llm = ScriptedAgent(
        [
            [call("retrieve_relevant_chunks")],
            [call("retrieve_relevant_chunks", identifier="call-2")],
        ]
    )
    with pytest.raises(ValueError, match="tool limit"):
        await run(llm, runtime, max_tool_rounds=1)
    assert llm.generations == 0 and len(runtime.calls) == 1
    llm = ScriptedAgent([[call("retrieve_relevant_chunks"), call("retrieve_relevant_chunks")]])
    with pytest.raises(ValueError, match="duplicate"):
        await run(llm, runtime)
    assert len(runtime.calls) == 1


@pytest.mark.asyncio
async def test_deadline_cancels_active_read_without_emitting_completed_answer():
    llm = ScriptedAgent([[call("retrieve_relevant_chunks")], []])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"}, scope="document"
            )
        }
    )
    runtime.delay = 1
    with pytest.raises(TimeoutError):
        await run(llm, runtime, timeout_seconds=0.02)
    assert llm.generations == 0 and runtime.running == 0


@pytest.mark.asyncio
async def test_cancellation_at_trace_boundary_prevents_late_final_publication():
    llm = ScriptedAgent([[call("retrieve_relevant_chunks")], []])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"}, scope="document", evidence=[source()]
            )
        }
    )

    async def cancel_after_tool(value):
        """Simulate a persisted cancellation discovered when recording the tool receipt."""
        if value.get("execution_status") == "completed":
            raise asyncio.CancelledError

    async def delta(value):
        """Reject any generation that incorrectly survives cancellation."""
        pytest.fail("Cancelled execution cannot emit an answer")

    with pytest.raises(asyncio.CancelledError):
        await run_document_agent_workflow(
            "Payment?",
            [],
            cast("DocumentToolRuntime", runtime),
            llm,
            delta,
            cancel_after_tool,
        )
    assert llm.generations == 0


def test_ready_artifact_receipt_supports_status_reply_without_borrowing_document_citations():
    """A ready artifact has both operational status and separately validated original document evidence."""
    final = answer("The summary is ready.", citation_ids=[], response_kind="workspace_answer")
    receipt = {
        "tool": "get_document_analysis_result",
        "scope": "document",
        "content": {
            "status": "ok",
            "artifact_id": "artifact-1",
            "artifact_status": "ready",
            "observed_at": "2026-10-05T12:00:00Z",
        },
    }
    assert validate_document_agent_answer(final, [source()], [receipt]).citation_ids == []
    with pytest.raises(InvalidCitationError):
        validate_document_agent_answer(
            final, [source()], [{**receipt, "tool": "get_selected_document_overviews"}]
        )


@pytest.mark.asyncio
async def test_formatting_followup_reuses_reloaded_original_sources_without_native_tool_calls():
    """Past assistant prose identifies intent while original DB-derived passages ground the reply."""
    llm = ScriptedAgent([[]], answer("| Payment | 30 days | [source-1]"))
    runtime = ScriptedRuntime({})
    history = [
        {
            "role": "assistant",
            "content": "Payment is in 99 days (untrusted prior text).",
            "message_id": "previous-message",
            "version_ids": ["version-1"],
            "citation_ids": ["source-1"],
        }
    ]
    result, _, traces = await run(llm, runtime, "Put that term into a compact table", history)
    assert runtime.calls == [] and runtime.previous_reads == [(["source-1"], ["version-1"])]
    assert result[1] == [source()]
    assert llm.final_payload is not None
    assert llm.final_payload["evidence"][0]["text"] == "Payment is due within 30 days."
    planning = json.loads(llm.transcripts[0][-1]["content"])
    assert planning["previous_answer_source_ids"] == ["source-1"]
    assert (
        traces[0]["server_evidence_reuse"] is True
        and traces[0]["source_message_id"] == "previous-message"
    )
    assert "99 days" not in json.dumps(traces)


@pytest.mark.asyncio
async def test_changed_selection_drops_old_citations_and_cannot_ground_a_no_tool_followup():
    llm = ScriptedAgent([[]])
    runtime = ScriptedRuntime({})
    history = [
        {
            "role": "assistant",
            "content": "Payment is 30 days [source-1]",
            "version_ids": ["old-version"],
            "citation_ids": ["source-1"],
        }
    ]
    with pytest.raises(InvalidCitationError, match="unknown source"):
        await run(llm, runtime, "Put that in a table", history)
    assert runtime.previous_reads == []


@pytest.mark.asyncio
async def test_help_after_a_document_turn_does_not_record_unused_evidence_as_a_tool():
    llm = ScriptedAgent(
        [[]], answer("I can explain documents.", citation_ids=[], response_kind="assistant_help")
    )
    runtime = ScriptedRuntime({})
    history = [
        {
            "role": "assistant",
            "content": "Payment terms",
            "message_id": "previous",
            "version_ids": ["version-1"],
            "citation_ids": ["source-1"],
        }
    ]
    _, _, traces = await run(llm, runtime, "How can you help me?", history)
    assert runtime.calls == [] and traces == []


@pytest.mark.asyncio
async def test_reuse_trace_cancellation_fences_the_no_tool_formatting_completion():
    llm = ScriptedAgent([[]])
    runtime = ScriptedRuntime({})
    history = [
        {
            "role": "assistant",
            "content": "Old answer",
            "message_id": "previous",
            "version_ids": ["version-1"],
            "citation_ids": ["source-1"],
        }
    ]

    async def on_delta(value):
        """Allow provisional text; only validated active generations may complete."""
        return None

    async def cancel_at_receipt(value):
        """Simulate discovering a persisted cancellation while recording reused-source provenance."""
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_document_agent_workflow(
            "Format that",
            history,
            cast("DocumentToolRuntime", runtime),
            llm,
            on_delta,
            cancel_at_receipt,
        )


class RepairScriptedAgent(ScriptedAgent):
    """Supply distinct first and repair candidates while validating each strict response."""

    def __init__(self, candidates):
        """Keep a finite candidate sequence so unlimited repair attempts fail the test."""
        super().__init__([[call("retrieve_relevant_chunks", {"query": "payment"})], []])
        self.candidates = iter(candidates)
        self.generation_payloads = []

    async def _answer_turn(self, messages, on_delta, evidence_ids):
        """Record identical native source receipts during one bounded reference correction."""
        self.generations += 1
        self.generation_payloads.append(read_native_chat_payload(messages))
        candidate = next(self.candidates)
        await on_delta(candidate.response)
        return ChatAgentTurn(
            assistant_message={"role": "assistant", "content": candidate.model_dump_json()},
            tool_calls=[],
            answer=candidate,
        )


@pytest.mark.asyncio
async def test_one_reference_repair_keeps_grounding_and_does_not_concatenate_streams():
    bad = answer("Payment is due [invented]", citation_ids=["invented"])
    good = answer()
    llm = RepairScriptedAgent([bad, good])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"},
                scope="document",
                evidence=[source()],
            )
        }
    )
    result, deltas, _ = await run(llm, runtime)
    assert llm.generations == 2 and result[0] == good
    assert deltas == [bad.response]
    repair = llm.generation_payloads[1]
    assert repair["allowed_original_evidence_ids"] == ["source-1"]
    assert repair["invalid_candidate"] == bad.model_dump()
    assert repair["evidence"] == llm.generation_payloads[0]["evidence"]
    assert repair["observations"] == llm.generation_payloads[0]["observations"]


@pytest.mark.asyncio
async def test_bad_reference_after_one_repair_remains_a_failure():
    bad = answer("Payment is due [invented]", citation_ids=["invented"])
    llm = RepairScriptedAgent([bad, bad])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"},
                scope="document",
                evidence=[source()],
            )
        }
    )
    with pytest.raises(InvalidCitationError):
        await run(llm, runtime)
    assert llm.generations == 2


@pytest.mark.asyncio
async def test_missing_workspace_receipt_never_triggers_reference_repair():
    bad = answer(
        "Two jobs are running [invented]",
        citation_ids=["invented"],
        response_kind="workspace_answer",
    )
    llm = ScriptedAgent([[]], bad)
    runtime = ScriptedRuntime({})
    with pytest.raises(InvalidCitationError):
        await run(llm, runtime)
    assert llm.generations == 1


@pytest.mark.asyncio
async def test_reference_repair_cannot_reclassify_claims_as_help_to_bypass_grounding():
    bad = answer("Payment is due [invented]", citation_ids=["invented"])
    bypass = answer("Payment is due.", citation_ids=[], response_kind="assistant_help")
    llm = RepairScriptedAgent([bad, bypass])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"},
                scope="document",
                evidence=[source()],
            )
        }
    )
    with pytest.raises(InvalidCitationError, match="grounding category"):
        await run(llm, runtime)
    assert llm.generations == 2


@pytest.mark.asyncio
async def test_selector_system_prompt_catalog_matches_the_native_six_tool_contracts():
    from docvault.tools.definitions import build_agent_tool_definitions

    llm = ScriptedAgent(
        [[]],
        answer("I can help with your documents.", citation_ids=[], response_kind="assistant_help"),
    )
    await run(llm, ScriptedRuntime({}), "How can you help me?")
    catalog = llm.transcripts[0][0]["content"]
    native_names = {item["function"]["name"] for item in build_agent_tool_definitions()}
    assert len(native_names) == 6
    assert all(f"{name}:" in catalog for name in native_names)
    assert not any(
        name in catalog
        for name in (
            "read_document_sections",
            "request_document_summary",
            "compare_selected_documents",
        )
    )


@pytest.mark.asyncio
async def test_tool_lifecycle_reports_pending_and_running_before_read_completion():
    llm = ScriptedAgent([[call("retrieve_relevant_chunks", {"query": "payment"})], []])
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok"}, evidence=[source()], scope="document"
            )
        }
    )
    runtime.delay = 0.02
    await run(llm, runtime)
    assert [item["execution_status"] for item in llm.trace_updates] == [
        "pending",
        "running",
        "completed",
    ]
    assert [item["status"] for item in llm.trace_updates] == ["pending", "running", "ok"]
    assert all(item["tool_call_id"] == "call-1" for item in llm.trace_updates)
    assert llm.trace_updates[0]["evidence_ids"] == [] and llm.trace_updates[-1]["evidence_ids"] == [
        "source-1"
    ]


@pytest.mark.asyncio
async def test_safe_tool_application_error_reports_failed_state_and_actionable_message():
    llm = ScriptedAgent(
        [[call("get_document_processing_diagnostics", {"version_id": "wrong"})], []],
        answer(
            "Please select that document.",
            citation_ids=[],
            response_kind="clarification",
            outcome="clarification_needed",
        ),
    )
    runtime = ScriptedRuntime(
        {
            "get_document_processing_diagnostics": ToolExecutionResult(
                content={
                    "status": "error",
                    "error": {
                        "code": "source_out_of_scope",
                        "message": "Select this document first.",
                        "retryable": False,
                    },
                },
                scope="operational",
            )
        }
    )
    await run(llm, runtime)
    assert [item["execution_status"] for item in llm.trace_updates] == [
        "pending",
        "running",
        "failed",
    ]
    assert llm.trace_updates[-1]["error"]["message"] == "Select this document first."


@pytest.mark.asyncio
async def test_raised_tool_failure_reports_safe_terminal_state_and_preserves_failure():
    class FailingRuntime(ScriptedRuntime):
        async def execute(self, name, arguments):
            """Raise an infrastructure error whose private detail must stay out of progress events."""
            raise RuntimeError("Private service credential detail")

    llm = ScriptedAgent([[call("get_document_processing_metrics", {"scope": "workspace"})], []])
    with pytest.raises(RuntimeError, match="Private service"):
        await run(llm, FailingRuntime({}))
    assert llm.generations == 0
    assert [item["execution_status"] for item in llm.trace_updates] == [
        "pending",
        "running",
        "failed",
    ]
    assert "Private service" not in json.dumps(llm.trace_updates)


@pytest.mark.asyncio
async def test_operational_reference_repair_needs_real_receipt_and_removes_document_citations():
    bad = answer(
        "One ready document [invented]", citation_ids=["invented"], response_kind="workspace_answer"
    )
    good = answer("One document is ready.", citation_ids=[], response_kind="workspace_answer")
    llm = RepairScriptedAgent([bad, good])
    llm.rounds = iter([[call("get_document_processing_metrics", {"scope": "workspace"})], []])
    runtime = ScriptedRuntime(
        {
            "get_document_processing_metrics": ToolExecutionResult(
                content={"status": "ok", "counts": {"ready": 1}},
                scope="operational",
            )
        }
    )
    result, _, _ = await run(llm, runtime, "How many documents are ready?")
    assert result[0] == good and llm.generations == 2
    assert llm.generation_payloads[1]["allowed_original_evidence_ids"] == []
    assert llm.generation_payloads[1]["observations"] == llm.generation_payloads[0]["observations"]


@pytest.mark.asyncio
async def test_selector_metadata_does_not_offer_volatile_counts_or_processing_states():
    class OperationalMetadataRuntime(ScriptedRuntime):
        def metadata(self):
            """Provide full runtime metadata; only stable identities belong in the planning input."""
            return [
                {
                    **super().metadata()[0],
                    "status": "ready",
                    "insight_status": "failed",
                    "latest_version_id": "latest",
                }
            ]

    llm = ScriptedAgent(
        [[]], answer("I can help with documents.", citation_ids=[], response_kind="assistant_help")
    )
    await run(llm, OperationalMetadataRuntime({}), "How can you help?")
    payload = json.loads(llm.transcripts[0][-1]["content"])
    selected = payload["selected_documents"][0]
    assert selected["version_id"] == "version-1" and selected["filename"] == "terms.txt"
    assert not {"status", "insight_status", "latest_version_id"}.intersection(selected)


@pytest.mark.asyncio
async def test_empty_retrieval_allows_one_native_overview_recovery_before_finalizing(monkeypatch):
    llm = ScriptedAgent(
        [
            [call("retrieve_relevant_chunks", {"query": "payment"})],
            [],
            [call("get_selected_document_overviews", {"cursor": None}, "overview-recovery")],
            [],
        ]
    )
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok", "passages": []}, scope="document"
            ),
            "get_selected_document_overviews": ToolExecutionResult(
                content={"status": "ok"}, evidence=[source()], scope="document"
            ),
        }
    )
    original_answer = llm.final
    original_answer_turn = llm._answer_turn

    async def recoverable_answer(messages, on_delta, evidence_ids):
        """Model abstains on an empty result, then answers when actual source support arrives."""
        llm.final = (
            original_answer
            if evidence_ids
            else answer("No supporting evidence.", citation_ids=[], outcome="insufficient_evidence")
        )
        return await original_answer_turn(messages, on_delta, evidence_ids)

    monkeypatch.setattr(llm, "_answer_turn", recoverable_answer)
    result, _, _ = await run(llm, runtime)
    assert result[0].citation_ids == ["source-1"]
    assert [name for name, _ in runtime.calls] == [
        "retrieve_relevant_chunks",
        "get_selected_document_overviews",
    ]
    assert llm.transcripts[2][-1]["role"] == "user"
    assert "No matching passages" in llm.transcripts[2][-1]["content"]


@pytest.mark.asyncio
async def test_native_recovery_can_stop_without_evidence_but_cannot_repeat_or_fabricate_sources():
    llm = ScriptedAgent(
        [[call("retrieve_relevant_chunks", {"query": "missing"})], [], []],
        answer(
            "There is insufficient supporting evidence.",
            citation_ids=[],
            outcome="insufficient_evidence",
        ),
    )
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok", "passages": []}, scope="document"
            )
        }
    )
    result, _, _ = await run(llm, runtime)
    assert len(llm.transcripts) == 3 and len(runtime.calls) == 1
    assert result[0].outcome == "insufficient_evidence" and result[0].citation_ids == []


@pytest.mark.asyncio
async def test_empty_retrieval_does_not_retry_an_already_attempted_unavailable_overview():
    llm = ScriptedAgent(
        [
            [
                call("retrieve_relevant_chunks", {"query": "missing"}),
                call("get_selected_document_overviews", {"cursor": None}, "overview"),
            ],
            [],
        ],
        answer(
            "No supporting sources are available.", citation_ids=[], outcome="insufficient_evidence"
        ),
    )
    runtime = ScriptedRuntime(
        {
            "retrieve_relevant_chunks": ToolExecutionResult(
                content={"status": "ok", "passages": []}, scope="document"
            ),
            "get_selected_document_overviews": ToolExecutionResult(
                content={"status": "error", "error": {"code": "not_ready"}}, scope="operational"
            ),
        }
    )
    result, _, _ = await run(llm, runtime)
    assert len(llm.transcripts) == 2 and len(runtime.calls) == 2
    assert result[0].outcome == "insufficient_evidence"
