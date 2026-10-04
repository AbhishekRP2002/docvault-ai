"""Provider-independent contracts for the LLM capabilities each workflow needs."""

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel

from docvault.llm.config import GenerationModelConfig, LLMTask
from docvault.llm.types import ChatGenerationLLMResponse

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)
DeltaCallback = Callable[[str], Awaitable[None]]


class StructuredGenerationLLM(Protocol):
    async def generate_structured_response(
        self, schema: type[ResponseModel], messages: list[dict], *, task: LLMTask
    ) -> ResponseModel:
        """Generate and validate a response using the requested task's schema."""
        ...


class ChatGenerationLLM(StructuredGenerationLLM, Protocol):
    async def stream_structured_answer(
        self, messages: list[dict], on_delta: DeltaCallback
    ) -> ChatGenerationLLMResponse:
        """Emit provisional answer text and return the fully validated chat response."""
        ...


class DocumentAnalysisLLM(StructuredGenerationLLM, Protocol):
    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Return the task's context capacity and output reservation for batching."""
        ...
