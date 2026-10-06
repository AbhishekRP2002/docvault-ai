"""Versioned derived overviews for topic discovery, separate from original evidence."""

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from docvault.cache import cache_get, cache_set, calculate_json_fingerprint, notify_change
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import acquire_upload_advisory_lock, require_version
from docvault.llm.provider import OpenRouterLLM
from docvault.models import Document, DocumentOverview, Job, Version
from docvault.retrieval import calculate_rrf, configure_hnsw_search

OVERVIEW_CANDIDATE_LIMIT = 15
OVERVIEW_PAGE_SIZE = 5
UUID_PATTERN = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")


def build_document_overview_identity(title: str, version: Version) -> dict:
    """Combine derived topic text with generation/model identities for exact index reuse."""
    insights = version.insights or {}
    parts = [title, insights.get("category", ""), " ".join(insights.get("tags", []))]
    parts.append(insights.get("summary", ""))
    parts.extend(fact.get("text", "") for fact in insights.get("key_insights", []))
    overview_text = UUID_PATTERN.sub("", "\n".join(str(part) for part in parts if part)).strip()
    generation = insights.get("generation", {}).get("fingerprint")
    return {
        "title": title,
        "text": overview_text,
        "content_hash": calculate_json_fingerprint(overview_text),
        "generation_fingerprint": generation or calculate_json_fingerprint(insights),
        "embedding_model": get_settings().openrouter_embedding_model,
    }


def overview_matches_current_insights(overview: DocumentOverview, identity: dict) -> bool:
    """Require the current text, generation and embedding identity, including a usable vector."""
    return overview.embedding is not None and all(
        getattr(overview, field) == identity[field]
        for field in ("title", "content_hash", "generation_fingerprint", "embedding_model")
    )


def enqueue_document_overview_index(db: Session, version_id: str) -> str | None:
    """Queue one idempotent derived-index intent without reparsing or changing source state."""
    version = require_version(db, version_id, ready=True)
    if version.insight_status != "ready" or not version.insights:
        return None
    document = db.get(Document, version.document_id)
    if document is None:
        return None
    identity = build_document_overview_identity(document.title, version)
    acquire_upload_advisory_lock(db, f"overview-index:{version_id}")
    overview = db.get(DocumentOverview, version_id)
    if overview is not None and overview_matches_current_insights(overview, identity):
        return None
    existing = db.scalar(
        select(Job)
        .where(
            Job.kind == "overview_index",
            Job.resource_id == version_id,
            Job.status.in_(["queued", "enqueued", "running", "failed"]),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        # Failed work remains in the durable dead-letter path; backfill never hides it.
        return existing.id
    job = Job(kind="overview_index", resource_id=version_id)
    db.add(job)
    db.flush()
    return job.id


def backfill_document_overview_jobs(*, execute: bool = False, limit: int = 200) -> dict:
    """Inspect or queue missing current-ready overviews; repeated batches safely reuse jobs."""
    items = []
    with session() as db, db.begin():
        versions = list(
            db.scalars(
                select(Version)
                .join(Document, Document.current_version_id == Version.id)
                .where(
                    Document.deleted_at.is_(None),
                    Version.status == "ready",
                    Version.insight_status == "ready",
                    Version.insights.is_not(None),
                )
                .order_by(Version.id)
            )
        )
        for version in versions:
            document = db.get(Document, version.document_id)
            if document is None:
                continue
            identity = build_document_overview_identity(document.title, version)
            overview = db.get(DocumentOverview, version.id)
            if overview is not None and overview_matches_current_insights(overview, identity):
                continue
            if len(items) == limit:
                break
            job_id = enqueue_document_overview_index(db, version.id) if execute else None
            items.append({"version_id": version.id, "job_id": job_id})
    if execute and items:
        notify_change()
    return {"execute": execute, "items": items, "count": len(items)}


def build_overview_candidate_queries(vector: list[float], query: str):
    """Build native filtered HNSW and GIN searches without a passage-similarity cutoff."""
    generation = Version.insights["generation"]["fingerprint"].astext
    eligible = (
        Document.deleted_at.is_(None),
        Version.status == "ready",
        Version.insight_status == "ready",
        DocumentOverview.embedding_model == get_settings().openrouter_embedding_model,
        DocumentOverview.embedding.is_not(None),
        or_(generation.is_(None), generation == DocumentOverview.generation_fingerprint),
    )
    base = (
        select(DocumentOverview.version_id)
        .join(Version, DocumentOverview.version_id == Version.id)
        .join(Document, Document.current_version_id == Version.id)
        .where(*eligible)
    )
    distance = DocumentOverview.embedding.cosine_distance(vector)
    terms = func.websearch_to_tsquery("english", query)
    semantic = (
        base.add_columns(distance.label("distance"))
        .order_by(distance)
        .limit(OVERVIEW_CANDIDATE_LIMIT)
    )
    lexical = (
        base.add_columns(func.ts_rank_cd(DocumentOverview.search, terms).label("rank"))
        .where(DocumentOverview.search.op("@@")(terms))
        .order_by(
            func.ts_rank_cd(DocumentOverview.search, terms).desc(), DocumentOverview.version_id
        )
        .limit(OVERVIEW_CANDIDATE_LIMIT)
    )
    return semantic, lexical


async def search_document_overviews(
    query: str, llm: OpenRouterLLM, cursor: int | None = None
) -> dict:
    """Return topic-ranked current document metadata, never new selected QA evidence.

    Similarity/ranks are transparent discovery signals, not evidence-quality guarantees.
    SQL limits both candidate lists to 15; pagination exposes their fused union.
    """
    offset = cursor or 0
    settings = get_settings()
    key = "overview-query:" + calculate_json_fingerprint(
        [query, settings.openrouter_embedding_model, settings.embedding_dimensions]
    )
    vector = cache_get(key)
    if vector is None:
        vector = (await llm.embed_texts([query]))[0]
        cache_set(key, vector, 86400)
    with session() as db:
        configure_hnsw_search(db)
        semantic_query, lexical_query = build_overview_candidate_queries(vector, query)
        semantic, lexical = list(db.execute(semantic_query)), list(db.execute(lexical_query))
        semantic_scores = {row.version_id: 1.0 - row.distance for row in semantic}
        lexical_scores = {row.version_id: row.rank for row in lexical}
        ranked_ids = calculate_rrf(
            [[row.version_id for row in semantic], [row.version_id for row in lexical]]
        )
        valid = []
        for identifier in ranked_ids:
            overview = db.get(DocumentOverview, identifier)
            version = require_version(db, identifier, ready=True)
            document = db.get(Document, version.document_id)
            if document is None or overview is None:
                continue
            identity = build_document_overview_identity(document.title, version)
            if not overview_matches_current_insights(overview, identity):
                continue
            insights = version.insights or {}
            latest = (
                db.get(Version, document.latest_version_id) if document.latest_version_id else None
            )
            valid.append(
                {
                    "document_id": document.id,
                    "version_id": version.id,
                    "filename": version.filename,
                    "title": document.title,
                    "version_number": version.version_number,
                    "category": insights.get("category"),
                    "tags": insights.get("tags", []),
                    "description": str(insights.get("summary", ""))[:1600],
                    "latest_upload_status": latest.status if latest else None,
                    "cosine_similarity": semantic_scores.get(identifier),
                    "lexical_rank": lexical_scores.get(identifier),
                    "selected_for_qa": False,
                }
            )
        live_count = (
            db.scalar(
                select(func.count()).select_from(Document).where(Document.deleted_at.is_(None))
            )
            or 0
        )
        indexed_count = (
            db.scalar(
                select(func.count())
                .select_from(DocumentOverview)
                .join(Version, Version.id == DocumentOverview.version_id)
                .join(Document, Document.current_version_id == Version.id)
                .where(Document.deleted_at.is_(None), Version.status == "ready")
            )
            or 0
        )
        end = offset + OVERVIEW_PAGE_SIZE
        return {
            "status": "ok",
            "items": valid[offset:end],
            "next_cursor": end if end < len(valid) else None,
            "coverage": {
                "candidate_matches": len(valid),
                "workspace_documents": live_count,
                "persisted_current_overview_records": indexed_count,
                "semantic_candidate_limit": OVERVIEW_CANDIDATE_LIMIT,
                "lexical_candidate_limit": OVERVIEW_CANDIDATE_LIMIT,
            },
            "selection_notice": "Discovery results do not change the selected QA sources.",
        }
