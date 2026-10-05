from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from pydantic_settings.sources import ENV_FILE_SENTINEL, DotenvType

from docvault.llm.config import LLMSettings


class Settings(LLMSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override to type checkers while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    database_url: str = "postgresql+psycopg://docvault:docvault@localhost:15432/docvault"
    redis_url: str = "redis://localhost:16379/0"
    storage_path: Path = Path("data")
    openrouter_chat_model: str = "openai/gpt-6-luna"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_upload_bytes: int = 25 * 1024 * 1024
    chat_rate_per_minute: int = 20
    upload_rate_per_hour: int = 20
    daily_budget_usd: float = 0
    job_timeout_seconds: int = 1800
    lease_seconds: int = 120
    generation_timeout_seconds: int = 600
    docling_num_threads: int = Field(default=4, gt=0)
    docling_parser_threads: int | None = Field(default=None, gt=0)
    docling_layout_batch_size: int = Field(default=4, gt=0)
    docling_ocr_batch_size: int = Field(default=4, gt=0)
    docling_table_batch_size: int = Field(default=4, gt=0)


@lru_cache
def get_settings() -> Settings:
    """Load and cache validated configuration from environment variables and the local .env."""
    return Settings()
