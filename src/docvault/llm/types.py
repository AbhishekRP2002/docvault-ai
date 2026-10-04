from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    id: str
    document_id: str
    version_id: str
    filename: str
    version_number: int
    text: str
    location: dict


class Answer(StrictModel):
    response: str = Field(min_length=1)
    suggestions: list[str] = Field(max_length=3)
    citation_ids: list[str]
    outcome: Literal["answered", "insufficient_evidence", "clarification_needed"]


class ParsedChunk(StrictModel):
    text: str
    embedding_text: str
    location: dict
    token_count: int


class ParsedDocument(StrictModel):
    chunks: list[ParsedChunk]
    text: str
    page_count: int | None
    parser: str


class RewrittenQuestion(StrictModel):
    question: str = Field(min_length=1)
    needs_clarification: bool
    clarification: str


class KeyInsight(StrictModel):
    text: str
    citation_ids: list[str] = Field(min_length=1)


class DocumentInsights(StrictModel):
    summary: str = Field(min_length=1)
    category: str
    tags: list[str] = Field(max_length=8)
    key_insights: list[KeyInsight] = Field(max_length=8)
    suggestions: list[str] = Field(max_length=3)
    citation_ids: list[str] = Field(min_length=1)


class ComparisonCell(StrictModel):
    version_id: str
    text: str
    status: Literal["found", "not_found"]
    citation_ids: list[str]


class DimensionFinding(StrictModel):
    text: str
    status: Literal["found", "not_found"]
    citation_ids: list[str]
