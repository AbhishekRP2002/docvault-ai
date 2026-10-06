"""Verify native derived discovery and independent index jobs using migrated PostgreSQL."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from support import require_persisted_row
from test_document_tools import ToolLLM, seed_overview_source
from test_hybrid_retrieval import QUERY_VECTOR
from test_hybrid_retrieval import isolated_database as isolated_database
from test_hybrid_retrieval import migrated_database as migrated_database

from docvault import db as database
from docvault import jobs, processing
from docvault.models import Document, DocumentOverview, Job, Version
from docvault.tools import discovery as document_discovery
from docvault.tools.discovery import (
    backfill_document_overview_jobs,
    build_document_overview_identity,
    build_overview_candidate_queries,
    enqueue_document_overview_index,
    search_document_overviews,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def discovery_database(migrated_database, monkeypatch):
    """Disable Redis side effects while retaining actual SQL migrations and indexes."""
    monkeypatch.setattr(document_discovery, "cache_get", lambda *args: None)
    monkeypatch.setattr(document_discovery, "cache_set", lambda *args: None)
    monkeypatch.setattr(document_discovery, "notify_change", lambda: None)
    monkeypatch.setattr(jobs, "notify_change", lambda: None)
    return migrated_database


def persist_overview(version_id: str, vector: list[float] = QUERY_VECTOR) -> None:
    """Persist the matching derived representation, linked to its original ready version."""
    with database.session() as db, db.begin():
        version = require_persisted_row(db, Version, version_id)
        document = require_persisted_row(db, Document, version.document_id)
        db.add(
            DocumentOverview(
                version_id=version_id,
                embedding=vector,
                **build_document_overview_identity(document.title, version),
            )
        )


def test_native_hybrid_discovery_current_live_scope_and_scores(discovery_database):
    """Topic discovery uses HNSW/GIN SQL, ignores obsolete/deleted sources and has no passage similarity floor."""
    selected, _ = seed_overview_source("renewal.txt")
    archived, _ = seed_overview_source("archived.txt")
    deleted, _ = seed_overview_source("deleted.txt")
    stale, _ = seed_overview_source("stale.txt")
    low_similarity = [0.4, (1 - 0.4**2) ** 0.5] + [0.0] * 1534
    for identifier in (selected, archived, deleted, stale):
        persist_overview(identifier, low_similarity)
    with database.session() as db, db.begin():
        archive_version = require_persisted_row(db, Version, archived)
        require_persisted_row(db, Document, archive_version.document_id).current_version_id = None
        removed = require_persisted_row(db, Version, deleted)
        require_persisted_row(db, Document, removed.document_id).deleted_at = processing.now()
        changed = require_persisted_row(db, Version, stale)
        changed.insights = {
            **(changed.insights or {}),
            "summary": "Completely different updated summary.",
        }
    result = asyncio.run(search_document_overviews("renewal cancellation", ToolLLM()))
    assert [item["version_id"] for item in result["items"]] == [selected]
    assert result["items"][0]["cosine_similarity"] == pytest.approx(0.4)
    assert result["items"][0]["lexical_rank"] > 0
    assert result["items"][0]["selected_for_qa"] is False
    dense, lexical = build_overview_candidate_queries(QUERY_VECTOR, "renewal")
    assert "<=>" in str(dense) and "@@" in str(lexical)
    assert "document_overviews" in str(dense) and "current_version_id" in str(dense)
    with discovery_database.engine.connect() as connection:
        indexes = list(
            connection.scalars(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE schemaname=:schema AND tablename='document_overviews'"
                ),
                {"schema": discovery_database.schema},
            )
        )
    assert any("USING hnsw" in value for value in indexes)
    assert any("USING gin" in value for value in indexes)


def test_index_job_and_backfill_are_idempotent_without_reparsing(discovery_database, monkeypatch):
    """Repeated backfills queue once, embed once and preserve original ready source/insight state."""
    version_id, _ = seed_overview_source()
    llm = ToolLLM()

    async def close():
        """Close the deterministic client without initializing provider resources."""

    monkeypatch.setattr(llm, "close", close)
    monkeypatch.setattr(processing, "create_llm_client", lambda _: llm)
    dry = backfill_document_overview_jobs()
    assert dry["count"] == 1 and dry["items"][0]["job_id"] is None
    one, two = (
        backfill_document_overview_jobs(execute=True),
        backfill_document_overview_jobs(execute=True),
    )
    job_id = one["items"][0]["job_id"]
    assert job_id == two["items"][0]["job_id"]
    jobs.run_job(job_id)
    jobs.run_job(job_id)
    with database.session() as db:
        version = require_persisted_row(db, Version, version_id)
        overview = require_persisted_row(db, DocumentOverview, version_id)
        assert version.status == "ready" and version.insight_status == "ready"
        assert overview.embedding is not None and overview.content_hash
        assert require_persisted_row(db, Job, job_id).status == "complete"
    assert len(llm.inputs) == 1
    assert backfill_document_overview_jobs(execute=True)["count"] == 0


def test_index_failure_and_replay_do_not_damage_ready_sources(discovery_database, monkeypatch):
    """Failed derived indexing stays independent; safe replay resolves its source document."""
    version_id, _ = seed_overview_source()

    class FailingLLM(ToolLLM):
        """Deterministic invalid embedding response used to exercise terminal index failure."""

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            """Reject this job without accessing any actual provider."""
            raise ValueError("Invalid embedding configuration.")

        async def close(self) -> None:
            """Close a deterministic no-network client."""

    monkeypatch.setattr(processing, "create_llm_client", lambda _: FailingLLM())
    queued = backfill_document_overview_jobs(execute=True)
    job_id = queued["items"][0]["job_id"]
    jobs.run_job(job_id)
    with database.session() as db:
        version = require_persisted_row(db, Version, version_id)
        assert version.status == "ready" and version.insight_status == "ready"
        failed = require_persisted_row(db, Job, job_id)
        assert failed.status == "failed" and jobs._resolve_job_document_ids(db, failed) == [
            version.document_id
        ]
    assert backfill_document_overview_jobs(execute=True)["items"][0]["job_id"] == job_id
    jobs.retry_job(job_id)
    with database.session() as db:
        assert require_persisted_row(db, Job, job_id).status == "queued"
        assert require_persisted_row(db, Version, version_id).status == "ready"
        assert require_persisted_row(db, Version, version_id).insight_status == "ready"


def test_generation_and_content_hash_changes_queue_new_index(discovery_database):
    """Title/insight/generation changes invalidate derived-vector reuse, not original chunk hashes."""
    version_id, _ = seed_overview_source()
    persist_overview(version_id)
    with database.session() as db, db.begin():
        assert enqueue_document_overview_index(db, version_id) is None
        version = require_persisted_row(db, Version, version_id)
        version.insights = {**(version.insights or {}), "generation": {"fingerprint": "b" * 64}}
        db.flush()
        identifier = enqueue_document_overview_index(db, version_id)
        assert identifier is not None
    with database.session() as db:
        assert (
            db.scalar(select(func.count()).select_from(Job).where(Job.kind == "overview_index"))
            == 1
        )
        assert require_persisted_row(db, Version, version_id).chunk_count == 1
    identity = None
    with database.session() as db:
        version = require_persisted_row(db, Version, version_id)
        identity = build_document_overview_identity("A doc " + str(uuid4()), version)
    assert len(identity["content_hash"]) == 64 and not document_discovery.UUID_PATTERN.search(
        identity["text"]
    )
