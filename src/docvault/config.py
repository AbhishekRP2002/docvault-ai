from functools import lru_cache
from pathlib import Path

from pydantic_settings import SettingsConfigDict

from docvault.llm.config import LLMSettings


class Settings(LLMSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://docvault:docvault@localhost:15432/docvault"
    redis_url: str = "redis://localhost:16379/0"
    storage_path: Path = Path("data")
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_upload_bytes: int = 25 * 1024 * 1024
    chat_rate_per_minute: int = 20
    upload_rate_per_hour: int = 20
    daily_budget_usd: float = 0
    job_timeout_seconds: int = 1800
    lease_seconds: int = 120
    generation_timeout_seconds: int = 600


@lru_cache
def get_settings() -> Settings:
    """Load and cache validated configuration from environment variables and the local .env."""
    return Settings()
