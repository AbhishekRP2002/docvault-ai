"""Document work performed outside request handlers, with fenced commits."""

import importlib.metadata
import os
from pathlib import Path

from sqlalchemy import delete, or_, select, update

from docvault.ai.insights import compare, summarize
from docvault.ai.parsing import parse_file
from docvault.ai.provider import token_count
from docvault.ai.types import Evidence, ParsedDocument
from docvault.cache import cache_get, cache_set, count_metric, notify_change, signature
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_version
from docvault.integrations import create_ai
from docvault.jobs import job_checkpoint
from docvault.models import Artifact, Chunk, Document, Job, JobAttempt, Version, now
from docvault.storage import storage_file


def parsed_path(version_id: str) -> Path:
    return storage_file(f"parsed/{version_id}.json")


def parser_fingerprint(version: Version) -> str:
    parser = "utf8-v1"
    if version.mime_type != "text/plain":
        parser = f"docling-{importlib.metadata.version('docling')}"
    return signature(
        {
            "sha256": version.sha256,
            "filename": version.filename,
            "parser": parser,
            "chunker": "structure-600-v1",
            "ocr": "rapidocr-english-torch",
        }
    )


def _read_parsed(version_id: str, fingerprint: str) -> ParsedDocument | None:
    path = parsed_path(version_id)
    try:
        if path.with_suffix(".fingerprint").read_text() != fingerprint:
            return None
        return ParsedDocument.model_validate_json(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _write_parsed(version_id: str, parsed: ParsedDocument, fingerprint: str, token: int) -> None:
    path = parsed_path(version_id)
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


def _set_stage(job_id: str, token: int, stage: str) -> None:
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token, stage)
        if job.kind == "ingest":
            version = require_version(db, job.resource_id)
            version.status, version.error = stage, None
        elif job.kind == "insights":
            require_version(db, job.resource_id, ready=True).insight_status = "running"
        elif job.kind in {"summary", "comparison"}:
            db.get(Artifact, job.resource_id).status = "running"
    notify_change()


def _canonical(version: Version) -> tuple[ParsedDocument, str]:
    fingerprint = parser_fingerprint(version)
    existing = _read_parsed(version.id, fingerprint)
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
        existing = _read_parsed(candidate, fingerprint)
        if existing:
            count_metric("parser_cache_hit")
            return existing, fingerprint
    return parse_file(
        storage_file(version.storage_key), version.mime_type, version.filename
    ), fingerprint


def _prepare_chunks(job_id: str, token: int, parsed: ParsedDocument, fingerprint: str) -> None:
    settings = get_settings()
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token, "chunking")
        version = require_version(db, job.resource_id)
        _write_parsed(version.id, parsed, fingerprint, token)
        existing = list(
            db.scalars(select(Chunk).where(Chunk.version_id == version.id).order_by(Chunk.ordinal))
        )
        hashes = [signature(chunk.embedding_text) for chunk in parsed.chunks]
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


def _embedding_key(input_hash: str) -> str:
    settings = get_settings()
    return "embedding:" + signature(
        [
            settings.openrouter_embedding_model,
            settings.embedding_dimensions,
            input_hash,
        ]
    )


def _valid_vector(value) -> bool:
    import math

    return (
        isinstance(value, list)
        and len(value) == get_settings().embedding_dimensions
        and all(isinstance(number, (int, float)) and math.isfinite(number) for number in value)
    )


def _reusable_vector(input_hash: str) -> list[float] | None:
    cached = cache_get(_embedding_key(input_hash))
    if _valid_vector(cached):
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
        if _valid_vector(vector):
            count_metric("embedding_cache_hit")
            cache_set(_embedding_key(input_hash), vector, ttl=86400)
            return vector
    return None


def _save_vectors(job_id: str, token: int, vectors: dict[str, list[float]]) -> None:
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
        cache_set(_embedding_key(input_hash), vector, ttl=86400)


async def _embed_missing(job_id: str, token: int, version_id: str) -> None:
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
        cached = _reusable_vector(input_hash)
        if cached is not None:
            reused[input_hash] = cached
        else:
            missing.append((input_hash, text))
    if reused:
        _save_vectors(job_id, token, reused)
    if not missing:
        return
    ai = create_ai(version_id)
    try:
        batch, size = [], 0

        async def save_batch(items):
            with session() as db, db.begin():
                job_checkpoint(db, job_id, token, "embedding")
            values = await ai.embed([text for _, text in items])
            _save_vectors(job_id, token, dict(zip([key for key, _ in items], values, strict=True)))
            count_metric("embedding_provider_batch")

        for item in missing:
            item_size = token_count(item[1])
            if batch and (len(batch) == 64 or size + item_size > 32_000):
                await save_batch(batch)
                batch, size = [], 0
            batch.append(item)
            size += item_size
        if batch:
            await save_batch(batch)
    finally:
        await ai.close()


def _promote(job_id: str, token: int) -> None:
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
        document = db.get(Document, version.document_id)
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


def version_evidence(db, version_id: str) -> list[Evidence]:
    version = require_version(db, version_id, ready=True)
    chunks = db.scalars(select(Chunk).where(Chunk.version_id == version_id).order_by(Chunk.ordinal))
    return [
        Evidence(
            id=chunk.id,
            document_id=version.document_id,
            version_id=version.id,
            filename=version.filename,
            version_number=version.version_number,
            text=chunk.text,
            location=chunk.location,
        )
        for chunk in chunks
    ]


async def _insights(job_id: str, token: int, version_id: str) -> None:
    with session() as db:
        version = require_version(db, version_id, ready=True)
        if version.insight_status == "ready" and version.insights is not None:
            return
    _set_stage(job_id, token, "insights")
    with session() as db:
        evidence = version_evidence(db, version_id)
    ai = create_ai(version_id)
    try:
        data = await summarize(ai, evidence)
    finally:
        await ai.close()
    with session() as db, db.begin():
        job_checkpoint(db, job_id, token)
        version = require_version(db, version_id, ready=True)
        version.insights, version.insight_status, version.insight_error = data, "ready", None


async def _artifact(job_id: str, token: int, artifact_id: str) -> None:
    with session() as db:
        artifact = db.get(Artifact, artifact_id)
        if artifact.status == "ready" and artifact.data is not None:
            return
    _set_stage(job_id, token, "generating")
    with session() as db:
        artifact = db.get(Artifact, artifact_id)
        evidence = {id: version_evidence(db, id) for id in artifact.version_ids}
        options, kind = artifact.options, artifact.kind
    ai = create_ai(artifact_id)
    try:
        if kind == "summary":
            if len(evidence) != 1:
                raise ValueError("A summary artifact requires exactly one document version.")
            data = await summarize(
                ai,
                next(iter(evidence.values())),
                length=options.get("length", "short"),
                focus_areas=options.get("focus_areas"),
                tone=options.get("tone", "neutral"),
            )
        elif kind == "comparison":
            data = await compare(ai, evidence, options.get("dimensions", []))
        else:
            raise ValueError("Unsupported artifact kind.")
    finally:
        await ai.close()
    with session() as db, db.begin():
        job_checkpoint(db, job_id, token)
        artifact = db.get(Artifact, artifact_id)
        artifact.data, artifact.status, artifact.error = data, "ready", None


def _cleanup(job_id: str, token: int, document_id: str) -> None:
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
            storage_file(version.storage_key).unlink(missing_ok=True)
            parsed_path(version.id).unlink(missing_ok=True)
            parsed_path(version.id).with_suffix(".fingerprint").unlink(missing_ok=True)
            db.execute(delete(Chunk).where(Chunk.version_id == version.id))
            version.status, version.insights, version.insight_status = "deleted", None, "cancelled"


async def process_job(job_id: str, token: int) -> None:
    with session() as db, db.begin():
        job = job_checkpoint(db, job_id, token)
        kind, resource_id = job.kind, job.resource_id
        version = require_version(db, resource_id) if kind == "ingest" else None
    if kind == "ingest":
        if version.status != "ready":
            _set_stage(job_id, token, "parsing")
            parsed, fingerprint = _canonical(version)
            _prepare_chunks(job_id, token, parsed, fingerprint)
            await _embed_missing(job_id, token, version.id)
        _promote(job_id, token)
    elif kind == "insights":
        await _insights(job_id, token, resource_id)
    elif kind in {"summary", "comparison"}:
        await _artifact(job_id, token, resource_id)
    elif kind == "cleanup":
        _cleanup(job_id, token, resource_id)
    else:
        raise ValueError("Unsupported job kind.")
