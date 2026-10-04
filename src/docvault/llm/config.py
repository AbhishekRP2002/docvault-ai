"""Environment-backed model selection and capacities for each LLM task."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import ENV_FILE_SENTINEL, DotenvType

LLMTask = Literal["chat", "rewrite", "summary", "comparison"]


class GenerationModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def validate_output_reservation(self) -> "GenerationModelConfig":
        """Reject a model capacity that leaves no room for input after reserving output."""
        if self.context_tokens <= self.max_output_tokens:
            raise ValueError("Model context must exceed the output token reservation.")
        return self


class EmbeddingModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    dimensions: int = Field(gt=0)
    max_input_tokens: int = Field(default=8191, gt=0)
    max_batch_inputs: int = Field(default=64, gt=0)
    max_batch_tokens: int = Field(default=32000, gt=0)


class LLMSettings(BaseSettings):
    """Model defaults can be overridden independently through the local environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_chat_model: str = "openai/gpt-4.1-mini"
    # Existing environment names continue to configure the chat task only.
    openrouter_context_tokens: int = Field(default=128000, gt=0)
    openrouter_max_output_tokens: int = Field(default=4096, gt=0)
    openrouter_chat_temperature: float | None = None
    openrouter_rewrite_model: str = "openai/gpt-4.1-nano"
    openrouter_rewrite_context_tokens: int = Field(default=128000, gt=0)
    openrouter_rewrite_max_output_tokens: int = Field(default=1024, gt=0)
    openrouter_rewrite_temperature: float | None = None
    openrouter_summary_model: str = "openai/gpt-4.1-mini"
    openrouter_summary_context_tokens: int = Field(default=128000, gt=0)
    openrouter_summary_max_output_tokens: int = Field(default=4096, gt=0)
    openrouter_summary_temperature: float | None = None
    openrouter_comparison_model: str = "openai/gpt-4.1"
    openrouter_comparison_context_tokens: int = Field(default=128000, gt=0)
    openrouter_comparison_max_output_tokens: int = Field(default=4096, gt=0)
    openrouter_comparison_temperature: float | None = None
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    openrouter_embedding_max_input_tokens: int = Field(default=8191, gt=0)
    openrouter_embedding_max_batch_inputs: int = Field(default=64, gt=0)
    openrouter_embedding_max_batch_tokens: int = Field(default=32000, gt=0)

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the settings library's dotenv override while retaining normal validation."""
        super().__init__(_env_file=_env_file, **values)

    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Resolve one task's model and validated input/output capacities without provider I/O."""
        if task == "chat":
            return GenerationModelConfig(
                model=self.openrouter_chat_model,
                context_tokens=self.openrouter_context_tokens,
                max_output_tokens=self.openrouter_max_output_tokens,
                temperature=self.openrouter_chat_temperature,
            )
        return GenerationModelConfig(
            model=getattr(self, f"openrouter_{task}_model"),
            context_tokens=getattr(self, f"openrouter_{task}_context_tokens"),
            max_output_tokens=getattr(self, f"openrouter_{task}_max_output_tokens"),
            temperature=getattr(self, f"openrouter_{task}_temperature"),
        )

    def embedding_model(self) -> EmbeddingModelConfig:
        """Resolve the embedding model, vector dimensions and independent batch capacities."""
        return EmbeddingModelConfig(
            model=self.openrouter_embedding_model,
            dimensions=self.embedding_dimensions,
            max_input_tokens=self.openrouter_embedding_max_input_tokens,
            max_batch_inputs=self.openrouter_embedding_max_batch_inputs,
            max_batch_tokens=self.openrouter_embedding_max_batch_tokens,
        )
