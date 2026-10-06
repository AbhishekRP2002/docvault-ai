"""OpenRouter's OpenAI-compatible API, with strict output and real JSON deltas."""

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
from docvault.tools.models import (
    AgentChatGenerationLLMResponse,
    AgentToolCall,
    ChatAgentTurn,
    build_grounded_agent_response_model,
)

Schema = TypeVar("Schema", bound=BaseModel)
UsageCallback = Callable[[dict], Awaitable[None]]
DeltaCallback = Callable[[str], Awaitable[None]]
MODEL_CONTEXT_CACHE: dict[str, tuple[float, dict[str, int]]] = {}


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
            for task in (
                "chat",
                "conversation_summary",
                "summary",
                "comparison",
            )
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

    async def resolve_model_context_tokens(self, task: LLMTask) -> int:
        """Use cached OpenRouter model metadata for planning; configured capacity is an outage fallback.

        Metadata requests have no generation usage. This estimate never rejects generation
        input: the selected provider remains authoritative for actual request acceptance.
        https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties
        """
        configuration = self.generation_model(task)
        cache_key, current = str(self.client.base_url), time.monotonic()
        cached = MODEL_CONTEXT_CACHE.get(cache_key)
        if cached is None or cached[0] <= current:
            capacities: dict[str, int] = {}
            lifetime = 3600
            try:
                models = await self.client.with_options(timeout=5.0).models.list()
                if not isinstance(models.data, list):
                    raise ValueError("Model metadata did not contain a model list.")
                for model in models.data:
                    metadata = model.model_dump()
                    capacity = metadata.get("context_length")
                    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
                        top_provider = metadata.get("top_provider")
                        capacity = (
                            top_provider.get("context_length")
                            if isinstance(top_provider, dict)
                            else None
                        )
                    if (
                        isinstance(capacity, int)
                        and not isinstance(capacity, bool)
                        and capacity > 0
                    ):
                        capacities[model.id] = capacity
            except (APIConnectionError, APIStatusError, APITimeoutError, ValueError):
                # Retry metadata later without blocking an otherwise valid generation.
                lifetime = 60
            MODEL_CONTEXT_CACHE[cache_key] = (current + lifetime, capacities)
        else:
            capacities = cached[1]
        return capacities.get(configuration.model, configuration.context_tokens)

    async def close(self) -> None:
        """Release the underlying asynchronous HTTP client's resources."""
        await self.client.close()

    def _build_generation_parameters(
        self, schema: type[BaseModel], messages: list[dict], task: LLMTask
    ) -> dict:
        """Select task settings and let the SDK derive the strict schema from the Pydantic model."""
        configuration = self.generation_model(task)
        parameters = {
            "model": configuration.model,
            "messages": messages,
            "response_format": schema,
            "max_tokens": configuration.max_output_tokens,
            "extra_body": {"provider": {"require_parameters": True}},
        }
        if configuration.temperature is not None:
            parameters["temperature"] = configuration.temperature
        if configuration.reasoning_effort is not None:
            parameters["extra_body"]["reasoning"] = {"effort": configuration.reasoning_effort}
        return parameters

    @staticmethod
    def _reasoning_fields(message: BaseModel) -> dict:
        """Echo provider reasoning unchanged so tool results can continue the same turn."""
        payload = message.model_dump(include={"reasoning", "reasoning_details"})
        return {
            field: payload[field]
            for field in ("reasoning", "reasoning_details")
            if payload.get(field) is not None
        }

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

    async def stream_chat_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: DeltaCallback,
        *,
        evidence_ids: list[str],
    ) -> ChatAgentTurn:
        """Stream one native chat-model turn: strict tool calls or a grounded structured answer.

        The installed SDK accumulates native calls, validates strict Pydantic arguments
        and parses the final answer schema. Provisional response text streams before
        completion; only a validated stop answer or a completed tool-call turn succeeds.
        """
        schema = build_grounded_agent_response_model(evidence_ids)
        parameters = self._build_generation_parameters(schema, messages, "chat")
        if tools:
            parameters.update(tools=tools, tool_choice="auto")
        started, result, status = time.monotonic(), {}, "failed"
        emitted, stream, operation = "", None, "generation"
        try:
            async with self.client.chat.completions.stream(**parameters) as stream:
                async for event in stream:
                    if event.type == "chunk":
                        result = self._capture_stream_accounting(event.snapshot, result)
                    elif event.type == "refusal.delta":
                        raise ProviderError("The LLM provider declined this request.")
                    elif event.type == "content.delta":
                        try:
                            partial = from_json(event.snapshot, allow_partial="trailing-strings")
                        except ValueError:
                            # JSON escapes/keys can be unfinished; final SDK validation is required.
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
                result = self._capture_stream_accounting(completion, result)
                if not completion.choices:
                    raise ProviderError("The LLM provider returned no completion.")
                choice = completion.choices[0]
                if choice.message.refusal:
                    raise ProviderError("The LLM provider declined this request.")
                if choice.finish_reason == "tool_calls":
                    operation = "tool_selection"
                    if choice.message.content or choice.message.parsed is not None or emitted:
                        raise ProviderError("The LLM provider mixed tool calls with an answer.")
                    calls, native_calls = [], []
                    ids: set[str] = set()
                    for call in choice.message.tool_calls or []:
                        if call.type != "function" or not isinstance(
                            call.function.parsed_arguments, BaseModel
                        ):
                            raise ProviderError(
                                "The LLM provider requested an unknown or invalid tool."
                            )
                        if not call.id or call.id in ids:
                            raise ProviderError(
                                "The LLM provider returned inconsistent tool call IDs."
                            )
                        ids.add(call.id)
                        calls.append(
                            AgentToolCall(
                                id=call.id,
                                name=call.function.name,
                                arguments=call.function.parsed_arguments.model_dump(),
                            )
                        )
                        native_calls.append(
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments,
                                },
                            }
                        )
                    if not calls:
                        raise ProviderError("The LLM provider returned no requested tools.")
                    status = "succeeded"
                    return ChatAgentTurn(
                        assistant_message={
                            "role": "assistant",
                            "content": None,
                            "tool_calls": native_calls,
                            **self._reasoning_fields(choice.message),
                        },
                        tool_calls=calls,
                    )
                if (
                    choice.finish_reason != "stop"
                    or choice.message.tool_calls
                    or choice.message.parsed is None
                ):
                    raise ProviderError(
                        "The LLM response ended before completion; retry the message."
                    )
                answer = AgentChatGenerationLLMResponse.model_validate(
                    choice.message.parsed.model_dump()
                )
                if emitted != answer.response:
                    raise ProviderError("The LLM provider returned inconsistent answer content.")
                status = "succeeded"
                return ChatAgentTurn(
                    assistant_message={
                        "role": "assistant",
                        "content": choice.message.content,
                        **self._reasoning_fields(choice.message),
                    },
                    tool_calls=[],
                    answer=answer,
                )
        except LengthFinishReasonError as exc:
            result = self._capture_stream_accounting(exc.completion, result)
            raise ProviderError(
                "The LLM response ended before completion; retry the message."
            ) from exc
        except ContentFilterFinishReasonError as exc:
            raise ProviderError("The LLM provider declined this request.") from exc
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise self._translate_provider_error(exc) from exc
        except ValueError as exc:
            raise ProviderError(
                "The LLM provider returned an invalid structured response or tool arguments."
            ) from exc
        finally:
            if stream is not None and result:
                snapshot = stream.current_completion_snapshot
                result = self._capture_stream_accounting(snapshot, result)
                if snapshot.choices and snapshot.choices[0].finish_reason == "tool_calls":
                    operation = "tool_selection"
            await self._record_provider_usage(
                result, operation, started, status, parameters["model"]
            )

    @staticmethod
    def _capture_stream_accounting(snapshot: BaseModel, previous: dict) -> dict:
        """Retain reported accounting when a later SDK snapshot has no usage-only payload."""
        result = snapshot.model_dump(exclude={"choices"})
        if result.get("usage") is None and previous.get("usage") is not None:
            result["usage"] = previous["usage"]
        return result

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
