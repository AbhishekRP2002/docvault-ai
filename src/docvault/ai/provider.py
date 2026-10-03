"""OpenRouter's OpenAI-compatible API, with strict output and real JSON deltas."""

import json
import math
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import tiktoken
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel

from docvault.ai.types import Answer

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


def strict_schema(schema: type[BaseModel]) -> dict:
    """Derive an OpenRouter strict response format from a Pydantic model's JSON schema."""
    result = schema.model_json_schema()

    def visit(value):
        """Remove defaults and require declared object fields recursively in the schema copy."""
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": True,
            "schema": result,
        },
    }


def response_prefix(raw: str) -> str:
    """Decode the complete characters available in the top-level response string.

    Key order, escaped quotes, incomplete escapes, and UTF-16 surrogate pairs are
    supported. Other fields are never exposed as answer text. Full JSON and
    schema validation still happen after the provider stream has ended.
    """
    decoder = json.JSONDecoder()
    offset = 0

    def whitespace(index):
        """Return the next non-whitespace offset in the current JSON buffer."""
        while index < len(raw) and raw[index].isspace():
            index += 1
        return index

    offset = whitespace(offset)
    if offset == len(raw) or raw[offset] != "{":
        return ""
    offset += 1
    while offset < len(raw):
        offset = whitespace(offset)
        try:
            key, offset = decoder.raw_decode(raw, offset)
        except ValueError:
            return ""
        offset = whitespace(offset)
        if offset == len(raw) or raw[offset] != ":":
            return ""
        offset = whitespace(offset + 1)
        if key == "response":
            if offset == len(raw) or raw[offset] != '"':
                return ""
            offset += 1
            output = []
            while offset < len(raw):
                char = raw[offset]
                if char == '"':
                    return "".join(output)
                if char != "\\":
                    if ord(char) < 32:
                        return "".join(output)
                    output.append(char)
                    offset += 1
                    continue
                end = offset + 2
                if end > len(raw):
                    break
                if raw[offset + 1] == "u":
                    end = offset + 6
                    if end > len(raw):
                        break
                    try:
                        value = int(raw[offset + 2 : end], 16)
                    except ValueError:
                        break
                    if 0xD800 <= value <= 0xDBFF:
                        end += 6
                        if end > len(raw):
                            break
                try:
                    decoded = json.loads('"' + raw[offset:end] + '"')
                except ValueError:
                    break
                if any(0xD800 <= ord(c) <= 0xDFFF for c in decoded):
                    break
                output.append(decoded)
                offset = end
            return "".join(output)
        try:
            _, offset = decoder.raw_decode(raw, offset)
        except ValueError:
            return ""
        offset = whitespace(offset)
        if offset == len(raw) or raw[offset] != ",":
            return ""
        offset += 1
    return ""


class OpenRouterAI:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        chat_model: str,
        embedding_model: str,
        embedding_dimensions: int = 1536,
        on_usage: UsageCallback | None = None,
        *,
        context_tokens: int = 128_000,
        max_output_tokens: int = 4096,
    ):
        """Configure an asynchronous client with explicit model capacities and no SDK retries.

        Reject a missing key or invalid output reservation before any API request.
        The optional usage callback receives accounting for attempted provider calls.
        """
        if not api_key:
            raise ProviderError("Set OPENROUTER_API_KEY to enable AI operations.")
        if context_tokens <= max_output_tokens or max_output_tokens < 1:
            raise ValueError("Model context must exceed the output token reservation.")
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=0,
            timeout=120.0,
        )
        self.chat_model = chat_model
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.context_tokens = context_tokens
        self.max_output_tokens = max_output_tokens
        self.on_usage = on_usage

    async def close(self) -> None:
        """Release the underlying asynchronous HTTP client's resources."""
        await self.client.close()

    def _parameters(self, schema: type[BaseModel], messages: list[dict]) -> dict:
        """Build strict-generation parameters and reject estimated model context overflow."""
        response_format = strict_schema(schema)
        # Include schema and a margin for message framing in the context check.
        estimate = token_count(json.dumps(messages, ensure_ascii=False))
        estimate += token_count(json.dumps(response_format)) + 128
        if estimate + self.max_output_tokens > self.context_tokens:
            raise ContextLimitError(
                "This request exceeds the configured model context window. "
                "Narrow the question or select a model with a larger context."
            )
        return {
            "model": self.chat_model,
            "messages": messages,
            "response_format": response_format,
            "max_tokens": self.max_output_tokens,
            "extra_body": {"provider": {"require_parameters": True}},
        }

    async def _usage(self, result: dict, operation: str, started: float, status: str) -> None:
        """Send normalized provider accounting and elapsed time to the optional async callback.

        Unreported usage remains unknown; embeddings have no output tokens.
        """
        if self.on_usage is None:
            return
        usage = result.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        await self.on_usage(
            {
                "model": result.get("model")
                or (self.embedding_model if operation == "embedding" else self.chat_model),
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
    def _error(exc: Exception) -> ProviderError:
        """Translate SDK failures into safe messages and classify connection/429/5xx retries."""
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return ProviderError("The AI provider could not be reached.", retryable=True)
        if isinstance(exc, APIStatusError):
            return ProviderError(
                f"The AI provider returned HTTP {exc.status_code}.",
                retryable=exc.status_code == 429 or exc.status_code >= 500,
            )
        return ProviderError("The AI provider returned an invalid response.")

    async def structured(self, schema: type[Schema], messages: list[dict]) -> Schema:
        """Request one schema-constrained completion and return the validated model instance.

        Report usage even on failure; refusals, incomplete output, provider errors,
        and schema validation failures propagate to the caller.
        """
        parameters = self._parameters(schema, messages)
        started, result, status = time.monotonic(), {}, "failed"
        try:
            completion = await self.client.chat.completions.create(**parameters)
            result = completion.model_dump()
            choice = completion.choices[0]
            if choice.finish_reason != "stop" or choice.message.refusal:
                raise ProviderError("The AI provider did not complete a structured answer.")
            value = schema.model_validate_json(choice.message.content or "")
            status = "succeeded"
            return value
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise self._error(exc) from exc
        finally:
            await self._usage(result, "generation", started, status)

    async def stream_answer(self, messages: list[dict], on_delta: DeltaCallback) -> Answer:
        """Emit provisional response-field deltas and return the fully validated answer.

        Reject refusals, truncated streams, and inconsistent text. Close the
        provider stream and report available usage on completion or failure.
        """
        parameters = self._parameters(Answer, messages)
        started, result, status = time.monotonic(), {}, "failed"
        raw, emitted, finish_reason = "", "", None
        stream = None
        try:
            stream = await self.client.chat.completions.create(**parameters, stream=True)
            async for chunk in stream:
                data = chunk.model_dump()
                result["id"] = data.get("id") or result.get("id")
                result["model"] = data.get("model") or result.get("model")
                if data.get("usage"):
                    result["usage"] = data["usage"]
                for choice in chunk.choices:
                    if choice.delta.refusal:
                        raise ProviderError("The AI provider declined this request.")
                    finish_reason = choice.finish_reason or finish_reason
                    raw += choice.delta.content or ""
                    prefix = response_prefix(raw)
                    if not prefix.startswith(emitted):
                        raise ProviderError("The AI provider returned inconsistent JSON text.")
                    if len(prefix) > len(emitted):
                        await on_delta(prefix[len(emitted) :])
                        emitted = prefix
            if finish_reason != "stop":
                raise ProviderError("The AI response ended before completion; retry the message.")
            answer = Answer.model_validate_json(raw)
            if emitted != answer.response:
                raise ProviderError("The AI provider returned inconsistent answer content.")
            status = "succeeded"
            return answer
        except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
            raise self._error(exc) from exc
        finally:
            if stream is not None:
                await stream.close()
            await self._usage(result, "generation", started, status)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ordered inputs in batches of at most 64 texts and 32,000 estimated tokens.

        Reject blank or oversized inputs and invalid response indices, dimensions,
        or nonfinite vector values. Record provider usage separately for each batch.
        """
        if not texts:
            return []
        batches, batch, batch_tokens = [], [], 0
        for value in texts:
            size = token_count(value)
            if not value.strip() or size > 8191:
                raise ContextLimitError("An embedding input is empty or exceeds 8,191 tokens.")
            if batch and (len(batch) == 64 or batch_tokens + size > 32_000):
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
                    model=self.embedding_model,
                    input=batch,
                    dimensions=self.embedding_dimensions,
                    encoding_format="float",
                    extra_body={"provider": {"require_parameters": True}},
                )
                result = completion.model_dump()
                entries = sorted(completion.data, key=lambda item: item.index)
                if [item.index for item in entries] != list(range(len(batch))):
                    raise ProviderError("The embedding provider returned incomplete inputs.")
                for item in entries:
                    if len(item.embedding) != self.embedding_dimensions or not all(
                        math.isfinite(value) for value in item.embedding
                    ):
                        raise ProviderError(
                            "The embedding provider returned invalid vector dimensions."
                        )
                    vectors.append(item.embedding)
                status = "succeeded"
            except (APIConnectionError, APIStatusError, APITimeoutError) as exc:
                raise self._error(exc) from exc
            finally:
                await self._usage(result, "embedding", started, status)
        return vectors
