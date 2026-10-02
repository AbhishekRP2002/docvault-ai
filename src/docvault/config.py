from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://docvault:docvault@localhost:15432/docvault"
    redis_url: str = "redis://localhost:16379/0"
    storage_path: Path = Path("data")
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_chat_model: str = "openai/gpt-4.1-mini"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    openrouter_context_tokens: int = 128000
    openrouter_max_output_tokens: int = 4096
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_upload_bytes: int = 25 * 1024 * 1024
    chat_rate_per_minute: int = 20
    upload_rate_per_hour: int = 20
    daily_budget_usd: float = 0
    job_timeout_seconds: int = 1800
    lease_seconds: int = 120
    generation_timeout_seconds: int = 600
    embedding_dimensions: int = 1536


@lru_cache
def get_settings() -> Settings:
    return Settings()
