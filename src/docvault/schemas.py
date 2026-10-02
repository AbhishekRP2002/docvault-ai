from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatCreate(InputModel):
    title: str = Field(default="New chat", min_length=1, max_length=200)
    version_ids: list[str] = Field(min_length=1)


class ChatUpdate(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    version_ids: list[str] | None = Field(default=None, min_length=1)


class MessageCreate(InputModel):
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("Enter a question.")
        return value.strip()


class SummaryCreate(InputModel):
    length: Literal["short", "medium", "long"] = "short"
    focus_areas: list[str] = Field(default_factory=list, max_length=5)
    tone: Literal["neutral", "executive", "plain_language"] = "neutral"


class ComparisonCreate(InputModel):
    version_ids: list[str] = Field(min_length=2)
    dimensions: list[str] = Field(
        default_factory=lambda: ["Key terms", "Differences", "Risks"], min_length=1, max_length=5
    )
