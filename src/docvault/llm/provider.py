"""OpenRouter's OpenAI-compatible API, with strict output and real JSON deltas."""

import json
import math
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import tiktoken
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
)
from pydantic import BaseModel
from pydantic_core import from_json

from docvault.llm.config import GenerationModelConfig, LLMSettings, LLMTask
from docvault.llm.types import ChatGenerationLLMResponse

Schema = TypeVar("Schema", bound=BaseModel)
UsageCallback = Callable[[dict], Awaitable[None]]
DeltaCallback = Callable[[str], Awaitable[None]]


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        """Attach a retry decision to a caller-safe provider error message."""
        super().__init__(message)
        self.retryable = retryable


class ContextLimitError(ProviderError):
    pass


def token_count(text: str) -> int:
    """Local estimate; the provider is authoritative for model-specific limits."""
    return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))


class OpenRouterLLM:
    def __init__(self, settings: LLMSettings, record_llm_call_async: UsageCallback | None = None):
        """Snapshot each task's validated model configuration and configure an async SDK client."""
        if not settings.openrouter_api_key.get_secret_value():
            raise ProviderError("Set OPENROUTER_API_KEY to enable LLM operations.")
        self.model_configurations = {
            task: settings.generation_model(task)
            for task in ("chat", "rewrite", "summary", "comparison")
        }
        self.embedding_configuration = settings.embedding_model()
        self.client = AsyncOpenAI(
            api_key=settings.openrouter_api_key.get_secret_value(),
            base_url=settings.openrouter_base_url,
            max_retries=0,
            timeout=120.0,
        )
        self.record_llm_call_async = record_llm_call_async

    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Return the immutable configuration selected for this client's task."""
        return self.model_configurations[task]

    async def close(self) -> None:
        """Release the underlying asynchronous HTTP client's resources."""
        await self.client.close()

    def _build_generation_parameters(
        self, schema: type[BaseModel], messages: list[dict], task: LLMTask
    ) -> dict:
        """Select task settings and let the SDK derive the strict schema from the Pydantic model."""
        configuration = self.generation_model(task)
        estimate = token_count(json.dumps(messages, ensure_ascii=False))
        estimate += token_count(json.dumps(schema.model_json_schema())) + 128
        if estimate + configuration.max_output_tokens > configuration.context_tokens:
            raise ContextLimitError(
                f"This request exceeds the configured {task} model context window. "
                "Narrow the question or select a model with a larger context."
            )
        parameters = {
            "model": configuration.model,
            "messages": messages,
            "response_format": schema,
            "max_tokens": configuration.max_output_tokens,
            "extra_body": {"provider": {"require_parameters": True}},
        }
        if configuration.temperature is not None:
            parameters["temperature"] = configuration.temperature
        return parameters

    async def _record_provider_usage(
        self, result: dict, operation: str, started: float, status: str, requested_model: str
    ) -> None:
        """Send normalized provider accounting and elapsed time to the optional async callback.

        Unreported usage remains unknown; embeddings have no output tokens.
        """
        if self.record_llm_call_async is None:
            return
        usage = result.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        await self.record_llm_call_async(
            {
                "model": result.get("model") or requested_model,
                "operation": operation,
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get(
                    "completion_tokens", 0 if operation == "embedding" else None
                ),
                "cached_tokens": details.get("cached_tokens"),
                "cost_usd": usage.get("cost"),
                "status": status,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "request_id": result.get("id"),
            }
        )

    @staticmethod
    def _translate_provider_error(exc: Exception) -> ProviderError:
        """Translate SDK failures into safe messages and classify connection/429/5xx retries."""
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return ProviderError("The LLM provider could not be reached.", retryable=True)
        if isinstance(exc, APIStatusError):
            return ProviderError(
                f"The LLM provider returned HTTP {exc.status_code}.",
                retryable=exc.status_code == 429 or exc.status_code >= 500,
            )
        return ProviderError("The LLM provider returned an invalid response.")

    async def generate_structured_response(
        self, schema: type[Schema], messages: list[dict], *, task: LLMTask
    ) -> Schema:
        """Return the SDK-parsed Pydantic result, rejecting refusals or incomplete output.

        OpenRouter receives the SDK-generated strict JSON schema. Usage is recorded
        for the selected task model even when parsing or provider requests fail.
        """
        parameters = self._build_generation_parameters(schema, messages, task)
        started, result, status = time.monotonic(), {}, "failed"
        try:
            response = await self.client.chat.completions.with_raw_response.parse(**parameters)
            # Capture provider accounting before SDK validation can reject the model content.
            result = response.http_response.json()
            completion = response.parse()
            if not completion.choices:
                raise ProviderError("The LLM provider returned no completion.")
            choice = completion.choices[0]
            if (
                choice.finish_reason != "stop"
                or choice.message.refusal
                or choice.message.parsed is None
            ):
                raise ProviderError("The LLM provider did not complete a structured answer.")
            status = "succeeded"
            return choice.message.parsed
        except LengthFinishReasonError as exc:
            result = exc.completion.model_dump(exclude={"choices"})
            raise ProviderError(
                "The LLM response ended before completion; retry the request."
            ) from exc
        except ContentFilterFinishReasonError as exc:
            raise ProviderError("The LLM provider declined this request.") from exc
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise self._translate_provider_error(exc) from exc
        except ValueError as exc:
            raise ProviderError(
                "The LLM provider returned an invalid structured response."
            ) from exc
        finally:
            await self._record_provider_usage(
                result, "generation", started, status, parameters["model"]
            )

    async def stream_structured_answer(
        self, messages: list[dict], on_delta: DeltaCallback
    ) -> ChatGenerationLLMResponse:
        """Stream provisional answer text with Pydantic's partial JSON parser and validate via SDK.

        The SDK owns schema conversion, accumulation, final validation and stream
        cleanup. Native partial parsing exposes unfinished response strings so the
        UI receives real text deltas before the full JSON object is complete.
        """
        parameters = self._build_generation_parameters(ChatGenerationLLMResponse, messages, "chat")
        started, result, status = time.monotonic(), {}, "failed"
        emitted, stream = "", None
        try:
            # OpenRouter includes usage automatically in the final SSE message.
            async with self.client.chat.completions.stream(**parameters) as stream:
                async for event in stream:
                    if event.type == "chunk":
                        result = event.snapshot.model_dump(exclude={"choices"})
                    elif event.type == "refusal.delta":
                        raise ProviderError("The LLM provider declined this request.")
                    elif event.type == "content.delta":
                        try:
                            partial = from_json(event.snapshot, allow_partial="trailing-strings")
                        except ValueError:
                            # A JSON key/escape may still be incomplete; final SDK validation is mandatory.
                            continue
                        prefix = partial.get("response", "") if isinstance(partial, dict) else ""
                        if not isinstance(prefix, str) or not prefix.startswith(emitted):
                            raise ProviderError(
                                "The LLM provider returned inconsistent answer content."
                            )
                        if len(prefix) > len(emitted):
                            await on_delta(prefix[len(emitted) :])
                            emitted = prefix
                if not result:
                    raise ProviderError("The LLM provider returned no completion.")
                completion = await stream.get_final_completion()
                result = completion.model_dump(exclude={"choices"})
                if not completion.choices:
                    raise ProviderError("The LLM provider returned no completion.")
                choice = completion.choices[0]
                answer = choice.message.parsed
                if choice.finish_reason != "stop" or choice.message.refusal or answer is None:
                    raise ProviderError(
                        "The LLM response ended before completion; retry the message."
                    )
                if emitted != answer.response:
                    raise ProviderError("The LLM provider returned inconsistent answer content.")
                status = "succeeded"
                return answer
        except LengthFinishReasonError as exc:
            result = exc.completion.model_dump(exclude={"choices"})
            raise ProviderError(
                "The LLM response ended before completion; retry the message."
            ) from exc
        except ContentFilterFinishReasonError as exc:
            raise ProviderError("The LLM provider declined this request.") from exc
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise self._translate_provider_error(exc) from exc
        except ValueError as exc:
            raise ProviderError(
                "The LLM provider returned an invalid structured response."
            ) from exc
        finally:
            if stream is not None and result:
                result = stream.current_completion_snapshot.model_dump(exclude={"choices"})
            await self._record_provider_usage(
                result, "generation", started, status, parameters["model"]
            )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed ordered inputs using the embedding task's configured batch/token capacities.

        Reject blank or oversized inputs and invalid response indices, dimensions,
        or nonfinite vector values. Record provider usage separately for each batch.
        """
        configuration = self.embedding_configuration
        if not texts:
            return []
        batches, batch, batch_tokens = [], [], 0
        for value in texts:
            size = token_count(value)
            capacity = min(configuration.max_input_tokens, configuration.max_batch_tokens)
            if not value.strip() or size > capacity:
                raise ContextLimitError(
                    f"An embedding input is empty or exceeds {capacity:,} tokens."
                )
            if batch and (
                len(batch) == configuration.max_batch_inputs
                or batch_tokens + size > configuration.max_batch_tokens
            ):
                batches.append(batch)
                batch, batch_tokens = [], 0
            batch.append(value)
            batch_tokens += size
        if batch:
            batches.append(batch)
        vectors = []
        for batch in batches:
            started, result, status = time.monotonic(), {}, "failed"
            try:
                completion = await self.client.embeddings.create(
                    model=configuration.model,
                    input=batch,
                    dimensions=configuration.dimensions,
                    encoding_format="float",
                    extra_body={"provider": {"require_parameters": True}},
                )
                result = completion.model_dump()
                entries = sorted(completion.data, key=lambda item: item.index)
                if [item.index for item in entries] != list(range(len(batch))):
                    raise ProviderError("The embedding provider returned incomplete inputs.")
                for item in entries:
                    if len(item.embedding) != configuration.dimensions or not all(
                        math.isfinite(value) for value in item.embedding
                    ):
                        raise ProviderError(
                            "The embedding provider returned invalid vector dimensions."
                        )
                    vectors.append(item.embedding)
                status = "succeeded"
            except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
                raise self._translate_provider_error(exc) from exc
            finally:
                await self._record_provider_usage(
                    result, "embedding", started, status, configuration.model
                )
        return vectors
