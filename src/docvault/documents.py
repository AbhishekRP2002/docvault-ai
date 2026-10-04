from datetime import datetime
from pathlib import Path

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docvault.cache import calculate_json_fingerprint, notify_change
from docvault.db import session
from docvault.errors import AppError
from docvault.models import Document, Idempotency, Job, JobAttempt, Version, now
from docvault.storage import resolve_storage_path


def acquire_upload_advisory_lock(db: Session, key: str) -> None:
    """Acquire a transaction-scoped PostgreSQL advisory lock derived from a request key."""
    db.execute(select(func.pg_advisory_xact_lock(int(calculate_json_fingerprint(key)[:15], 16))))


def require_document(db: Session, document_id: str, *, lock: bool = False) -> Document:
    """Return an undeleted document, optionally row-locking it, or raise a not-found error."""
    query = select(Document).where(Document.id == document_id, Document.deleted_at.is_(None))
    if lock:
        query = query.with_for_update()
    doc = db.scalar(query)
    if not doc:
        raise AppError(404, "document_not_found", "Document not found.")
    return doc


def require_version(db: Session, version_id: str, *, ready: bool = False) -> Version:
    """Return a version of an undeleted document and optionally require a ready index."""
    version = db.scalar(
        select(Version)
        .join(Document)
        .where(Version.id == version_id, Document.deleted_at.is_(None))
    )
    if not version:
        raise AppError(404, "version_not_found", "Document version not found.")
    if ready and version.status != "ready":
        raise AppError(
            409, "document_not_ready", f"{version.filename} is not ready for questions yet."
        )
    return version


def require_versions(db: Session, version_ids: list[str], *, ready: bool = True) -> list[Version]:
    """Validate a nonempty selection and resolve unique versions in the supplied order."""
    if not version_ids:
        raise AppError(422, "documents_required", "Select at least one document.")
    return [
        require_version(db, version_id, ready=ready) for version_id in dict.fromkeys(version_ids)
    ]


def serialize_processing(job: Job, attempt: JobAttempt | None, timestamp: datetime) -> dict:
    """Describe the latest ingestion attempt, excluding queue and retry backoff time."""
    started_at = finished_at = None
    if job.attempts:
        # Manual retries reset attempts but retain the previous cycle's attempt rows.
        # A missing current attempt may fall back to the job's own recorded timestamps.
        if attempt and (
            attempt.attempt != job.attempts
            or (job.started_at and attempt.started_at < job.started_at)
        ):
            attempt = None
        started_at = attempt.started_at if attempt else job.started_at
        finished_at = attempt.finished_at if attempt else job.finished_at
    end = timestamp if job.status == "running" else finished_at
    duration_ms = (
        max(0, (end - started_at).total_seconds() * 1000)
        if started_at is not None and end is not None
        else None
    )
    return dict(
        run_id=job.id,
        status=job.status,
        attempts=job.attempts,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
    )


def load_processing_metadata(db: Session, version_ids: list[str]) -> dict[str, dict]:
    """Read the newest ingestion job and attempt per version in at most two queries."""
    if not version_ids:
        return {}
    ranked_jobs = (
        select(
            Job.id,
            func.row_number()
            .over(partition_by=Job.resource_id, order_by=(Job.created_at.desc(), Job.id.desc()))
            .label("rank"),
        )
        .where(Job.resource_id.in_(version_ids), Job.kind == "ingest")
        .subquery()
    )
    jobs = list(
        db.scalars(
            select(Job).join(ranked_jobs, Job.id == ranked_jobs.c.id).where(ranked_jobs.c.rank == 1)
        )
    )
    if not jobs:
        return {}
    ranked_attempts = (
        select(
            JobAttempt.id,
            func.row_number()
            .over(
                partition_by=JobAttempt.job_id,
                order_by=(JobAttempt.started_at.desc(), JobAttempt.id.desc()),
            )
            .label("rank"),
        )
        .where(JobAttempt.job_id.in_([job.id for job in jobs]))
        .subquery()
    )
    attempts = {
        attempt.job_id: attempt
        for attempt in db.scalars(
            select(JobAttempt)
            .join(ranked_attempts, JobAttempt.id == ranked_attempts.c.id)
            .where(ranked_attempts.c.rank == 1)
        )
    }
    timestamp = now()
    return {
        job.resource_id: serialize_processing(job, attempts.get(job.id), timestamp) for job in jobs
    }


def _serialize_document(doc: Document, version: Version | None, processing: dict | None) -> dict:
    """Build the shared document payload from already loaded metadata."""
    if version is None:
        raise AppError(
            409, "document_not_ready", "This document has no available uploaded version yet."
        )
    insights = version.insights or {}
    return dict(
        id=doc.id,
        title=doc.title,
        filename=version.filename,
        mime_type=version.mime_type,
        size_bytes=version.size_bytes,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
        current_version_id=doc.current_version_id,
        latest_version_id=version.id,
        status=version.status,
        version_number=version.version_number,
        page_count=version.page_count,
        chunk_count=version.chunk_count,
        token_count=version.token_count,
        parser=version.parser,
        embedding_model=version.embedding_model,
        processing=processing,
        insight_status=version.insight_status,
        summary=insights.get("summary"),
        category=insights.get("category"),
        tags=insights.get("tags", []),
        error=version.error,
    )


def serialize_document_response(db: Session, doc: Document, version: Version | None = None) -> dict:
    """Build a library payload from a document and its supplied or latest version."""
    if version is None and doc.latest_version_id is not None:
        version = db.get(Version, doc.latest_version_id)
    processing = load_processing_metadata(db, [version.id]) if version else {}
    return _serialize_document(doc, version, processing.get(version.id) if version else None)


def serialize_document_responses(db: Session, documents: list[Document]) -> list[dict]:
    """Serialize a page's latest versions and processing metadata without per-row queries."""
    if not documents:
        return []
    version_ids = [doc.latest_version_id for doc in documents if doc.latest_version_id]
    versions = {
        version.id: version
        for version in db.scalars(select(Version).where(Version.id.in_(version_ids)))
    }
    processing = load_processing_metadata(db, list(versions))
    return [
        _serialize_document(
            doc,
            versions.get(doc.latest_version_id) if doc.latest_version_id else None,
            processing.get(doc.latest_version_id) if doc.latest_version_id else None,
        )
        for doc in documents
    ]


def accept_document_upload(info: dict, key: str | None, document_id: str | None = None) -> dict:
    """Persist or reuse a validated upload and create ingestion intent with the version.

    Serialize duplicate requests, replay matching keys, and remove any unadopted source file.
    """
    fingerprint = calculate_json_fingerprint([info["sha256"], info["filename"], document_id])
    scoped_key = f"upload:{document_id or 'new'}:{key}" if key else None
    adopted = False
    try:
        with session() as db, db.begin():
            acquire_upload_advisory_lock(db, scoped_key or f"upload:{fingerprint}")
            if scoped_key and (record := db.get(Idempotency, scoped_key)):
                if record.fingerprint != fingerprint:
                    raise AppError(
                        409,
                        "idempotency_conflict",
                        "This request key was used for different content.",
                    )
                doc = require_document(db, record.resource_id)
                return record.result or serialize_document_response(db, doc)
            doc = require_document(db, document_id, lock=True) if document_id else None
            if not doc:
                previous = db.scalar(
                    select(Version)
                    .join(Document)
                    .where(
                        Version.sha256 == info["sha256"],
                        Version.filename == info["filename"],
                        Document.deleted_at.is_(None),
                    )
                    .order_by(Version.created_at.desc())
                    .limit(1)
                )
                if previous:
                    doc = require_document(db, previous.document_id, lock=True)
            if doc:
                latest = db.get(Version, doc.latest_version_id) if doc.latest_version_id else None
                if latest and latest.sha256 == info["sha256"]:
                    if scoped_key:
                        db.add(
                            Idempotency(
                                key=scoped_key,
                                fingerprint=fingerprint,
                                resource_id=doc.id,
                                result=jsonable_encoder(serialize_document_response(db, doc)),
                            )
                        )
                    return serialize_document_response(db, doc)
                last_version_number = db.scalar(
                    select(func.max(Version.version_number)).where(Version.document_id == doc.id)
                )
                number = last_version_number + 1 if last_version_number is not None else 1
            else:
                doc = Document(title=Path(info["filename"]).stem)
                db.add(doc)
                db.flush()
                number = 1
            version = Version(document_id=doc.id, version_number=number, **info)
            db.add(version)
            db.flush()
            doc.latest_version_id = version.id
            doc.updated_at = now()
            db.add(Job(kind="ingest", resource_id=version.id))
            if scoped_key:
                db.add(
                    Idempotency(
                        key=scoped_key,
                        fingerprint=fingerprint,
                        resource_id=doc.id,
                        result=jsonable_encoder(serialize_document_response(db, doc)),
                    )
                )
            db.flush()
            result = serialize_document_response(db, doc)
        adopted = True
        notify_change()
        return result
    finally:
        if not adopted:
            resolve_storage_path(info["storage_key"]).unlink(missing_ok=True)
