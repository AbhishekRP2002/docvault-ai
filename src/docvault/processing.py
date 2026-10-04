"""Document work performed outside request handlers, with fenced commits."""

import importlib.metadata
import os
from pathlib import Path

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from docvault.cache import (
    cache_get,
    cache_set,
    calculate_json_fingerprint,
    count_metric,
    notify_change,
)
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_document, require_version
from docvault.integrations import create_llm_client
from docvault.jobs import SourceDeleted, job_checkpoint
from docvault.llm.insights import generate_document_comparison, generate_document_summary
from docvault.llm.models import Evidence
from docvault.llm.prompts import build_generation_identity
from docvault.llm.provider import token_count
from docvault.models import Artifact, Chunk, Document, Job, JobAttempt, Version, now
from docvault.parsing import ParsedDocument, parse_document_file
from docvault.retrieval import create_cited_evidence_record
from docvault.storage import resolve_storage_path


def get_parsed_document_path(version_id: str) -> Path:
    """Return the workspace path for a version's canonical parsed JSON."""
    return resolve_storage_path(f"parsed/{version_id}.json")


def calculate_parser_fingerprint(version: Version) -> str:
    """Hash the source identity and parser, chunker, and OCR configuration for reuse."""
    parser = "utf8-v1"
    if version.mime_type != "text/plain":
        parser = f"docling-{importlib.metadata.version('docling')}"
    return calculate_json_fingerprint(
        {
            "sha256": version.sha256,
            "filename": version.filename,
            "parser": parser,
            "chunker": f"docling-hybrid-600-v2-core-{importlib.metadata.version('docling-core')}",
            "ocr": "rapidocr-english-torch",
        }
    )


def _load_cached_parsed_document(version_id: str, fingerprint: str) -> ParsedDocument | None:
    """Load a valid parsed artifact only when its saved fingerprint matches.

    Missing, outdated, or invalid artifacts are treated as cache misses.
    """
    path = get_parsed_document_path(version_id)
    try:
        if path.with_suffix(".fingerprint").read_text() != fingerprint:
            return None
        return ParsedDocument.model_validate_json(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _persist_parsed_document(
    version_id: str, parsed: ParsedDocument, fingerprint: str, token: int
) -> None:
    """Atomically replace the parsed JSON, then write its reuse fingerprint.

    Flush the temporary file to disk and remove it even if writing fails.
    """
    path = get_parsed_document_path(version_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{token}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(parsed.model_dump_json())
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.with_suffix(".fingerprint").write_text(fingerprint)
    finally:
        temporary.unlink(missing_ok=True)


def _get_required_artifact(db: Session, artifact_id: str) -> Artifact:
    """Return an existing artifact or signal that its source resource was removed."""
    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise SourceDeleted("The artifact no longer exists.")
    return artifact


def _update_processing_stage(job_id: str, token: int, stage: str) -> None:
    """Verify job ownership and commit the stage to its associated resource.

    Publish a change notification after the transaction commits.
    """
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token, stage)
        if job.kind == "ingest":
            version = require_version(db, job.resource_id)
            version.status, version.error = stage, None
        elif job.kind == "insights":
            require_version(db, job.resource_id, ready=True).insight_status = "running"
        elif job.kind in {"summary", "comparison"}:
            _get_required_artifact(db, job.resource_id).status = "running"
    notify_change()


def _load_or_parse_document(version: Version) -> tuple[ParsedDocument, str]:
    """Reuse matching parsed content from this or another live version, or parse the file."""
    fingerprint = calculate_parser_fingerprint(version)
    existing = _load_cached_parsed_document(version.id, fingerprint)
    if existing:
        count_metric("parser_cache_hit")
        return existing, fingerprint
    with session() as db:
        candidates = list(
            db.scalars(
                select(Version.id)
                .join(Document)
                .where(
                    Version.sha256 == version.sha256,
                    Version.filename == version.filename,
                    Version.status == "ready",
                    Document.deleted_at.is_(None),
                )
            )
        )
    for candidate in candidates:
        existing = _load_cached_parsed_document(candidate, fingerprint)
        if existing:
            count_metric("parser_cache_hit")
            return existing, fingerprint
    return parse_document_file(
        resolve_storage_path(version.storage_key), version.mime_type, version.filename
    ), fingerprint


def _persist_document_chunks(
    job_id: str, token: int, parsed: ParsedDocument, fingerprint: str
) -> None:
    """Persist parsed chunks and move a claimed ingestion job to embedding.

    Preserve vectors when ordered input hashes and the model match; refresh source locations.
    """
    settings = get_settings()
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token, "chunking")
        version = require_version(db, job.resource_id)
        _persist_parsed_document(version.id, parsed, fingerprint, token)
        existing = list(
            db.scalars(select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal))
        )
        hashes = [calculate_json_fingerprint(chunk.embedding_text) for chunk in parsed.chunks]
        reusable = (
            len(existing) == len(hashes)
            and [chunk.input_hash for chunk in existing] == hashes
            and version.embedding_model == settings.openrouter_embedding_model
        )
        if not reusable:
            db.execute(delete(Chunk).where(Chunk.version_id == version.id))
            db.add_all(
                [
                    Chunk(
                        version_id=version.id,
                        ordinal=index,
                        input_hash=hashes[index],
                        **chunk.model_dump(),
                    )
                    for index, chunk in enumerate(parsed.chunks)
                ]
            )
        else:
            # A parser upgrade can change source boxes while preserving the exact
            # embedding input. Keep reusable vectors, refresh the source mapping.
            for stored, parsed_chunk in zip(existing, parsed.chunks, strict=True):
                stored.text = parsed_chunk.text
                stored.location = parsed_chunk.location
                stored.token_count = parsed_chunk.token_count
        version.status = "embedding"
        version.chunk_count = len(parsed.chunks)
        version.token_count = sum(chunk.token_count for chunk in parsed.chunks)
        version.page_count, version.parser = parsed.page_count, parsed.parser
        version.embedding_model = settings.openrouter_embedding_model
        job.stage = "embedding"
    notify_change()


def _build_embedding_cache_key(input_hash: str) -> str:
    """Build a cache key scoped to the input hash, embedding model, and dimensions."""
    settings = get_settings()
    return "embedding:" + calculate_json_fingerprint(
        [
            settings.openrouter_embedding_model,
            settings.embedding_dimensions,
            input_hash,
        ]
    )


def _is_valid_embedding_vector(value) -> bool:
    """Check that a cached vector has the configured length and finite numeric values."""
    import math

    return (
        isinstance(value, list)
        and len(value) == get_settings().embedding_dimensions
        and all(isinstance(number, (int, float)) and math.isfinite(number) for number in value)
    )


def _find_reusable_embedding(input_hash: str) -> list[float] | None:
    """Find a valid vector in Redis or a ready, undeleted version using the same model."""
    cached = cache_get(_build_embedding_cache_key(input_hash))
    if _is_valid_embedding_vector(cached):
        count_metric("embedding_cache_hit")
        return cached
    with session() as db:
        value = db.scalar(
            select(Chunk.embedding)
            .join(Version)
            .join(Document)
            .where(
                Chunk.input_hash == input_hash,
                Chunk.embedding.is_not(None),
                Version.embedding_model == get_settings().openrouter_embedding_model,
                Version.status == "ready",
                Document.deleted_at.is_(None),
            )
            .limit(1)
        )
    if value is not None:
        vector = [float(number) for number in value]
        if _is_valid_embedding_vector(vector):
            count_metric("embedding_cache_hit")
            cache_set(_build_embedding_cache_key(input_hash), vector, ttl=86400)
            return vector
    return None


def _persist_chunk_embeddings(job_id: str, token: int, vectors: dict[str, list[float]]) -> None:
    """Persist vectors under the current job claim, then cache them by embedding input."""
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token, "embedding")
        for input_hash, vector in vectors.items():
            db.execute(
                update(Chunk)
                .where(
                    Chunk.version_id == job.resource_id,
                    Chunk.input_hash == input_hash,
                )
                .values(embedding=vector)
            )
    for input_hash, vector in vectors.items():
        cache_set(_build_embedding_cache_key(input_hash), vector, ttl=86400)


async def _embed_missing_chunks(job_id: str, token: int, version_id: str) -> None:
    """Reuse known vectors and embed the remaining distinct inputs in microbatches.

    Checkpoint ownership before each provider batch and persist completed batches for retry.
    """
    with session() as db:
        pending = list(
            db.scalars(
                select(Chunk)
                .where(
                    Chunk.version_id == version_id,
                    Chunk.embedding.is_(None),
                )
                .order_by(Chunk.ordinal)
            )
        )
    # Deduplicate identical embedding inputs within this version as well.
    unique = {chunk.input_hash: chunk.embedding_text for chunk in pending}
    missing, reused = [], {}
    for input_hash, text in unique.items():
        cached = _find_reusable_embedding(input_hash)
        if cached is not None:
            reused[input_hash] = cached
        else:
            missing.append((input_hash, text))
    if reused:
        _persist_chunk_embeddings(job_id, token, reused)
    if not missing:
        return
    llm = create_llm_client(version_id)
    try:
        configuration = get_settings().embedding_model()
        batch, size = [], 0

        async def embed_and_persist_batch(items):
            """Verify ownership, embed this batch, and save vectors keyed by input hash."""
            with session() as db, db.begin():
                job_checkpoint(db, job_id, token, "embedding")
            values = await llm.embed_texts([text for _, text in items])
            _persist_chunk_embeddings(
                job_id, token, dict(zip([key for key, _ in items], values, strict=True))
            )
            count_metric("embedding_provider_batch")

        for item in missing:
            item_size = token_count(item[1])
            if batch and (
                len(batch) == configuration.max_batch_inputs
                or size + item_size > configuration.max_batch_tokens
            ):
                await embed_and_persist_batch(batch)
                batch, size = [], 0
            batch.append(item)
            size += item_size
        if batch:
            await embed_and_persist_batch(batch)
    finally:
        await llm.close()


def _activate_ready_version(job_id: str, token: int) -> None:
    """Mark a fully embedded version ready and enqueue its automatic insights.

    Advance the current-version pointer only when this version is at least as new.
    """
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token, "ready")
        version = require_version(db, job.resource_id)
        missing = db.scalar(
            select(Chunk.id)
            .where(
                Chunk.version_id == version.id,
                Chunk.embedding.is_(None),
            )
            .limit(1)
        )
        if missing or not version.chunk_count:
            raise ValueError("The document index is incomplete.")
        version.status, version.error, version.ready_at = "ready", None, now()
        document = require_document(db, version.document_id)
        current = (
            db.get(Version, document.current_version_id) if document.current_version_id else None
        )
        if current is None or version.version_number >= current.version_number:
            document.current_version_id = version.id
            document.updated_at = now()
        insight_job = db.scalar(
            select(Job.id).where(Job.kind == "insights", Job.resource_id == version.id)
        )
        if insight_job is None:
            db.add(Job(kind="insights", resource_id=version.id))
    notify_change()


def load_document_version_evidence(db: Session, version_id: str) -> list[Evidence]:
    """Load all chunks of a live, ready version as evidence in document order."""
    version = require_version(db, version_id, ready=True)
    chunks = db.scalars(select(Chunk).where(Chunk.version_id == version_id).order_by(Chunk.ordinal))
    return [create_cited_evidence_record(chunk, version) for chunk in chunks]


async def _create_document_insights(job_id: str, token: int, version_id: str) -> None:
    """Generate and persist full-document insights unless they are already complete.

    Recheck the job claim and source readiness before saving the provider result.
    """
    with session() as db:
        version = require_version(db, version_id, ready=True)
        if version.insight_status == "ready" and version.insights is not None:
            return
    _update_processing_stage(job_id, token, "insights")
    with session() as db:
        evidence = load_document_version_evidence(db, version_id)
    llm = create_llm_client(version_id)
    try:
        data = await generate_document_summary(llm, evidence)
        identity = build_generation_identity(llm.generation_model("summary"), "summary")
        data["generation"] = {
            "fingerprint": calculate_json_fingerprint(identity),
            "task": "summary",
            "model_config": identity["model_config"],
        }
    finally:
        await llm.close()
    with session() as db, db.begin():
        job_checkpoint(db, job_id, token)
        version = require_version(db, version_id, ready=True)
        version.insights, version.insight_status, version.insight_error = data, "ready", None


async def _generate_requested_artifact(job_id: str, token: int, artifact_id: str) -> None:
    """Generate a requested summary or comparison and persist it under the job claim.

    Reuse completed artifacts and recheck the artifact and sources before publication.
    """
    with session() as db:
        artifact = _get_required_artifact(db, artifact_id)
        if artifact.status == "ready" and artifact.data is not None:
            return
    _update_processing_stage(job_id, token, "generating")
    with session() as db:
        artifact = _get_required_artifact(db, artifact_id)
        evidence = {id: load_document_version_evidence(db, id) for id in artifact.version_ids}
        options, kind = artifact.options, artifact.kind
    llm = create_llm_client(artifact_id)
    try:
        if kind not in {"summary", "comparison"}:
            raise ValueError("Unsupported artifact kind.")
        task = "summary" if kind == "summary" else "comparison"
        generation_identity = build_generation_identity(llm.generation_model(task), task)
        if options.get("generation_fingerprint") not in (
            None,
            calculate_json_fingerprint(generation_identity),
        ):
            raise ValueError("LLM configuration changed after submission; request a new artifact.")
        if kind == "summary":
            if len(evidence) != 1:
                raise ValueError("A summary artifact requires exactly one document version.")
            data = await generate_document_summary(
                llm,
                next(iter(evidence.values())),
                length=options.get("length", "short"),
                focus_areas=options.get("focus_areas"),
                tone=options.get("tone", "neutral"),
            )
        else:
            data = await generate_document_comparison(llm, evidence, options.get("dimensions", []))
        data["generation"] = {
            "fingerprint": calculate_json_fingerprint(generation_identity),
            "task": task,
            "model_config": generation_identity["model_config"],
        }
    finally:
        await llm.close()
    with session() as db, db.begin():
        job_checkpoint(db, job_id, token)
        artifact = _get_required_artifact(db, artifact_id)
        artifact.data, artifact.status, artifact.error = data, "ready", None


def _cleanup_deleted_document(job_id: str, token: int, document_id: str) -> None:
    """Remove files, chunks, and insights belonging to a logically deleted document.

    Cancel dependent artifacts and fence their active jobs while retaining metadata.
    """
    with session() as db, db.begin():
        job_checkpoint(db, job_id, token, "cleanup")
        document = db.get(Document, document_id)
        if document is None or document.deleted_at is None:
            raise ValueError("Only a deleted document can be cleaned up.")
        versions = list(db.scalars(select(Version).where(Version.document_id == document_id)))
        version_ids = [version.id for version in versions]
        artifacts = (
            list(
                db.scalars(
                    select(Artifact).where(
                        or_(
                            *[
                                Artifact.version_ids.contains([version_id])
                                for version_id in version_ids
                            ],
                        )
                    )
                )
            )
            if version_ids
            else []
        )
        for artifact in artifacts:
            artifact.data, artifact.status = None, "cancelled"
            artifact.error = "A source document was deleted."
        resource_ids = [*version_ids, *[artifact.id for artifact in artifacts]]
        cancelled_ids = list(
            db.scalars(
                select(Job.id).where(
                    Job.resource_id.in_(resource_ids),
                    Job.status.in_(["queued", "enqueued", "running", "cancelled"]),
                )
            )
        )
        if cancelled_ids:
            db.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.job_id.in_(cancelled_ids),
                    JobAttempt.status == "running",
                )
                .values(
                    status="cancelled", error="A source document was deleted.", finished_at=now()
                )
            )
            db.execute(
                update(Job)
                .where(Job.id.in_(cancelled_ids))
                .values(
                    status="cancelled",
                    token=Job.token + 1,
                    lease_until=None,
                    finished_at=now(),
                )
            )
        for version in versions:
            resolve_storage_path(version.storage_key).unlink(missing_ok=True)
            get_parsed_document_path(version.id).unlink(missing_ok=True)
            get_parsed_document_path(version.id).with_suffix(".fingerprint").unlink(missing_ok=True)
            db.execute(delete(Chunk).where(Chunk.version_id == version.id))
            version.status, version.insights, version.insight_status = "deleted", None, "cancelled"


async def process_document_job(job_id: str, token: int) -> None:
    """Dispatch a claimed job to ingestion, insights, artifact generation, or cleanup.

    Ingestion reuses parsed artifacts and vectors before publishing a complete index.
    """
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token)
        kind, resource_id = job.kind, job.resource_id
        version = require_version(db, resource_id) if kind == "ingest" else None
    if kind == "ingest":
        if version is None:
            raise ValueError("An ingestion job requires a document version.")
        if version.status != "ready":
            _update_processing_stage(job_id, token, "parsing")
            parsed, fingerprint = _load_or_parse_document(version)
            _persist_document_chunks(job_id, token, parsed, fingerprint)
            await _embed_missing_chunks(job_id, token, version.id)
        _activate_ready_version(job_id, token)
    elif kind == "insights":
        await _create_document_insights(job_id, token, resource_id)
    elif kind in {"summary", "comparison"}:
        await _generate_requested_artifact(job_id, token, resource_id)
    elif kind == "cleanup":
        _cleanup_deleted_document(job_id, token, resource_id)
    else:
        raise ValueError("Unsupported job kind.")
