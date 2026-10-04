"""Check task prompt/schema contracts, not the quality of model-generated answers."""

import re

import pytest
from openai.lib._parsing._completions import type_to_response_format_param
from pydantic import BaseModel, ValidationError

from docvault.cache import calculate_json_fingerprint
from docvault.llm import prompts
from docvault.llm.config import GenerationModelConfig, LLMTask
from docvault.llm.models import (
    ChatGenerationLLMResponse,
    CitedKeyInsight,
    ComparisonDimensionLLMResponse,
    InputQueryRewriteLLMResponse,
    InsightsGenerationLLMResponse,
)

TASK_CONTRACTS: tuple[tuple[LLMTask, type[BaseModel], str, tuple[str, ...]], ...] = (
    (
        "input_query_rewrite",
        InputQueryRewriteLLMResponse,
        "INPUT_QUERY_REWRITE_SYSTEM_PROMPT",
        ("question", "conversation"),
    ),
    (
        "chat",
        ChatGenerationLLMResponse,
        "CHAT_SYSTEM_PROMPT",
        ("question", "standalone_question", "conversation", "evidence"),
    ),
    (
        "summary",
        InsightsGenerationLLMResponse,
        "SUMMARY_SYSTEM_PROMPT",
        ("sections", "length", "tone", "focus_areas"),
    ),
    (
        "comparison",
        ComparisonDimensionLLMResponse,
        "COMPARISON_SYSTEM_PROMPT",
        ("dimension", "sections"),
    ),
)


@pytest.mark.parametrize("task,schema,prompt_name,input_fields", TASK_CONTRACTS)
def test_generation_identity_connects_each_task_to_its_actual_prompt_and_schema(
    task, schema, prompt_name, input_fields
):
    """Bind cache reuse to the exact instructions and schema used by each generation task."""
    configuration = GenerationModelConfig(
        model="test/contract", context_tokens=12000, max_output_tokens=1000
    )
    identity = prompts.build_generation_identity(configuration, task)
    prompt = getattr(prompts, prompt_name)
    assert identity["task"] == task
    assert identity["prompt"] == prompt
    assert identity["schema"] == schema.model_json_schema()
    for field in set(input_fields) | set(schema.model_fields):
        assert re.search(rf"\b{re.escape(field)}\b", prompt), f"Undocumented task field: {field}"


@pytest.mark.parametrize("task,schema,prompt_name,input_fields", TASK_CONTRACTS)
def test_installed_sdk_derives_strict_schemas_with_required_described_fields(
    task, schema, prompt_name, input_fields
):
    """Exercise installed SDK schema conversion for all response contracts without HTTP calls."""
    response_format = type_to_response_format_param(schema)
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    specification = response_format.get("json_schema")
    assert isinstance(specification, dict)
    assert specification["name"] == schema.__name__
    assert specification.get("strict") is True
    contract = specification.get("schema")
    assert isinstance(contract, dict)
    assert contract["additionalProperties"] is False
    required = contract.get("required")
    assert isinstance(required, list) and set(required) == set(schema.model_fields)
    properties = contract.get("properties")
    assert isinstance(properties, dict)
    assert all(
        isinstance(field, dict) and field.get("description") for field in properties.values()
    )
    if task == "summary":
        definitions = contract.get("$defs")
        assert isinstance(definitions, dict)
        insight_contract = definitions.get("CitedKeyInsight")
        assert isinstance(insight_contract, dict)
        assert insight_contract["additionalProperties"] is False
        insight_required = insight_contract.get("required")
        assert isinstance(insight_required, list)
        assert set(insight_required) == set(CitedKeyInsight.model_fields)


@pytest.mark.parametrize("length,target", [("short", 100), ("medium", 250), ("long", 500)])
def test_summary_prompt_formats_length_without_changing_the_citation_contract(length, target):
    """Every supported length resolves its placeholder while retaining the same task fields."""
    prompt = prompts.build_summary_system_prompt(length)
    assert str(target) in prompt
    assert "{target_words}" not in prompt
    assert all(field in prompt for field in InsightsGenerationLLMResponse.model_fields)


def test_input_query_rewrite_prompt_edits_invalidate_only_that_task(monkeypatch):
    """Prevent cached resolutions from surviving edits to input-query rewrite instructions."""
    configuration = GenerationModelConfig(
        model="test/contract", context_tokens=12000, max_output_tokens=1000
    )
    original: dict[LLMTask, str] = {
        task: calculate_json_fingerprint(prompts.build_generation_identity(configuration, task))
        for task, _, _, _ in TASK_CONTRACTS
    }
    monkeypatch.setattr(
        prompts,
        "INPUT_QUERY_REWRITE_SYSTEM_PROMPT",
        prompts.INPUT_QUERY_REWRITE_SYSTEM_PROMPT + "\nResolve references conservatively.",
    )
    for task, fingerprint in original.items():
        changed = calculate_json_fingerprint(prompts.build_generation_identity(configuration, task))
        assert (changed != fingerprint) == (task == "input_query_rewrite")


def test_input_query_rewrite_schema_places_ambiguity_decision_before_resolved_query():
    """Keep decision-first generation guidance; property order does not guarantee model accuracy."""
    expected_order = ["needs_clarification", "clarification_question", "standalone_question"]
    response_format = type_to_response_format_param(InputQueryRewriteLLMResponse)
    assert isinstance(response_format, dict)
    specification = response_format.get("json_schema")
    assert isinstance(specification, dict)
    schema = specification.get("schema")
    assert isinstance(schema, dict)
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    assert list(properties) == expected_order
    assert schema.get("required") == expected_order


@pytest.mark.parametrize(
    "response",
    [
        ChatGenerationLLMResponse(
            response="The supplied evidence does not establish the requested price.",
            suggestions=[],
            citation_ids=[],
            outcome="insufficient_evidence",
        ),
        InputQueryRewriteLLMResponse(
            standalone_question="What is its notice period?",
            needs_clarification=True,
            clarification_question="Do you mean Acme or Atlas?",
        ),
        ComparisonDimensionLLMResponse(
            finding_text="The supplied sections do not specify a notice period.",
            status="not_found",
            citation_ids=[],
        ),
    ],
)
def test_response_contracts_allow_abstention_without_inventing_sources(response):
    """Keep unsupported and ambiguous outcomes expressible within the required strict schema."""
    assert type(response).model_validate(response.model_dump()) == response


@pytest.mark.parametrize("schema", [ChatGenerationLLMResponse, InsightsGenerationLLMResponse])
def test_user_facing_response_contracts_limit_follow_up_questions(schema):
    """Enforce the user-requested maximum of three suggestions at the schema boundary."""
    if schema is ChatGenerationLLMResponse:
        fields = {
            "response": "Supported answer.",
            "citation_ids": ["original-source"],
            "outcome": "answered",
        }
    else:
        fields = {
            "summary": "Supported summary.",
            "category": "Policy",
            "tags": [],
            "key_insights": [],
            "citation_ids": ["original-source"],
        }
    with pytest.raises(ValidationError) as failure:
        schema.model_validate({**fields, "suggestions": ["one", "two", "three", "four"]})
    assert any(error["loc"] == ("suggestions",) for error in failure.value.errors())
