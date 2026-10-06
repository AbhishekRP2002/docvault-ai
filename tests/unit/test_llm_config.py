"""Task configuration and generation reuse identities without provider I/O."""

import json

import pytest
from pydantic import ValidationError

from docvault.cache import calculate_json_fingerprint
from docvault.config import Settings
from docvault.llm import prompts
from docvault.llm.config import (
    ChatModelSettings,
    ComparisonModelSettings,
    ConversationSummaryModelSettings,
    EmbeddingModelSettings,
    LLMSettings,
    LLMTask,
    OpenRouterSettings,
    SummaryModelSettings,
)
from docvault.tools.models import AgentChatGenerationLLMResponse


@pytest.fixture
def clean_model_environment(monkeypatch):
    for name in LLMSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_obsolete_agent_and_rewrite_settings_are_ignored(clean_model_environment, tmp_path):
    """An existing dotenv cannot revive separate agent/rewrite models or their validation."""
    dotenv = tmp_path / "legacy.env"
    dotenv.write_text(
        "OPENROUTER_CHAT_MODEL=test/current-chat\n"
        "OPENROUTER_AGENT_MODEL=test/obsolete-agent\n"
        "OPENROUTER_AGENT_CONTEXT_TOKENS=not-an-integer\n"
        "OPENROUTER_INPUT_QUERY_REWRITE_MODEL=test/obsolete-rewrite\n"
        "OPENROUTER_INPUT_QUERY_REWRITE_MAX_OUTPUT_TOKENS=not-an-integer\n"
        "OPENROUTER_REWRITE_CONTEXT_TOKENS=not-an-integer\n"
    )
    settings = LLMSettings(_env_file=dotenv)
    assert settings.generation_model("chat").model == "test/current-chat"
    assert set(settings.model_dump()).isdisjoint(
        {
            "openrouter_agent_model",
            "openrouter_agent_context_tokens",
            "openrouter_input_query_rewrite_model",
            "openrouter_input_query_rewrite_max_output_tokens",
            "openrouter_rewrite_context_tokens",
        }
    )


def test_chat_settings_own_the_existing_tool_round_environment_name(
    clean_model_environment, monkeypatch
):
    """Moving tool selection into chat retains the existing bounded-round control."""
    assert ChatModelSettings(_env_file=None).agent_max_tool_rounds == 6
    monkeypatch.setenv("AGENT_MAX_TOOL_ROUNDS", "4")
    assert ChatModelSettings(_env_file=None).agent_max_tool_rounds == 4
    assert LLMSettings(_env_file=None).agent_max_tool_rounds == 4
    monkeypatch.setenv("AGENT_MAX_TOOL_ROUNDS", "0")
    with pytest.raises(ValidationError):
        ChatModelSettings(_env_file=None)


@pytest.mark.parametrize(
    "settings_class,environment_name,configuration_method",
    [
        (ChatModelSettings, "OPENROUTER_CHAT_MODEL", "chat_model_configuration"),
        (
            ConversationSummaryModelSettings,
            "OPENROUTER_CONVERSATION_SUMMARY_MODEL",
            "conversation_summary_model_configuration",
        ),
        (SummaryModelSettings, "OPENROUTER_SUMMARY_MODEL", "summary_model_configuration"),
        (
            ComparisonModelSettings,
            "OPENROUTER_COMPARISON_MODEL",
            "comparison_model_configuration",
        ),
        (EmbeddingModelSettings, "OPENROUTER_EMBEDDING_MODEL", "embedding_model"),
    ],
)
def test_task_settings_can_load_independently(
    clean_model_environment, tmp_path, settings_class, environment_name, configuration_method
):
    """Each task reads its own dotenv fields without validating unrelated app settings."""
    dotenv = tmp_path / "task.env"
    dotenv.write_text(
        f"{environment_name}=test/independent-model\n"
        "OPENROUTER_API_KEY=unit-test-placeholder\n"
        "DATABASE_URL=not-a-database\n"
        "MAX_UPLOAD_BYTES=not-an-integer\n"
    )
    settings = settings_class(_env_file=dotenv)
    assert getattr(settings, configuration_method)().model == "test/independent-model"
    assert "openrouter_api_key" not in settings.model_dump()
    assert "database_url" not in settings.model_dump()


def test_task_settings_own_disjoint_fields_and_aggregate_preserves_them():
    """The aggregate exposes all existing fields without sharing ownership between tasks."""
    settings_classes = (
        OpenRouterSettings,
        ChatModelSettings,
        ConversationSummaryModelSettings,
        SummaryModelSettings,
        ComparisonModelSettings,
        EmbeddingModelSettings,
    )
    fields = [name for settings_class in settings_classes for name in settings_class.model_fields]
    assert len(fields) == len(set(fields))
    assert set(LLMSettings.model_fields) == set(fields)


def test_application_settings_retains_task_overrides_and_validated_profiles(
    clean_model_environment, monkeypatch
):
    """Existing app-level defaults and environment overrides survive the settings split."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setenv("OPENROUTER_CHAT_MODEL", "test/application-chat")
    monkeypatch.setenv("OPENROUTER_COMPARISON_MODEL", "test/application-comparison")
    settings = Settings()
    assert settings.generation_model("chat").model == "test/application-chat"
    assert settings.generation_model("comparison").model == "test/application-comparison"
    assert settings.embedding_model() == EmbeddingModelSettings(_env_file=None).embedding_model()
    monkeypatch.delenv("OPENROUTER_CHAT_MODEL")
    assert (
        Settings().generation_model("chat").model
        == Settings.model_fields["openrouter_chat_model"].default
    )


def test_explicit_dotenv_load_and_environment_precedence(
    clean_model_environment, monkeypatch, tmp_path
):
    """An explicit dotenv supplies defaults while process environment retains precedence."""
    dotenv = tmp_path / "models.env"
    dotenv.write_text(
        "OPENROUTER_SUMMARY_MODEL=test/dotenv-summary\n"
        "OPENROUTER_CONVERSATION_SUMMARY_MODEL=test/dotenv-memory\n"
    )
    monkeypatch.setenv("OPENROUTER_SUMMARY_MODEL", "test/environment-summary")
    settings = LLMSettings(_env_file=dotenv)
    assert settings.generation_model("summary").model == "test/environment-summary"
    assert settings.generation_model("conversation_summary").model == "test/dotenv-memory"


def test_default_dotenv_is_respected_and_none_disables_it(
    clean_model_environment, monkeypatch, tmp_path
):
    """The constructor sentinel uses model_config; explicit None disables that dotenv."""
    dotenv = tmp_path / "models.env"
    dotenv.write_text("OPENROUTER_SUMMARY_MODEL=test/default-dotenv\n")
    monkeypatch.setitem(LLMSettings.model_config, "env_file", dotenv)
    assert LLMSettings().generation_model("summary").model == "test/default-dotenv"
    assert LLMSettings(_env_file=None).generation_model("summary").model == "openai/gpt-4.1-mini"


def test_dotenv_values_remain_validated(clean_model_environment, tmp_path):
    """Constructor forwarding still rejects invalid typed values from a dotenv."""
    dotenv = tmp_path / "models.env"
    dotenv.write_text("OPENROUTER_SUMMARY_CONTEXT_TOKENS=not-an-integer\n")
    with pytest.raises(ValidationError, match="openrouter_summary_context_tokens"):
        LLMSettings(_env_file=dotenv)


@pytest.mark.parametrize(
    "task,model",
    [
        ("chat", "openai/gpt-4.1-mini"),
        ("conversation_summary", "openai/gpt-4.1-mini"),
        ("summary", "openai/gpt-4.1-mini"),
        ("comparison", "openai/gpt-4.1"),
    ],
)
def test_default_generation_models_are_selected_by_task(clean_model_environment, task, model):
    settings = LLMSettings(_env_file=None)
    configuration = settings.generation_model(task)
    assert configuration.model == model
    assert configuration.context_tokens == 128000
    assert settings.embedding_model().model == "openai/text-embedding-3-small"
    assert settings.embedding_model().dimensions == 1536


def test_environment_overrides_are_independent_for_each_task(clean_model_environment, monkeypatch):
    overrides = {
        "OPENROUTER_CHAT_MODEL": "test/chat",
        "OPENROUTER_CONTEXT_TOKENS": "50000",
        "OPENROUTER_MAX_OUTPUT_TOKENS": "2000",
        "OPENROUTER_CHAT_TEMPERATURE": "0.1",
        "OPENROUTER_CONVERSATION_SUMMARY_MODEL": "test/conversation_summary",
        "OPENROUTER_CONVERSATION_SUMMARY_CONTEXT_TOKENS": "30000",
        "OPENROUTER_CONVERSATION_SUMMARY_MAX_OUTPUT_TOKENS": "1000",
        "OPENROUTER_CONVERSATION_SUMMARY_TEMPERATURE": "0",
        "OPENROUTER_SUMMARY_MODEL": "test/summary",
        "OPENROUTER_SUMMARY_CONTEXT_TOKENS": "90000",
        "OPENROUTER_SUMMARY_MAX_OUTPUT_TOKENS": "3000",
        "OPENROUTER_SUMMARY_TEMPERATURE": "0.2",
        "OPENROUTER_COMPARISON_MODEL": "test/comparison",
        "OPENROUTER_COMPARISON_CONTEXT_TOKENS": "120000",
        "OPENROUTER_COMPARISON_MAX_OUTPUT_TOKENS": "4000",
        "OPENROUTER_COMPARISON_TEMPERATURE": "0.3",
        "OPENROUTER_EMBEDDING_MODEL": "test/embedding",
        "EMBEDDING_DIMENSIONS": "1024",
        "OPENROUTER_EMBEDDING_MAX_INPUT_TOKENS": "4000",
        "OPENROUTER_EMBEDDING_MAX_BATCH_INPUTS": "16",
        "OPENROUTER_EMBEDDING_MAX_BATCH_TOKENS": "12000",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    settings = LLMSettings(_env_file=None)
    configurations: list[tuple[LLMTask, int, int, float]] = [
        ("chat", 50000, 2000, 0.1),
        ("conversation_summary", 30000, 1000, 0),
        ("summary", 90000, 3000, 0.2),
        ("comparison", 120000, 4000, 0.3),
    ]
    for task, context, output, temperature in configurations:
        assert settings.generation_model(task).model_dump() == {
            "model": f"test/{task}",
            "context_tokens": context,
            "max_output_tokens": output,
            "temperature": temperature,
        }
    assert settings.embedding_model().model_dump() == {
        "model": "test/embedding",
        "dimensions": 1024,
        "max_input_tokens": 4000,
        "max_batch_inputs": 16,
        "max_batch_tokens": 12000,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "test/new-model"),
        ("context_tokens", 64000),
        ("max_output_tokens", 2048),
        ("temperature", 0.5),
    ],
)
def test_each_generation_setting_changes_reuse_identity(clean_model_environment, field, value):
    configuration = LLMSettings(_env_file=None).generation_model("summary")
    original = prompts.build_generation_identity(configuration, "summary")
    changed = prompts.build_generation_identity(
        configuration.model_copy(update={field: value}), "summary"
    )
    assert calculate_json_fingerprint(original) != calculate_json_fingerprint(changed)


def test_prompt_changes_invalidate_only_the_matching_task(clean_model_environment, monkeypatch):
    settings = LLMSettings(_env_file=None)
    tasks: tuple[LLMTask, ...] = ("chat", "conversation_summary", "summary", "comparison")
    original: dict[LLMTask, dict] = {
        task: prompts.build_generation_identity(settings.generation_model(task), task)
        for task in tasks
    }
    monkeypatch.setattr(
        prompts, "CHAT_SYSTEM_PROMPT", prompts.CHAT_SYSTEM_PROMPT + " New instructions."
    )
    for task, identity in original.items():
        changed = prompts.build_generation_identity(settings.generation_model(task), task)
        assert (calculate_json_fingerprint(identity) != calculate_json_fingerprint(changed)) == (
            task == "chat"
        )


def test_response_schema_changes_invalidate_generation_identity(
    clean_model_environment, monkeypatch
):
    class ChatResponseWithConfidence(AgentChatGenerationLLMResponse):
        confidence: float

    configuration = LLMSettings(_env_file=None).generation_model("chat")
    original = prompts.build_generation_identity(configuration, "chat")
    monkeypatch.setattr(prompts, "AgentChatGenerationLLMResponse", ChatResponseWithConfidence)
    changed = prompts.build_generation_identity(configuration, "chat")
    assert calculate_json_fingerprint(original) != calculate_json_fingerprint(changed)
    assert "confidence" in changed["schema"]["properties"]


def test_summary_word_targets_invalidate_summary_reuse(clean_model_environment, monkeypatch):
    settings = LLMSettings(_env_file=None)
    original = prompts.build_generation_identity(settings.generation_model("summary"), "summary")
    chat_identity = prompts.build_generation_identity(settings.generation_model("chat"), "chat")
    monkeypatch.setattr(
        prompts, "SUMMARY_WORD_TARGETS", {**prompts.SUMMARY_WORD_TARGETS, "short": 120}
    )
    changed = prompts.build_generation_identity(settings.generation_model("summary"), "summary")
    assert calculate_json_fingerprint(original) != calculate_json_fingerprint(changed)
    assert (
        prompts.build_generation_identity(settings.generation_model("chat"), "chat")
        == chat_identity
    )
    assert "120 words" in prompts.build_summary_system_prompt("short")


def test_model_override_does_not_change_unrelated_tasks(clean_model_environment, monkeypatch):
    original = LLMSettings(_env_file=None)
    monkeypatch.setenv("OPENROUTER_SUMMARY_MODEL", "test/summary-override")
    changed = LLMSettings(_env_file=None)
    for task in ("chat", "conversation_summary", "summary", "comparison"):
        original_identity = prompts.build_generation_identity(original.generation_model(task), task)
        changed_identity = prompts.build_generation_identity(changed.generation_model(task), task)
        assert (original_identity != changed_identity) == (task == "summary")
    assert original.embedding_model() == changed.embedding_model()


def test_generation_identity_contains_no_credentials(clean_model_environment):
    settings = LLMSettings(_env_file=None, openrouter_api_key="unit-test-placeholder")
    identity = prompts.build_generation_identity(settings.generation_model("chat"), "chat")
    assert "unit-test-placeholder" not in json.dumps(identity)
    assert "openrouter_api_key" not in json.dumps(identity)


@pytest.mark.parametrize(
    "overrides",
    [
        {"openrouter_summary_temperature": 3},
        {"openrouter_summary_model": ""},
    ],
)
def test_invalid_summary_configuration_is_rejected(clean_model_environment, overrides):
    settings = LLMSettings(_env_file=None, **overrides)
    with pytest.raises(ValidationError):
        settings.generation_model("summary")
