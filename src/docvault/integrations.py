"""Configured provider boundary and durable usage accounting."""

import asyncio

from docvault.ai.provider import OpenRouterAI
from docvault.config import get_settings
from docvault.db import session
from docvault.models import AICall


def create_ai(resource_id: str | None = None) -> OpenRouterAI:
    settings = get_settings()

    def save_usage(value: dict) -> None:
        with session() as db, db.begin():
            db.add(AICall(resource_id=resource_id, **value))

    async def on_usage(value: dict) -> None:
        await asyncio.to_thread(save_usage, value)

    return OpenRouterAI(
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_base_url,
        settings.openrouter_chat_model,
        settings.openrouter_embedding_model,
        settings.embedding_dimensions,
        on_usage,
        context_tokens=settings.openrouter_context_tokens,
        max_output_tokens=settings.openrouter_max_output_tokens,
    )
