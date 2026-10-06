"""Exercise scoped tools against real migrated PostgreSQL, without provider requests."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from support import require_persisted_row
from test_hybrid_retrieval import QUERY_VECTOR, chunk_row, seed_version
from test_hybrid_retrieval import isolated_database as isolated_database
from test_hybrid_retrieval import migrated_database as migrated_database

from docvault import db as database
from docvault.llm.config import GenerationModelConfig, LLMTask
from docvault.llm.provider import OpenRouterLLM
from docvault.models import Artifact, Chunk, Document, Job, Version, now
from docvault.tools.runtime import DocumentToolRuntime

pytestmark = pytest.mark.integration


class ToolLLM(OpenRouterLLM):
    """Deterministic tool accounting stub; never initialize or contact a provider."""

    def __init__(self, context_tokens: int = 128000):
        """Record embedding inputs and expose a controllable native context capacity."""
        self.inputs = []
        self.context_tokens = context_tokens

    def generation_model(self, task: LLMTask) -> GenerationModelConfig:
        """Return the deterministic model capacity for tool pagination."""
        return GenerationModelConfig(
            model="test/tool", context_tokens=self.context_tokens, max_output_tokens=128
        )

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Record calls and return a deterministic vector for native search queries."""
        self.inputs.extend(texts)
        return [QUERY_VECTOR for _ in texts]


def seed_overview_source(
    filename: str = "harbor.txt",
    content: str = "Annual plan USD 1200; cancellation requires 30 days notice.",
) -> tuple[str, str]:
    """Create real original evidence and a derived insight referencing that original ID."""
    identifier = seed_version(filename)
    with database.session() as db, db.begin():
        row = chunk_row(identifier, 0, QUERY_VECTOR, content)
        db.add(Chunk(**row))
        version = require_persisted_row(db, Version, identifier)
        version.chunk_count = 1
        version.insight_status = "ready"
        version.insights = {
            "summary": content,
            "category": "Policy",
            "tags": ["renewal"],
            "key_insights": [
                {"text": "Cancellation requires notice.", "citation_ids": [row["id"]]}
            ],
            "citation_ids": [row["id"]],
            "coverage": {"complete": True, "chunks_processed": 1, "total_chunks": 1},
        }
        return identifier, str(row["id"])


def run_tool(runtime: DocumentToolRuntime, name: str, arguments: dict):
    """Execute the real async validation boundary and return its structured result."""
    return asyncio.run(runtime.execute(name, arguments))


def test_overview_bypasses_search_and_resolves_original_citations(migrated_database, monkeypatch):
    """Generic prompts read complete stored overviews and cannot invent derived source IDs."""
    selected, chunk_id = seed_overview_source()
    other, _ = seed_overview_source("unselected.txt")
    llm = ToolLLM()
    runtime = DocumentToolRuntime([selected], str(uuid4()), llm)
    result = run_tool(runtime, "get_selected_document_overviews", {"cursor": None})
    assert result.content["status"] == "ok" and result.scope == "document"
    assert result.content["items"][0]["summary"].startswith("Annual plan")
    assert result.content["coverage"]["complete"] is True
    assert [item.id for item in result.evidence] == [chunk_id]
    assert {item.version_id for item in result.evidence} == {selected}
    assert llm.inputs == []
    assert runtime.metadata()[0]["version_id"] == selected
    forbidden = run_tool(runtime, "get_document_processing_diagnostics", {"version_id": other})
    assert forbidden.content["error"]["code"] == "source_out_of_scope"
    # Even valid stored overview references must belong to their original source.
    with database.session() as db, db.begin():
        version = require_persisted_row(db, Version, selected)
        version.insights = {"summary": "Forged", "citation_ids": [str(uuid4())]}
    invalid = run_tool(runtime, "get_selected_document_overviews", {"cursor": None})
    assert invalid.content["error"]["code"] == "invalid_source_references"
    assert not invalid.evidence


def test_failed_insights_return_bounded_samples_not_raw_document(migrated_database):
    """An insight failure yields a useful labeled sample without traversing all raw sections."""
    selected, chunk_id = seed_overview_source()
    with database.session() as db, db.begin():
        version = require_persisted_row(db, Version, selected)
        version.insight_status, version.insights = "failed", None
        for ordinal in range(1, 20):
            db.add(
                Chunk(**chunk_row(selected, ordinal, QUERY_VECTOR, f"Section {ordinal}. " * 200))
            )
    runtime = DocumentToolRuntime([selected], str(uuid4()), ToolLLM())
    overview = run_tool(runtime, "get_selected_document_overviews", {"cursor": None})
    item = overview.content["items"][0]
    assert item["status"] == "failed" and item["coverage"]["complete"] is False
    assert item["coverage"]["total_chunks"] == 20
    assert len(overview.evidence) == len(item["supporting_snippets"]) == 3
    assert overview.evidence[0].id == chunk_id
    assert overview.content["next_cursor"] is None
    assert all(len(snippet["text"]) <= 1200 for snippet in item["supporting_snippets"])
    with database.session() as db:
        assert require_persisted_row(db, Version, selected).status == "ready"


def test_latest_revision_metrics_exclude_history_and_deleted_documents(migrated_database):
    """Count each document's latest upload, not ready historical versions or removed documents."""
    selected, _ = seed_overview_source()
    failed, _ = seed_overview_source("failed.txt")
    deleted, _ = seed_overview_source("deleted.txt")
    with database.session() as db, db.begin():
        previous = require_persisted_row(db, Version, selected)
        document = require_persisted_row(db, Document, previous.document_id)
        latest = Version(
            document_id=document.id,
            version_number=2,
            filename="new.txt",
            mime_type="text/plain",
            size_bytes=1,
            sha256="a" * 64,
            storage_key="unused",
            status="embedding",
        )
        db.add(latest)
        db.flush()
        document.latest_version_id = latest.id
        require_persisted_row(db, Version, failed).status = "failed"
        removed = require_persisted_row(db, Version, deleted)
        require_persisted_row(db, Document, removed.document_id).deleted_at = now()
    runtime = DocumentToolRuntime([selected], str(uuid4()), ToolLLM())
    selected_metrics = run_tool(runtime, "get_document_processing_metrics", {"scope": "selected"})
    assert selected_metrics.content["counts"] == {
        "total": 1,
        "queued": 0,
        "processing": 1,
        "ready": 0,
        "failed": 0,
        "other": 0,
    }
    assert selected_metrics.content["documents_with_ready_source"] == 1
    workspace = run_tool(runtime, "get_document_processing_metrics", {"scope": "workspace"})
    assert workspace.content["counts"]["total"] == 2
    assert workspace.content["counts"]["failed"] == 1
    assert workspace.content["insights"] == {"pending": 1, "ready": 1}


def test_strict_arguments_empty_scope_and_deleted_sources(migrated_database):
    """Unknown tools, forged hidden IDs and missing selections are safe observable failures."""
    selected, _ = seed_overview_source()
    runtime = DocumentToolRuntime([selected], str(uuid4()), ToolLLM())
    for name, args in [
        ("missing", {}),
        ("retrieve_relevant_chunks", {"query": "price", "version_ids": [selected]}),
        ("get_selected_document_overviews", {"cursor": "0"}),
    ]:
        result = run_tool(runtime, name, args)
        assert result.content["status"] == "error" and not result.evidence
    empty = DocumentToolRuntime([], str(uuid4()), ToolLLM())
    assert (
        run_tool(empty, "get_selected_document_overviews", {"cursor": None}).content["status"]
        == "selection_needed"
    )
    assert (
        run_tool(empty, "get_document_processing_metrics", {"scope": "selected"}).content["status"]
        == "selection_needed"
    )
    with database.session() as db, db.begin():
        version = require_persisted_row(db, Version, selected)
        require_persisted_row(db, Document, version.document_id).deleted_at = now()
    assert (
        run_tool(runtime, "get_selected_document_overviews", {"cursor": None}).content["error"][
            "code"
        ]
        == "version_not_found"
    )


def test_saved_result_preview_scoped_and_agent_tools_read_only(migrated_database, monkeypatch):
    """Saved results return bounded previews; removed paid agent actions cannot queue jobs."""
    from docvault.analysis import get_or_create_artifact_job

    monkeypatch.setattr("docvault.analysis.notify_change", lambda: None)
    first, chunk_id = seed_overview_source()
    second, _ = seed_overview_source("second.txt")
    runtime = DocumentToolRuntime([first], str(uuid4()), ToolLLM())
    # The existing dashboard/API admission remains available and idempotent.
    options = {"length": "short", "focus_areas": [], "tone": "neutral"}
    one = get_or_create_artifact_job("summary", [first], options)
    two = get_or_create_artifact_job("summary", [first], options)
    assert one["id"] == two["id"]
    artifact_id = one["id"]
    with database.session() as db, db.begin():
        artifact = require_persisted_row(db, Artifact, artifact_id)
        artifact.status, artifact.data = (
            "ready",
            {"summary": "Annual plan " * 1000, "citation_ids": [chunk_id], "key_insights": []},
        )
    result = run_tool(runtime, "get_document_analysis_result", {"artifact_id": artifact_id})
    assert result.content["artifact_status"] == "ready" and "data" not in result.content
    assert len(result.content["preview"]["summary"]) <= 1600
    assert result.content["preview"]["preview_only"] is True
    assert [item.id for item in result.evidence] == [chunk_id]
    forbidden = run_tool(
        DocumentToolRuntime([second], str(uuid4()), ToolLLM()),
        "get_document_analysis_result",
        {"artifact_id": artifact_id},
    )
    assert forbidden.content["error"]["code"] == "source_out_of_scope"
    for name in (
        "read_document_sections",
        "request_document_summary",
        "compare_selected_documents",
    ):
        assert run_tool(runtime, name, {}).content["error"]["code"] == "unknown_tool"
    with database.session() as db:
        assert len(list(db.scalars(select(Job)))) == 1


def test_diagnostics_remain_scoped_and_read_only(migrated_database):
    """Persisted index failures remain accessible without replaying or changing their state."""
    selected, _ = seed_overview_source()
    with database.session() as db, db.begin():
        db.add(
            Job(
                kind="overview_index",
                resource_id=selected,
                status="failed",
                error="Provider unavailable",
                error_code="provider_unavailable",
                started_at=now() - timedelta(seconds=10),
                finished_at=now(),
            )
        )
    result = run_tool(
        DocumentToolRuntime([selected], str(uuid4()), ToolLLM()),
        "get_document_processing_diagnostics",
        {"version_id": selected},
    )
    assert result.content["items"][0]["kind"] == "overview_index"
    assert result.content["items"][0]["status"] == "failed"
    assert result.content["items"][0]["error"]["code"] == "provider_unavailable"


def test_large_pdf_geometry_is_compact_only_in_model_view(migrated_database):
    """Geometry must not consume tool context while original public citation locators survive."""
    from docvault.retrieval import build_public_citation

    selected, chunk_id = seed_overview_source()
    geometry = {
        "page": 1,
        "pages": [1, 2],
        "headings": ["Annual subscription"],
        "spans": [{"page": 1, "bbox": [0.123456, 12.987654, 34.123456, 78.987654]}] * 2000,
        "source_spans": [{"start": 0, "end": 120, "bbox": [1.1234, 2.3456, 3.4567, 4.5678]}] * 2000,
    }
    with database.session() as db, db.begin():
        require_persisted_row(db, Chunk, chunk_id).location = geometry
    runtime = DocumentToolRuntime([selected], str(uuid4()), ToolLLM(context_tokens=5000))
    overview = run_tool(runtime, "get_selected_document_overviews", {"cursor": None})
    assert overview.content["status"] == "ok"
    locator = overview.content["items"][0]["supporting_snippets"][0]["location"]
    assert locator["page"] == 1 and locator["pages"] == [1, 2]
    assert locator["headings"] == ["Annual subscription"]
    assert "spans" not in locator and "source_spans" not in locator
    assert overview.evidence[0].location == geometry
    assert build_public_citation(overview.evidence[0])["location"] == geometry


def test_prior_citations_reload_original_sources_only_for_matching_snapshot(migrated_database):
    """Formatting reuse enforces the captured snapshot and validates original live source IDs."""
    from docvault.errors import AppError

    selected, chunk_id = seed_overview_source()
    other, other_chunk = seed_overview_source("other-snapshot.txt")
    runtime = DocumentToolRuntime([selected], str(uuid4()), ToolLLM())
    evidence = asyncio.run(runtime.read_previous_citation_evidence([chunk_id], [selected]))
    assert evidence[0].text.startswith("Annual plan") and evidence[0].version_id == selected
    assert asyncio.run(runtime.read_previous_citation_evidence([other_chunk], [other])) == []
    with pytest.raises(AppError, match="unavailable source references"):
        asyncio.run(runtime.read_previous_citation_evidence([other_chunk], [selected]))
    with database.session() as db, db.begin():
        version = require_persisted_row(db, Version, selected)
        require_persisted_row(db, Document, version.document_id).deleted_at = now()
    with pytest.raises(AppError, match="Document version not found"):
        asyncio.run(runtime.read_previous_citation_evidence([chunk_id], [selected]))


def test_analysis_preview_preserves_only_fully_supported_insights():
    """A few snippets must not pretend to support every statement in a generated summary."""
    from docvault.tools.runtime import build_analysis_preview, select_analysis_supporting_ids

    data = {
        "summary": "Long derived orientation.",
        "citation_ids": ["intro", "sft", "grpo", "extra"],
        "key_insights": [
            {"text": "SFT concept", "citation_ids": ["sft"]},
            {"text": "GRPO concept", "citation_ids": ["grpo"]},
            {"text": "Joint evidence required", "citation_ids": ["intro", "extra"]},
        ],
    }
    selected = select_analysis_supporting_ids(data)
    assert selected == ["sft", "grpo", "intro"]
    preview = build_analysis_preview(data, set(selected))
    assert [item["text"] for item in preview["key_insights"]] == ["SFT concept", "GRPO concept"]
    assert "Derived orientation only" in preview["summary_note"]
    assert preview["preview_only"] is True
