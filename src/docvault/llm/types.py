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
