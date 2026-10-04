"""Configured provider boundary and durable usage accounting."""

import asyncio

from docvault.config import get_settings
from docvault.db import session
from docvault.llm.provider import OpenRouterLLM
from docvault.models import LLMCall


def create_llm_client(resource_id: str | None = None) -> OpenRouterLLM:
    """Create the configured OpenRouter client with durable usage accounting for a resource."""
    settings = get_settings()

    def persist_llm_call(value: dict) -> None:
        """Persist one provider usage record associated with this client's resource."""
        with session() as db, db.begin():
            db.add(LLMCall(resource_id=resource_id, **value))

    async def record_llm_call_async(value: dict) -> None:
        """Offload synchronous usage persistence so it does not block the async provider loop."""
        await asyncio.to_thread(persist_llm_call, value)

    return OpenRouterLLM(
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_base_url,
        settings.openrouter_chat_model,
        settings.openrouter_embedding_model,
        settings.embedding_dimensions,
        record_llm_call_async,
        context_tokens=settings.openrouter_context_tokens,
        max_output_tokens=settings.openrouter_max_output_tokens,
    )
