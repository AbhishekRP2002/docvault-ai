"""Full-document summaries and comparisons with bounded map/reduce requests."""

import json

from docvault.llm.config import LLMTask
from docvault.llm.graphs import InvalidCitationError, validate_citation_ids
from docvault.llm.prompts import (
    COMPARISON_SYSTEM_PROMPT,
    SUMMARY_WORD_TARGETS,
    build_summary_system_prompt,
)
from docvault.llm.provider import ContextLimitError, OpenRouterLLM, token_count
from docvault.llm.types import (
    ComparisonDimensionLLMResponse,
    DocumentComparisonCell,
    Evidence,
    InsightsGenerationLLMResponse,
)


def _serialize_prompt_payload(value) -> str:
    """Encode prompt data as JSON while retaining readable Unicode characters."""
    return json.dumps(value, ensure_ascii=False)


def _calculate_section_token_capacity(llm: OpenRouterLLM, task: LLMTask) -> int:
    """Return section capacity after prompt/output reservations, rejecting unusable contexts."""
    # Capacity follows the configured provider model. This is not a file limit or
    # a product evidence budget. Leave room for instructions, schema and output.
    configuration = llm.generation_model(task)
    usable = configuration.context_tokens - configuration.max_output_tokens - 2048
    if usable < 512:
        raise ContextLimitError("The configured model context is too small for document analysis.")
    return usable


def _group_records_by_token_capacity(records: list[dict], capacity: int) -> list[list[dict]]:
    """Pack ordered records into estimated token budgets without dropping or truncating them.

    Raise ContextLimitError when a single record cannot fit in the given capacity.
    """
    groups, current, size = [], [], 2
    for record in records:
        record_size = token_count(_serialize_prompt_payload(record)) + 2
        if record_size > capacity:
            raise ContextLimitError(
                "A source section exceeds the model context; use a larger model context."
            )
        if current and size + record_size > capacity:
            groups.append(current)
            current, size = [], 2
        current.append(record)
        size += record_size
    if current:
        groups.append(current)
    return groups


def _build_source_records(chunks: list[Evidence]) -> list[dict]:
    """Convert evidence into prompt sections retaining original citation IDs and locations."""
    return [
        {
            "text": item.text,
            "citation_ids": [item.id],
            "filename": item.filename,
            "version_id": item.version_id,
            "location": item.location,
        }
        for item in chunks
    ]


def _collect_allowed_citation_ids(records: list[dict]) -> set[str]:
    """Collect source IDs permitted by the current batch of sections or reduced findings."""
    return {source for record in records for source in record["citation_ids"]}


def _validate_summary_citations(result: InsightsGenerationLLMResponse, available: set[str]) -> None:
    """Reject summary or insight citations that were not available to the generating call."""
    referenced = set(result.citation_ids)
    referenced.update(source for fact in result.key_insights for source in fact.citation_ids)
    if referenced - available:
        raise InvalidCitationError("Document insights referenced an unknown source.")


async def generate_document_summary(
    llm: OpenRouterLLM,
    chunks: list[Evidence],
    length: str = "short",
    focus_areas: list[str] | None = None,
    tone: str = "neutral",
) -> dict:
    """Summarize all supplied chunks through model-sized batches and recursive reduction.

    Validate customization and source IDs, then return insights with coverage and
    options metadata. Fail on invalid citations or reductions that do not shrink
    enough to fit; no document sections are silently discarded.
    """
    targets = SUMMARY_WORD_TARGETS
    if length not in targets or tone not in {"neutral", "executive", "plain_language"}:
        raise ValueError("Unsupported summary length or tone.")
    if not chunks:
        raise ValueError("A summary requires document evidence.")
    focus_areas = focus_areas or []
    if len(focus_areas) > 5 or any(not item.strip() for item in focus_areas):
        raise ValueError("Provide up to five nonempty focus areas.")
    records, capacity = (
        _build_source_records(chunks),
        _calculate_section_token_capacity(llm, "summary"),
    )
    instructions = build_summary_system_prompt(length)
    while True:
        groups = _group_records_by_token_capacity(records, capacity)
        summaries = []
        for group in groups:
            result = await llm.generate_structured_response(
                InsightsGenerationLLMResponse,
                [
                    {"role": "system", "content": instructions},
                    {
                        "role": "user",
                        "content": _serialize_prompt_payload(
                            {
                                "length": length,
                                "tone": tone,
                                "focus_areas": focus_areas,
                                "sections": group,
                            }
                        ),
                    },
                ],
                task="summary",
            )
            _validate_summary_citations(result, _collect_allowed_citation_ids(group))
            summaries.append(result)
        if len(summaries) == 1:
            final = summaries[0].model_dump()
            # Keep the stored/public insight shape stable at the output boundary.
            final["key_insights"] = [
                {"text": insight.insight_text, "citation_ids": insight.citation_ids}
                for insight in summaries[0].key_insights
            ]
            final["citation_ids"] = list(
                dict.fromkeys(
                    final["citation_ids"]
                    + [source for fact in final["key_insights"] for source in fact["citation_ids"]]
                )
            )
            final["coverage"] = {
                "chunks_processed": len(chunks),
                "total_chunks": len(chunks),
                "complete": True,
            }
            final["options"] = {"length": length, "focus_areas": focus_areas, "tone": tone}
            return final
        next_records = []
        for summary in summaries:
            value = summary.model_dump()
            value["citation_ids"] = list(
                dict.fromkeys(
                    value["citation_ids"]
                    + [source for fact in summary.key_insights for source in fact.citation_ids]
                )
            )
            next_records.append(value)
        if token_count(_serialize_prompt_payload(next_records)) >= token_count(
            _serialize_prompt_payload(records)
        ):
            raise ContextLimitError(
                "The model could not compact section summaries; use a larger model context."
            )
        records = next_records


async def _extract_comparison_dimension(
    llm: OpenRouterLLM,
    chunks: list[Evidence],
    dimension: str,
) -> ComparisonDimensionLLMResponse:
    """Extract one comparison dimension across all supplied sections and reduce findings.

    Return not_found for absent evidence. Supported findings retain original
    citations; invalid IDs and reductions that fail to shrink raise errors.
    """
    if not chunks:
        return ComparisonDimensionLLMResponse(
            finding_text="No supporting information was found.", status="not_found", citation_ids=[]
        )
    records, capacity = (
        _build_source_records(chunks),
        _calculate_section_token_capacity(llm, "comparison"),
    )
    while True:
        groups = _group_records_by_token_capacity(records, capacity)
        findings = []
        for group in groups:
            result = await llm.generate_structured_response(
                ComparisonDimensionLLMResponse,
                [
                    {
                        "role": "system",
                        "content": COMPARISON_SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": _serialize_prompt_payload(
                            {"dimension": dimension, "sections": group}
                        ),
                    },
                ],
                task="comparison",
            )
            if set(result.citation_ids) - _collect_allowed_citation_ids(group):
                raise InvalidCitationError("Comparison referenced an unknown source.")
            if result.status == "found" and not result.citation_ids:
                raise InvalidCitationError("A comparison finding must cite a source.")
            if result.status == "not_found" and result.citation_ids:
                raise InvalidCitationError(
                    "An absent comparison finding cannot cite supporting evidence."
                )
            findings.append(result)
        if len(findings) == 1:
            return findings[0]
        # Missing sections cannot overwrite supported findings during reduction.
        next_records = [finding.model_dump() for finding in findings if finding.status == "found"]
        if not next_records:
            return ComparisonDimensionLLMResponse(
                finding_text="No supporting information was found.",
                status="not_found",
                citation_ids=[],
            )
        if token_count(_serialize_prompt_payload(next_records)) >= token_count(
            _serialize_prompt_payload(records)
        ):
            raise ContextLimitError(
                "The comparison could not be compacted; use a larger model context."
            )
        records = next_records


async def generate_document_comparison(
    llm: OpenRouterLLM,
    evidence_by_version: dict[str, list[Evidence]],
    dimensions: list[str],
) -> dict:
    """Build a cited cell for each requested dimension and selected document version.

    Require at least two versions, nonempty dimensions, and correctly scoped
    evidence. Return rows with explicit missing-information cells and coverage.
    """
    if len(evidence_by_version) < 2:
        raise ValueError("Choose at least two document versions to compare.")
    if not dimensions or any(not dimension.strip() for dimension in dimensions):
        raise ValueError("At least one comparison dimension is required.")
    for version_id, evidence in evidence_by_version.items():
        if any(item.version_id != version_id for item in evidence):
            raise ValueError("Comparison evidence does not match its document version.")
    rows = []
    for dimension in dict.fromkeys(dimensions):
        cells = []
        for version_id, evidence in evidence_by_version.items():
            finding = await _extract_comparison_dimension(llm, evidence, dimension)
            validate_citation_ids(finding.citation_ids, evidence)
            cells.append(
                DocumentComparisonCell(
                    version_id=version_id,
                    text=finding.finding_text,
                    status=finding.status,
                    citation_ids=finding.citation_ids,
                ).model_dump()
            )
        rows.append({"dimension": dimension, "cells": cells})
    return {
        "dimensions": list(dict.fromkeys(dimensions)),
        "version_ids": list(evidence_by_version),
        "rows": rows,
        "coverage": {"version_count": len(evidence_by_version), "complete": True},
    }
