"""Task configuration and generation reuse identities without provider I/O."""

import json

import pytest
from pydantic import ValidationError

from docvault.cache import calculate_json_fingerprint
from docvault.llm import prompts
from docvault.llm.config import LLMSettings
from docvault.llm.types import ChatGenerationLLMResponse


@pytest.fixture
def clean_model_environment(monkeypatch):
    for name in LLMSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.mark.parametrize(
    "task,model",
    [
        ("chat", "openai/gpt-4.1-mini"),
        ("rewrite", "openai/gpt-4.1-nano"),
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
        "OPENROUTER_REWRITE_MODEL": "test/rewrite",
        "OPENROUTER_REWRITE_CONTEXT_TOKENS": "10000",
        "OPENROUTER_REWRITE_MAX_OUTPUT_TOKENS": "500",
        "OPENROUTER_REWRITE_TEMPERATURE": "0",
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
    for task, context, output, temperature in [
        ("chat", 50000, 2000, 0.1),
        ("rewrite", 10000, 500, 0),
        ("summary", 90000, 3000, 0.2),
        ("comparison", 120000, 4000, 0.3),
    ]:
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
    original = {
        task: prompts.build_generation_identity(settings.generation_model(task), task)
        for task in ("chat", "rewrite", "summary", "comparison")
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
    class ChatResponseWithConfidence(ChatGenerationLLMResponse):
        confidence: float

    configuration = LLMSettings(_env_file=None).generation_model("chat")
    original = prompts.build_generation_identity(configuration, "chat")
    monkeypatch.setattr(prompts, "ChatGenerationLLMResponse", ChatResponseWithConfidence)
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
    for task in ("chat", "rewrite", "summary", "comparison"):
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
        {"openrouter_summary_context_tokens": 1000, "openrouter_summary_max_output_tokens": 1000},
        {"openrouter_summary_temperature": 3},
        {"openrouter_summary_model": ""},
    ],
)
def test_invalid_summary_configuration_is_rejected(clean_model_environment, overrides):
    settings = LLMSettings(_env_file=None, **overrides)
    with pytest.raises(ValidationError):
        settings.generation_model("summary")
