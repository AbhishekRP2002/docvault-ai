"""Strict task responses, cited evidence, parser records, and public comparison cells."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Reject undeclared fields in records and structured model responses."""

    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    """Server-issued source passage and location supplied as grounding evidence."""

    id: str
    document_id: str
    version_id: str
    filename: str
    version_number: int
    text: str
    location: dict


class ChatGenerationLLMResponse(StrictModel):
    """Grounded chat reply, suggested follow-ups, source references, and answer outcome."""

    response: str = Field(
        min_length=1, description="User-facing answer with inline source markers."
    )
    suggestions: list[str] = Field(
        max_length=3, description="Zero to three useful follow-up questions for the user."
    )
    citation_ids: list[str] = Field(
        description="Server-issued evidence IDs referenced by the answer."
    )
    outcome: Literal["answered", "insufficient_evidence", "clarification_needed"] = Field(
        description="Whether the answer is supported, lacks evidence, or needs user clarification."
    )


class ParsedChunk(StrictModel):
    """Source text, contextualized embedding input, location, and token count for one chunk."""

    text: str
    embedding_text: str
    location: dict
    token_count: int


class ParsedDocument(StrictModel):
    """Complete parser result before chunk persistence or vector indexing."""

    chunks: list[ParsedChunk]
    text: str
    page_count: int | None
    parser: str


class QuestionRewriteLLMResponse(StrictModel):
    """Standalone retrieval question and any clarification needed to resolve references."""

    standalone_question: str = Field(
        min_length=1, description="Self-contained retrieval question resolved from conversation."
    )
    needs_clarification: bool = Field(
        description="True when conversation cannot resolve an ambiguous user reference."
    )
    clarification_question: str = Field(
        description="Question to ask the user when clarification is needed; otherwise empty."
    )


class CitedKeyInsight(StrictModel):
    """One grounded insight within the complete insights generation response."""

    insight_text: str = Field(
        description="Document fact or insight supported by the cited evidence."
    )
    citation_ids: list[str] = Field(
        min_length=1, description="Server-issued evidence IDs supporting this specific insight."
    )


class InsightsGenerationLLMResponse(StrictModel):
    """Document summary, classification, grounded insights, and suggested follow-ups."""

    summary: str = Field(min_length=1, description="Summary of all supplied source sections.")
    category: str = Field(description="Document category inferred from the supplied content.")
    tags: list[str] = Field(max_length=8, description="Up to eight content-based document tags.")
    key_insights: list[CitedKeyInsight] = Field(
        max_length=8, description="Up to eight individually cited document insights."
    )
    suggestions: list[str] = Field(
        max_length=3, description="Zero to three useful follow-up questions about the document."
    )
    citation_ids: list[str] = Field(
        min_length=1, description="Server-issued evidence IDs supporting the document summary."
    )


class DocumentComparisonCell(StrictModel):
    """Public comparison result for one version and dimension; assembled by the application."""

    version_id: str
    text: str
    status: Literal["found", "not_found"]
    citation_ids: list[str]


class ComparisonDimensionLLMResponse(StrictModel):
    """Cited finding for one requested comparison dimension across supplied source sections."""

    finding_text: str = Field(
        description="Finding for the requested dimension, or an absence notice."
    )
    status: Literal["found", "not_found"] = Field(
        description="Whether the supplied evidence supports a finding for this dimension."
    )
    citation_ids: list[str] = Field(
        description="Supporting server-issued evidence IDs; empty when status is not_found."
    )
