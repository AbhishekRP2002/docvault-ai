"""Strict native tool inputs and internal agent records; scope is injected by the server."""

from types import GenericAlias
from typing import Literal, cast

from pydantic import Field, create_model

from docvault.llm.models import ChatGenerationLLMResponse, Evidence, StrictModel


class AgentChatGenerationLLMResponse(ChatGenerationLLMResponse):
    """Final chat reply with an internal grounding category; public response fields stay unchanged."""

    response_kind: Literal[
        "document_answer", "assistant_help", "workspace_answer", "clarification"
    ] = Field(
        description="Grounding category: original documents, capabilities, live tool state, or blocked intent."
    )


def build_grounded_agent_response_model(
    evidence_ids: list[str],
) -> type[AgentChatGenerationLLMResponse]:
    """Constrain native final-output citations to the server's actual evidence whitelist."""
    identifiers = tuple(dict.fromkeys(evidence_ids))
    if identifiers:
        citation_type = GenericAlias(list, getattr(Literal, "__getitem__")(identifiers))
        citation_field = Field(
            description="Only these original evidence IDs may support document claims."
        )
    else:
        citation_type = list[str]
        citation_field = Field(
            max_length=0,
            description="No original evidence supplied: return []. Tool/job/document IDs are not citations.",
        )
    return cast(
        type[AgentChatGenerationLLMResponse],
        create_model(
            "GroundedAgentChatGenerationLLMResponse",
            __base__=AgentChatGenerationLLMResponse,
            citation_ids=(citation_type, citation_field),
        ),
    )


class AgentToolCall(StrictModel):
    """One parsed native request whose ID must have exactly one matching tool result."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict


class ChatAgentTurn(StrictModel):
    """One native chat turn: either validated tool calls or a structured final answer."""

    assistant_message: dict
    tool_calls: list[AgentToolCall]
    answer: AgentChatGenerationLLMResponse | None = None


class ToolExecutionResult(StrictModel):
    """Scoped handler result and original evidence kept separately from model-visible JSON."""

    content: dict
    evidence: list[Evidence] = Field(default_factory=list)
    scope: Literal["document", "workspace", "operational"]
    query: str | None = None


class SelectedDocumentOverviewsToolInput(StrictModel):
    """Request a page of the server-selected documents' existing summaries and insights."""

    cursor: int | None = Field(
        description="Next cursor from an earlier result, or null to start.", ge=0
    )


class RelevantChunksToolInput(StrictModel):
    """The specific fact to search for within server-scoped selected sources."""

    query: str = Field(
        min_length=1, description="Standalone factual question with clear references resolved."
    )


class SearchDocumentsToolInput(StrictModel):
    """Topic discovery in the allowed library; results do not change selected chat sources."""

    query: str = Field(min_length=1, description="Topic or description of the documents to find.")
    cursor: int | None = Field(
        description="Next cursor from an earlier result, or null to start.", ge=0
    )


class DocumentProcessingMetricsToolInput(StrictModel):
    """Count current document revisions within the selected sources or allowed workspace."""

    scope: Literal["selected", "workspace"]


class DocumentProcessingDiagnosticsToolInput(StrictModel):
    """Inspect persisted stage errors and attempts without retrying or changing processing."""

    version_id: str = Field(
        min_length=1, description="Accessible version ID returned by the server."
    )


class DocumentAnalysisResultToolInput(StrictModel):
    """Read a previously issued summary/comparison artifact without creating a new job."""

    artifact_id: str = Field(
        min_length=1,
        description="Artifact ID provided by the user, saved analysis metadata, or an earlier result.",
    )


TOOL_INPUT_MODELS: dict[str, type[StrictModel]] = {
    "get_selected_document_overviews": SelectedDocumentOverviewsToolInput,
    "retrieve_relevant_chunks": RelevantChunksToolInput,
    "search_documents": SearchDocumentsToolInput,
    "get_document_processing_metrics": DocumentProcessingMetricsToolInput,
    "get_document_processing_diagnostics": DocumentProcessingDiagnosticsToolInput,
    "get_document_analysis_result": DocumentAnalysisResultToolInput,
}
