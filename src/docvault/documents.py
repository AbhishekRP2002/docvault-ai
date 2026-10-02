from pathlib import Path

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from docvault.cache import notify_change, signature
from docvault.db import session
from docvault.errors import AppError
from docvault.models import Document, Idempotency, Job, Version, now
from docvault.storage import storage_file


def advisory_lock(db, key: str):
    db.execute(select(func.pg_advisory_xact_lock(int(signature(key)[:15], 16))))


def require_document(db, document_id: str, *, lock=False) -> Document:
    query = select(Document).where(Document.id == document_id, Document.deleted_at.is_(None))
    if lock:
        query = query.with_for_update()
    doc = db.scalar(query)
    if not doc:
        raise AppError(404, "document_not_found", "Document not found.")
    return doc


def require_version(db, version_id: str, *, ready=False) -> Version:
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


def require_versions(db, version_ids: list[str], *, ready=True) -> list[Version]:
    if not version_ids:
        raise AppError(422, "documents_required", "Select at least one document.")
    return [
        require_version(db, version_id, ready=ready) for version_id in dict.fromkeys(version_ids)
    ]


def document_response(db, doc: Document, version: Version | None = None) -> dict:
    version = version or db.get(Version, doc.latest_version_id)
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
        insight_status=version.insight_status,
        summary=insights.get("summary"),
        category=insights.get("category"),
        tags=insights.get("tags", []),
        error=version.error,
    )


def accept_upload(info: dict, key: str | None, document_id: str | None = None) -> dict:
    fingerprint = signature([info["sha256"], info["filename"], document_id])
    scoped_key = f"upload:{document_id or 'new'}:{key}" if key else None
    adopted = False
    try:
        with session() as db, db.begin():
            advisory_lock(db, scoped_key or f"upload:{fingerprint}")
            if scoped_key and (record := db.get(Idempotency, scoped_key)):
                if record.fingerprint != fingerprint:
                    raise AppError(
                        409,
                        "idempotency_conflict",
                        "This request key was used for different content.",
                    )
                require_document(db, record.resource_id)
                return record.result or document_response(db, db.get(Document, record.resource_id))
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
                latest = db.get(Version, doc.latest_version_id)
                if latest and latest.sha256 == info["sha256"]:
                    if scoped_key:
                        db.add(
                            Idempotency(
                                key=scoped_key,
                                fingerprint=fingerprint,
                                resource_id=doc.id,
                                result=jsonable_encoder(document_response(db, doc)),
                            )
                        )
                    return document_response(db, doc)
                number = (
                    db.scalar(
                        select(func.max(Version.version_number)).where(
                            Version.document_id == doc.id
                        )
                    )
                    + 1
                )
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
                        result=jsonable_encoder(document_response(db, doc)),
                    )
                )
            db.flush()
            result = document_response(db, doc)
        adopted = True
        notify_change()
        return result
    finally:
        if not adopted:
            storage_file(info["storage_key"]).unlink(missing_ok=True)
