"""Explicit LangGraph chat flow. PostgreSQL owns history and recovery.

This graph deliberately has no checkpointer and does not promise durable resume.
https://docs.langchain.com/oss/python/langgraph/graph-api
"""

import json
from collections.abc import Awaitable, Callable
from typing import Required, TypedDict

from langgraph.graph import END, START, StateGraph

from docvault.llm.models import ChatGenerationLLMResponse, Evidence, InputQueryRewriteLLMResponse
from docvault.llm.prompts import CHAT_SYSTEM_PROMPT, INPUT_QUERY_REWRITE_SYSTEM_PROMPT
from docvault.llm.provider import DeltaCallback, OpenRouterLLM


class InvalidCitationError(ValueError):
    pass


class ChatState(TypedDict):
    """Complete invocation state; answer is present and None until generation finishes."""

    question: Required[str]
    query: str
    history: Required[list[dict]]
    clarification: str
    evidence: list[Evidence]
    answer: ChatGenerationLLMResponse | None


def validate_citation_ids(citation_ids: list[str], evidence: list[Evidence]) -> None:
    """Reject IDs outside the supplied evidence; this does not verify claim support."""
    allowed = {item.id for item in evidence}
    if set(citation_ids) - allowed:
        raise InvalidCitationError("The generated result referenced an unknown source.")


def serialize_evidence_payload(evidence: list[Evidence]) -> str:
    """Serialize source passages and their metadata as Unicode-preserving JSON."""
    return json.dumps([item.model_dump() for item in evidence], ensure_ascii=False)


async def run_document_chat_workflow(
    question: str,
    history: list[dict],
    retrieve_relevant_chunks: Callable[[str], Awaitable[list[Evidence]]],
    llm: OpenRouterLLM,
    on_delta: DeltaCallback,
) -> tuple[ChatGenerationLLMResponse, list[Evidence], str]:
    """Run question rewriting, retrieval, generation, and citation validation.

    Return the validated answer, retrieved evidence, and standalone query. Model
    text reaches on_delta provisionally; the caller owns persistence. An empty
    question or invalid source references fail instead of returning an answer.
    """
    if not question.strip():
        raise ValueError("A question is required.")
    # Caller supplies persisted conversational turns, never system instructions.
    turns = [
        {"role": item["role"], "content": item["content"]}
        for item in history
        if item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)
    ]

    async def input_query_rewrite(state: ChatState) -> dict:
        """Resolve follow-up references with the model, or request clarification."""
        if not state["history"]:
            return {"query": state["question"], "clarification": ""}
        result = await llm.generate_structured_response(
            InputQueryRewriteLLMResponse,
            [
                {
                    "role": "system",
                    "content": INPUT_QUERY_REWRITE_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "conversation": state["history"],
                            "question": state["question"],
                        }
                    ),
                },
            ],
            task="input_query_rewrite",
        )
        return {
            "query": result.standalone_question,
            "clarification": (
                result.clarification_question or "Which document or detail do you mean?"
            )
            if result.needs_clarification
            else "",
        }

    async def retrieve_grounding_evidence(state: ChatState) -> dict:
        """Retrieve fresh evidence unless clarification is needed; reject conflicting IDs."""
        if state["clarification"]:
            return {"evidence": []}
        found = await retrieve_relevant_chunks(state["query"])
        # Stable IDs must identify exactly one passage, not two conflicting sources.
        unique: dict[str, Evidence] = {}
        for item in found:
            if item.id in unique and unique[item.id] != item:
                raise InvalidCitationError("Retrieved source identifiers are inconsistent.")
            unique[item.id] = item
        return {"evidence": list(unique.values())}

    async def generate_grounded_answer(state: ChatState) -> dict:
        """Stream a grounded model answer, or return a local clarification or no-evidence result."""
        if state["clarification"]:
            return {
                "answer": ChatGenerationLLMResponse(
                    response=state["clarification"],
                    suggestions=[],
                    citation_ids=[],
                    outcome="clarification_needed",
                )
            }
        if not state["evidence"]:
            return {
                "answer": ChatGenerationLLMResponse(
                    response="I could not find supporting evidence in the selected documents.",
                    suggestions=[],
                    citation_ids=[],
                    outcome="insufficient_evidence",
                )
            }
        answer = await llm.stream_structured_answer(
            [
                {
                    "role": "system",
                    "content": CHAT_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "question": state["question"],
                            "standalone_question": state["query"],
                            "conversation": state["history"],
                            "evidence": [item.model_dump() for item in state["evidence"]],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            on_delta,
        )
        return {"answer": answer}

    def validate_generated_answer(state: ChatState) -> dict:
        """Require valid sources for answered results and deduplicate citation IDs in order."""
        answer = state["answer"]
        if answer is None:
            raise ValueError("The generation stage did not produce an answer.")
        validate_citation_ids(answer.citation_ids, state["evidence"])
        if answer.outcome == "answered" and not answer.citation_ids:
            raise InvalidCitationError("An evidence-based answer must cite a source.")
        return {
            "answer": answer.model_copy(
                update={
                    "citation_ids": list(dict.fromkeys(answer.citation_ids)),
                }
            )
        }

    graph = StateGraph(ChatState)
    graph.add_node("input_query_rewrite", input_query_rewrite)
    graph.add_node("retrieve_relevant_chunks", retrieve_grounding_evidence)
    graph.add_node("generate", generate_grounded_answer)
    graph.add_node("validate", validate_generated_answer)
    graph.add_edge(START, "input_query_rewrite")
    graph.add_edge("input_query_rewrite", "retrieve_relevant_chunks")
    graph.add_edge("retrieve_relevant_chunks", "generate")
    graph.add_edge("generate", "validate")
    graph.add_edge("validate", END)
    result = await graph.compile().ainvoke(
        {
            "question": question,
            "history": turns,
            "query": question,
            "clarification": "",
            "evidence": [],
            "answer": None,
        }
    )
    answer = result.get("answer")
    if not isinstance(answer, ChatGenerationLLMResponse):
        raise ValueError("The chat workflow did not produce a validated answer.")
    return answer, result["evidence"], result["query"]
