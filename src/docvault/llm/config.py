"""Environment-backed model selection and capacities for each LLM task."""

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import ENV_FILE_SENTINEL, DotenvType

LLMTask = Literal["chat", "input_query_rewrite", "summary", "comparison"]


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


class EnvironmentSettings(BaseSettings):
    """Load settings from the environment or an explicitly selected dotenv file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Forward the dotenv override while retaining normal settings validation."""
        super().__init__(_env_file=_env_file, **values)


# Explicit subclass constructors keep _env_file visible to static type checkers;
# otherwise Pydantic's synthesized field-only signatures omit that option.
class OpenRouterSettings(EnvironmentSettings):
    """Connection settings shared by generation and embedding requests."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"


class ChatModelSettings(EnvironmentSettings):
    """Model selection and capacities for grounded chat answers."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    openrouter_chat_model: str = "openai/gpt-4.1-mini"
    # Retain the original environment names for the chat task.
    openrouter_context_tokens: int = Field(default=128000, gt=0)
    openrouter_max_output_tokens: int = Field(default=4096, gt=0)
    openrouter_chat_temperature: float | None = None

    def chat_model_configuration(self) -> GenerationModelConfig:
        """Build the validated, immutable configuration for chat generation."""
        return GenerationModelConfig(
            model=self.openrouter_chat_model,
            context_tokens=self.openrouter_context_tokens,
            max_output_tokens=self.openrouter_max_output_tokens,
            temperature=self.openrouter_chat_temperature,
        )


class InputQueryRewriteModelSettings(EnvironmentSettings):
    """Model selection and capacities for rewriting follow-up questions."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    openrouter_input_query_rewrite_model: str = Field(
        default="openai/gpt-4.1-nano",
        validation_alias=AliasChoices(
            "OPENROUTER_INPUT_QUERY_REWRITE_MODEL", "OPENROUTER_REWRITE_MODEL"
        ),
    )
    openrouter_input_query_rewrite_context_tokens: int = Field(
        default=128000,
        gt=0,
        validation_alias=AliasChoices(
            "OPENROUTER_INPUT_QUERY_REWRITE_CONTEXT_TOKENS", "OPENROUTER_REWRITE_CONTEXT_TOKENS"
        ),
    )
    openrouter_input_query_rewrite_max_output_tokens: int = Field(
        default=1024,
        gt=0,
        validation_alias=AliasChoices(
            "OPENROUTER_INPUT_QUERY_REWRITE_MAX_OUTPUT_TOKENS",
            "OPENROUTER_REWRITE_MAX_OUTPUT_TOKENS",
        ),
    )
    openrouter_input_query_rewrite_temperature: float | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "OPENROUTER_INPUT_QUERY_REWRITE_TEMPERATURE", "OPENROUTER_REWRITE_TEMPERATURE"
        ),
    )

    def input_query_rewrite_model_configuration(self) -> GenerationModelConfig:
        """Build the validated, immutable configuration for question rewriting."""
        return GenerationModelConfig(
            model=self.openrouter_input_query_rewrite_model,
            context_tokens=self.openrouter_input_query_rewrite_context_tokens,
            max_output_tokens=self.openrouter_input_query_rewrite_max_output_tokens,
            temperature=self.openrouter_input_query_rewrite_temperature,
        )


class SummaryModelSettings(EnvironmentSettings):
    """Model selection and capacities for summaries and cited document insights."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    openrouter_summary_model: str = "openai/gpt-4.1-mini"
    openrouter_summary_context_tokens: int = Field(default=128000, gt=0)
    openrouter_summary_max_output_tokens: int = Field(default=4096, gt=0)
    openrouter_summary_temperature: float | None = None

    def summary_model_configuration(self) -> GenerationModelConfig:
        """Build the validated, immutable configuration for summary generation."""
        return GenerationModelConfig(
            model=self.openrouter_summary_model,
            context_tokens=self.openrouter_summary_context_tokens,
            max_output_tokens=self.openrouter_summary_max_output_tokens,
            temperature=self.openrouter_summary_temperature,
        )


class ComparisonModelSettings(EnvironmentSettings):
    """Model selection and capacities for extracting comparison findings."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    openrouter_comparison_model: str = "openai/gpt-4.1"
    openrouter_comparison_context_tokens: int = Field(default=128000, gt=0)
    openrouter_comparison_max_output_tokens: int = Field(default=4096, gt=0)
    openrouter_comparison_temperature: float | None = None

    def comparison_model_configuration(self) -> GenerationModelConfig:
        """Build the validated, immutable configuration for document comparison."""
        return GenerationModelConfig(
            model=self.openrouter_comparison_model,
            context_tokens=self.openrouter_comparison_context_tokens,
            max_output_tokens=self.openrouter_comparison_max_output_tokens,
            temperature=self.openrouter_comparison_temperature,
        )


class EmbeddingModelSettings(EnvironmentSettings):
    """Embedding model, vector dimensions and independent batching capacities."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the dotenv override while preserving settings validation."""
        super().__init__(_env_file=_env_file, **values)

    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    openrouter_embedding_max_input_tokens: int = Field(default=8191, gt=0)
    openrouter_embedding_max_batch_inputs: int = Field(default=64, gt=0)
    openrouter_embedding_max_batch_tokens: int = Field(default=32000, gt=0)

    def embedding_model(self) -> EmbeddingModelConfig:
        """Build the validated, immutable embedding configuration."""
        return EmbeddingModelConfig(
            model=self.openrouter_embedding_model,
            dimensions=self.embedding_dimensions,
            max_input_tokens=self.openrouter_embedding_max_input_tokens,
            max_batch_inputs=self.openrouter_embedding_max_batch_inputs,
            max_batch_tokens=self.openrouter_embedding_max_batch_tokens,
        )


class LLMSettings(
    OpenRouterSettings,
    ChatModelSettings,
    InputQueryRewriteModelSettings,
    SummaryModelSettings,
    ComparisonModelSettings,
    EmbeddingModelSettings,
):
    """Aggregate task settings in one load, preserving existing environment names and callers."""

    def __init__(self, *, _env_file: DotenvType | None = ENV_FILE_SENTINEL, **values: Any) -> None:
        """Expose the aggregate's dotenv override to callers and static type checkers."""
        super().__init__(_env_file=_env_file, **values)

    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Resolve a task's configuration through its dedicated settings class."""
        configurations = {
            "chat": self.chat_model_configuration,
            "input_query_rewrite": self.input_query_rewrite_model_configuration,
            "summary": self.summary_model_configuration,
            "comparison": self.comparison_model_configuration,
        }
        return configurations[task]()
