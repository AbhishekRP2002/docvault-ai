"""Strict LLM task responses, cited evidence, and public comparison cells."""

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
        min_length=1,
        description="Direct user-facing answer or limitation, with exact evidence IDs beside claims.",
    )
    suggestions: list[str] = Field(
        max_length=3,
        description="Provide one to three distinct useful next questions based on this answer and conversation; empty only when no useful follow-up exists. Never assume unsupported facts.",
    )
    citation_ids: list[str] = Field(
        description="Distinct original evidence IDs actually used in response, in first-use order."
    )
    outcome: Literal["answered", "insufficient_evidence", "clarification_needed"] = Field(
        description="Whether the answer is supported, lacks evidence, or needs user clarification."
    )


class ConversationSummaryLLMResponse(StrictModel):
    """Condensed conversation memory for reference resolution, never document grounding."""

    summary: str = Field(
        min_length=1,
        description="Concise older-turn memory: user intent, named subjects, unresolved references and pending tasks; prior answers are unverified conversation, not evidence.",
    )


class CitedKeyInsight(StrictModel):
    """One grounded insight within the complete insights generation response."""

    insight_text: str = Field(
        description="Material document fact or qualification supported by this insight's own citations."
    )
    citation_ids: list[str] = Field(
        min_length=1,
        description="Original server-issued source IDs supporting this insight, not intermediate summaries.",
    )


class InsightsGenerationLLMResponse(StrictModel):
    """Document summary, classification, grounded insights, and suggested follow-ups."""

    summary: str = Field(
        min_length=1,
        description="Grounded summary of all supplied sections, retaining material conflicts and limits.",
    )
    category: str = Field(description="Document category inferred from the supplied content.")
    tags: list[str] = Field(max_length=8, description="Up to eight content-based document tags.")
    key_insights: list[CitedKeyInsight] = Field(
        max_length=8, description="Up to eight individually cited document insights."
    )
    suggestions: list[str] = Field(
        max_length=3, description="Zero to three useful follow-up questions about the document."
    )
    citation_ids: list[str] = Field(
        min_length=1,
        description="Distinct original server-issued source IDs supporting summary through reduction.",
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
        description="Grounded finding with material qualifications/conflicts, or a scoped missing-information notice."
    )
    status: Literal["found", "not_found"] = Field(
        description="found for supported facts including explicit negatives; not_found for missing evidence."
    )
    citation_ids: list[str] = Field(
        description="Original source IDs supporting finding_text; nonempty for found, empty for not_found."
    )
