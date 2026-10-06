"""Explicit LangGraph chat flow. PostgreSQL owns history and recovery.

This graph deliberately has no checkpointer and does not promise durable resume.
https://docs.langchain.com/oss/python/langgraph/graph-api
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, TypedDict

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph

from docvault.llm.evidence import build_llm_evidence_record
from docvault.llm.models import Evidence
from docvault.llm.prompts import CHAT_SYSTEM_PROMPT
from docvault.llm.provider import DeltaCallback, OpenRouterLLM
from docvault.tools.models import (
    AgentChatGenerationLLMResponse,
    AgentToolCall,
    ToolExecutionResult,
)

if TYPE_CHECKING:
    from docvault.tools.runtime import DocumentToolRuntime


class InvalidCitationError(ValueError):
    """Reject a final result or tool evidence that violates the grounding contract."""


class CitationReferenceIntegrityError(InvalidCitationError):
    """A final reference uses unknown IDs or differs from its structured citation list."""


def validate_citation_ids(citation_ids: list[str], evidence: list[Evidence]) -> None:
    """Reject IDs outside the supplied evidence; this does not verify claim support."""
    allowed = {item.id for item in evidence}
    if set(citation_ids) - allowed:
        raise InvalidCitationError("The generated result referenced an unknown source.")


class DocumentAgentState(TypedDict):
    """Native call/result transcript and fresh receipts for one bounded agent invocation."""

    messages: list[dict]
    calls: list[AgentToolCall]
    evidence: list[Evidence]
    observations: list[dict]
    trace: list[dict]
    rounds: int
    overview_recovery_attempted: bool
    query: str
    answer: AgentChatGenerationLLMResponse | None


def build_compact_tool_trace(
    call: AgentToolCall, result: ToolExecutionResult, timestamp: str
) -> dict:
    """Keep server-issued referents and receipt metadata without duplicating source passages."""
    content = result.content
    references = []
    for item in content.get("items", []):
        if isinstance(item, dict):
            references.append(
                {
                    key: item[key]
                    for key in (
                        "document_id",
                        "version_id",
                        "filename",
                        "version_number",
                        "status",
                        "artifact_id",
                        "category",
                        "title",
                    )
                    if key in item
                }
            )
    trace = {
        "tool_call_id": call.id,
        "tool": call.name,
        "arguments": call.arguments,
        "scope": result.scope,
        "status": content.get("status", "ok"),
        "execution_status": "failed" if content.get("status") == "error" else "completed",
        "observed_at": timestamp,
        "evidence_ids": [item.id for item in result.evidence],
        "version_ids": list(dict.fromkeys(item.version_id for item in result.evidence)),
        "document_references": references,
    }
    for key in (
        "artifact_id",
        "artifact_status",
        "result_path",
        "document_id",
        "version_id",
        "next_cursor",
        "counts",
        "total",
    ):
        if key in content:
            trace[key] = content[key]
    error = content.get("error")
    if isinstance(error, dict):
        trace["error_code"] = error.get("code")
        trace["error"] = {
            key: error[key] for key in ("code", "message", "retryable") if key in error
        }
    return trace


def validate_answer_reference_integrity(
    answer: AgentChatGenerationLLMResponse,
    evidence: list[Evidence],
) -> None:
    """Reject invented IDs and inconsistent inline references without changing source scope."""
    try:
        validate_citation_ids(answer.citation_ids, evidence)
    except InvalidCitationError as exc:
        raise CitationReferenceIntegrityError(str(exc)) from exc
    # Links use [label](url); bare bracket identifiers are the citation syntax.
    citation_text = re.sub(r"(?m)^(\s*[-*+]\s+)\[[ xX]\]", r"\1", answer.response)
    inline = {
        match
        for match in re.findall(r"(?<!\\)\[([^\]\n]+)\](?!\()", citation_text)
        if re.fullmatch(r"[A-Za-z0-9_-]+", match)
    }
    listed = set(answer.citation_ids)
    if inline - listed or listed - inline:
        raise CitationReferenceIntegrityError(
            "Inline source references must match the structured citations."
        )


def has_fresh_workspace_receipt(observations: list[dict]) -> bool:
    """Accept only successful fresh operational/catalog receipts, including saved-artifact status."""
    receipts = [
        item
        for item in observations
        if (
            item["scope"] in {"workspace", "operational"}
            or (
                item["tool"] == "get_document_analysis_result"
                and all(
                    item["content"].get(key)
                    for key in ("artifact_id", "artifact_status", "observed_at")
                )
            )
        )
        and item["content"].get("status") == "ok"
    ]
    return bool(receipts)


def validate_document_agent_answer(
    answer: AgentChatGenerationLLMResponse,
    evidence: list[Evidence],
    observations: list[dict],
) -> AgentChatGenerationLLMResponse:
    """Enforce original-source citations or fresh operational receipts for the declared answer kind."""
    validate_answer_reference_integrity(answer, evidence)
    listed = set(answer.citation_ids)
    if answer.response_kind == "document_answer":
        if answer.outcome == "answered" and not listed:
            raise InvalidCitationError("A document answer must cite original supporting sources.")
    elif answer.response_kind == "assistant_help":
        if observations or listed:
            raise InvalidCitationError("A help response cannot claim document or tool evidence.")
    elif answer.response_kind == "workspace_answer":
        if not has_fresh_workspace_receipt(observations):
            raise InvalidCitationError("A workspace answer needs a fresh successful tool receipt.")
        if listed:
            raise CitationReferenceIntegrityError(
                "Operational answers must not include document citation IDs."
            )
    elif answer.response_kind == "clarification":
        if listed or answer.outcome != "clarification_needed":
            raise InvalidCitationError("A clarification must ask for input without source claims.")
    return answer.model_copy(update={"citation_ids": list(dict.fromkeys(answer.citation_ids))})


async def run_document_agent_workflow(
    question: str,
    history: list[dict],
    runtime: DocumentToolRuntime,
    llm: OpenRouterLLM,
    on_delta: DeltaCallback,
    on_tool_trace: Callable[[dict], Awaitable[None]],
    *,
    max_tool_rounds: int = 6,
    timeout_seconds: float = 600,
) -> tuple[AgentChatGenerationLLMResponse, list[Evidence], str, list[dict]]:
    """Run zero or more scoped tools through conditional edges, then stream and validate a final reply.

    Native call IDs remain paired with their results. Independent scoped reads run
    concurrently and report pending, running and terminal state. PostgreSQL remains the caller's history
    authority: this graph neither checkpoints nor promises mid-turn durable resume.
    """
    from docvault.tools.definitions import (
        build_agent_tool_catalog_prompt,
        build_agent_tool_definitions,
    )

    if not question.strip():
        raise ValueError("A question is required.")
    if max_tool_rounds < 1 or timeout_seconds <= 0:
        raise ValueError("Agent execution bounds must be positive.")
    turns = [
        {"role": item["role"], "content": item["content"]}
        for item in history
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    ]
    references = [
        {"turn": index, "tools": item["agent_trace"]}
        for index, item in enumerate(history)
        if item.get("role") == "assistant"
        and isinstance(item.get("agent_trace"), list)
        and item["agent_trace"]
    ]
    started_at = asyncio.get_running_loop().time()
    metadata = await asyncio.wait_for(asyncio.to_thread(runtime.metadata), timeout_seconds)
    tools = build_agent_tool_definitions()
    allowed_versions = set(runtime.version_ids)
    previous = next((item for item in reversed(history) if item.get("role") == "assistant"), None)
    previous_evidence: list[Evidence] = []
    if (
        previous is not None
        and isinstance(previous.get("citation_ids"), list)
        and isinstance(previous.get("version_ids"), list)
        and set(previous["version_ids"]) == allowed_versions
    ):
        remaining = timeout_seconds - (asyncio.get_running_loop().time() - started_at)
        previous_evidence = await asyncio.wait_for(
            runtime.read_previous_citation_evidence(
                previous["citation_ids"], previous["version_ids"]
            ),
            remaining,
        )
        if any(item.version_id not in allowed_versions for item in previous_evidence):
            raise InvalidCitationError(
                "Previous source references are outside the selected sources."
            )

    async def chat(state: DocumentAgentState) -> dict:
        """Let one chat-model turn request scoped tools or stream the final answer."""
        messages = state["messages"]
        empty_retrieval = any(
            item["tool"] == "retrieve_relevant_chunks"
            and item["content"].get("status") == "ok"
            and item["content"].get("passages") == []
            for item in state["observations"]
        )
        overview_attempted = any(
            item["tool"] == "get_selected_document_overviews" for item in state["observations"]
        )
        may_recover = (
            empty_retrieval
            and not overview_attempted
            and not state["overview_recovery_attempted"]
            and state["rounds"] < max_tool_rounds
        )
        buffered: list[str] = []

        async def buffer_delta(value: str) -> None:
            """Hold a premature absence answer until the bounded overview recovery decision."""
            buffered.append(value)

        turn = await llm.stream_chat_turn(
            messages,
            tools,
            buffer_delta if may_recover else on_delta,
            evidence_ids=[item.id for item in state["evidence"]],
        )
        recovered = state["overview_recovery_attempted"]
        if (
            may_recover
            and turn.answer is not None
            and turn.answer.outcome == "insufficient_evidence"
        ):
            messages = [
                *messages,
                turn.assistant_message,
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": question,
                            "retrieval_feedback": "No matching passages does not establish document-wide absence. Read selected document overviews for bounded support before abstaining, or clarify an ambiguous subject.",
                        }
                    ),
                },
            ]
            turn = await llm.stream_chat_turn(
                messages, tools, on_delta, evidence_ids=[item.id for item in state["evidence"]]
            )
            recovered = True
        else:
            for value in buffered:
                await on_delta(value)
        if bool(turn.tool_calls) == (turn.answer is not None):
            raise ValueError("A chat turn must return tools or an answer, not both or neither.")
        if turn.tool_calls and state["rounds"] >= max_tool_rounds:
            raise ValueError("The assistant reached its tool limit. Please narrow the request.")
        seen_ids = {item["tool_call_id"] for item in state["trace"]}
        batch_ids = [item.id for item in turn.tool_calls]
        if len(set(batch_ids)) != len(batch_ids) or seen_ids.intersection(batch_ids):
            raise ValueError("The model returned duplicate tool call identifiers.")
        return {
            "messages": [*messages, turn.assistant_message],
            "calls": turn.tool_calls,
            "answer": turn.answer,
            "overview_recovery_attempted": recovered,
        }

    async def execute_tools(state: DocumentAgentState) -> dict:
        """Run independent scoped reads and publish each call's lifecycle without source text."""
        calls = state["calls"]
        unique = {item.id: item for item in state["evidence"]}

        def pending_trace(call: AgentToolCall) -> dict:
            """Build a compact model-request record before any handler has executed."""
            return {
                "tool_call_id": call.id,
                "tool": call.name,
                "arguments": call.arguments,
                "scope": "operational",
                "status": "pending",
                "execution_status": "pending",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "evidence_ids": [],
                "version_ids": [],
                "document_references": [],
            }

        for call in calls:
            await on_tool_trace(pending_trace(call))

        async def execute_one(call: AgentToolCall) -> tuple[ToolExecutionResult, dict]:
            """Publish running and terminal state; failed/cancelled reads cannot claim completion."""
            active = {**pending_trace(call), "status": "running", "execution_status": "running"}
            await on_tool_trace(active)
            try:
                result = await runtime.execute(call.name, call.arguments)
                for item in result.evidence:
                    if item.version_id not in allowed_versions:
                        raise InvalidCitationError(
                            "A tool returned evidence outside the selected sources."
                        )
                    if item.id in unique and unique[item.id] != item:
                        raise InvalidCitationError("Retrieved source identifiers are inconsistent.")
                    unique[item.id] = item
                trace = build_compact_tool_trace(
                    call, result, datetime.now(timezone.utc).isoformat()
                )
            except BaseException as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                failed = {
                    **active,
                    "status": "error",
                    "execution_status": "failed",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "error_code": "tool_cancelled" if cancelled else "tool_execution_failed",
                    "error": {
                        "code": "tool_cancelled" if cancelled else "tool_execution_failed",
                        "message": "Tool execution stopped."
                        if cancelled
                        else "The tool could not finish. Please retry.",
                        "retryable": not cancelled,
                    },
                }
                try:
                    await on_tool_trace(failed)
                except asyncio.CancelledError:
                    # Persisted cancellation may fence the cleanup callback itself.
                    pass
                raise
            await on_tool_trace(trace)
            return result, trace

        # All exposed tools are independent reads; sibling tasks are cancelled on a raised failure.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(execute_one(call)) for call in calls]
        except ExceptionGroup as exc:
            if len(exc.exceptions) == 1:
                raise exc.exceptions[0]
            raise
        results = [task.result() for task in tasks]
        messages, observations, traces = [], [], []
        query = state["query"]
        for call, (result, trace) in zip(calls, results, strict=True):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result.content, ensure_ascii=False),
                }
            )
            observations.append(
                {"tool": call.name, "scope": result.scope, "content": result.content}
            )
            traces.append(trace)
            if result.query:
                query = result.query
        return {
            "messages": [*state["messages"], *messages],
            "observations": [*state["observations"], *observations],
            "trace": [*state["trace"], *traces],
            "evidence": list(unique.values()),
            "rounds": state["rounds"] + 1,
            "query": query,
        }

    async def validate_answer(state: DocumentAgentState) -> dict:
        """Validate provenance; allow one reference-only repair using the same chat and sources."""
        answer = state["answer"]
        if answer is None:
            raise ValueError("The agent did not produce an answer.")
        try:
            answer = validate_document_agent_answer(
                answer, state["evidence"], state["observations"]
            )
        except CitationReferenceIntegrityError as exc:
            original_kind = answer.response_kind
            if original_kind not in {"document_answer", "workspace_answer"}:
                raise
            if original_kind == "workspace_answer" and not has_fresh_workspace_receipt(
                state["observations"]
            ):
                raise
            identifiers = (
                [item.id for item in state["evidence"]]
                if original_kind == "document_answer"
                else []
            )
            correction = {
                "validation_error": str(exc),
                "allowed_original_evidence_ids": identifiers,
                "invalid_candidate": answer.model_dump(),
                "instruction": "Correct inline/list references using the supplied original IDs. Remove unsupported claims; preserve answer kind, scope and facts. Operational replies require the existing fresh receipts and no citations.",
            }

            async def discard_repair_delta(value: str) -> None:
                """Completion replaces provisional text; never concatenate two candidate answers."""
                return None

            turn = await llm.stream_chat_turn(
                [*state["messages"], {"role": "user", "content": json.dumps(correction)}],
                [],
                discard_repair_delta,
                evidence_ids=identifiers,
            )
            if turn.tool_calls or turn.answer is None:
                raise InvalidCitationError("Reference repair must return an answer without tools.")
            answer = turn.answer
            if answer.response_kind != original_kind:
                raise InvalidCitationError("Citation repair cannot change the grounding category.")
            answer = validate_document_agent_answer(
                answer, state["evidence"], state["observations"]
            )
        traces = state["trace"]
        reused = [item for item in previous_evidence if item.id in answer.citation_ids]
        fresh_ids = {identifier for trace in traces for identifier in trace["evidence_ids"]}
        reused = [item for item in reused if item.id not in fresh_ids]
        if answer.response_kind == "document_answer" and reused and previous is not None:
            # This is a server-side evidence read, not a model-issued native tool call.
            trace = {
                "tool": "reuse_previous_citation_evidence",
                "tool_call_id": f"history:{previous.get('message_id', 'previous')}",
                "arguments": {},
                "scope": "document",
                "status": "ok",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "execution_status": "completed",
                "source_message_id": previous.get("message_id"),
                "evidence_ids": [item.id for item in reused],
                "version_ids": list(dict.fromkeys(item.version_id for item in reused)),
                "document_references": [],
                "server_evidence_reuse": True,
            }
            await on_tool_trace(trace)
            traces = [*traces, trace]
        return {"answer": answer, "trace": traces}

    def route_selection(state: DocumentAgentState) -> str:
        """Continue through tool execution only when the model requested calls."""
        return "tools" if state["calls"] else "validate"

    graph = StateGraph(DocumentAgentState)
    graph.add_node("chat", chat)
    graph.add_node("tools", execute_tools)
    graph.add_node("validate", validate_answer)
    graph.add_edge(START, "chat")
    graph.add_conditional_edges("chat", route_selection, {"tools": "tools", "validate": "validate"})
    graph.add_edge("tools", "chat")
    graph.add_edge("validate", END)
    initial: DocumentAgentState = {
        "messages": [
            {
                "role": "system",
                "content": CHAT_SYSTEM_PROMPT + "\n\n" + build_agent_tool_catalog_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": question,
                        "conversation": turns,
                        "selected_documents": [
                            {
                                key: item[key]
                                for key in (
                                    "document_id",
                                    "version_id",
                                    "filename",
                                    "title",
                                    "version_number",
                                )
                                if key in item
                            }
                            for item in metadata
                        ],
                        "previous_tool_references": references,
                        "previous_answer_source_ids": [item.id for item in previous_evidence],
                        "evidence": [build_llm_evidence_record(item) for item in previous_evidence],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "calls": [],
        "evidence": previous_evidence,
        "observations": [],
        "trace": [],
        "rounds": 0,
        "overview_recovery_attempted": False,
        "query": question,
        "answer": None,
    }
    remaining = timeout_seconds - (asyncio.get_running_loop().time() - started_at)
    try:
        async with asyncio.timeout(remaining):
            result = await graph.compile().ainvoke(
                initial, {"recursion_limit": max_tool_rounds * 2 + 6}
            )
    except NodeCancelledError as exc:
        # LangGraph wraps cancellation raised inside a node; callers still own cancellation.
        raise asyncio.CancelledError from exc
    answer = result.get("answer")
    if not isinstance(answer, AgentChatGenerationLLMResponse):
        raise ValueError("The agent did not produce a validated answer.")
    return answer, result["evidence"], result["query"], result["trace"]
