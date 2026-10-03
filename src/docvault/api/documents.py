import json
from typing import Annotated

from fastapi import APIRouter, File, Header, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from docvault.cache import notify_change, signature
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import (
    accept_upload,
    advisory_lock,
    document_response,
    require_document,
    require_version,
    require_versions,
)
from docvault.errors import AppError
from docvault.limits import enforce_rate
from docvault.models import Artifact, Batch, Chunk, Document, Idempotency, Job, Version, now
from docvault.retrieval import chunk_evidence, citation
from docvault.schemas import ComparisonCreate, SummaryCreate
from docvault.storage import storage_file, store_upload

router = APIRouter(prefix="/v1", tags=["documents"])
RequestKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


@router.get("/documents")
def documents(
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Return a page of live documents filtered by title and latest-version status."""
    with session() as db:
        query = (
            select(Document)
            .where(Document.deleted_at.is_(None))
            .order_by(Document.updated_at.desc())
        )
        if q:
            query = query.where(Document.title.ilike(f"%{q}%"))
        if status:
            query = query.join(Version, Document.latest_version_id == Version.id).where(
                Version.status == status
            )
        return {
            "items": [
                document_response(db, doc) for doc in db.scalars(query.limit(limit).offset(offset))
            ]
        }


@router.post("/documents", status_code=202)
async def upload(file: Annotated[UploadFile, File()], idempotency_key: RequestKey = None):
    """Rate-limit and store a file, then accept or reuse its document ingestion request."""
    enforce_rate("upload", get_settings().upload_rate_per_hour, 3600)
    return accept_upload(await store_upload(file), idempotency_key)


@router.post("/document-batches", status_code=202)
async def upload_batch(
    files: Annotated[list[UploadFile], File()], idempotency_key: RequestKey = None
):
    """Persist or replay a validated upload batch with per-file results and staging-file cleanup."""
    if not files or len(files) > 10:
        raise AppError(422, "batch_size", "Choose between one and ten files per upload batch.")
    if sum(file.size or 0 for file in files) > 100 * 1024 * 1024:
        raise AppError(413, "batch_too_large", "The batch exceeds 100 MiB.")
    prepared = []
    try:
        for file in files:
            try:
                enforce_rate("upload", get_settings().upload_rate_per_hour, 3600)
                prepared.append(
                    {"info": await store_upload(file), "filename": file.filename, "error": None}
                )
            except AppError as exc:
                prepared.append({"info": None, "filename": file.filename, "error": exc.message})
        fingerprint = signature(
            [
                (item["filename"], item["info"]["sha256"] if item["info"] else item["error"])
                for item in prepared
            ]
        )
        with session() as db, db.begin():
            scoped_key = f"batch:{idempotency_key}" if idempotency_key else None
            if scoped_key:
                advisory_lock(db, scoped_key)
                if record := db.get(Idempotency, scoped_key):
                    if record.fingerprint != fingerprint:
                        raise AppError(
                            409,
                            "idempotency_conflict",
                            "This batch key was used for different files.",
                        )
                    return batch_response(db, db.get(Batch, record.resource_id))
            items = []
            for index, item in enumerate(prepared):
                doc = None
                if item["info"]:
                    info = item.pop("info")
                    try:
                        doc = accept_upload(
                            info, f"{idempotency_key}:{index}" if idempotency_key else None
                        )
                    except AppError as exc:
                        item["error"] = exc.message
                items.append(
                    {
                        "document_id": doc["id"] if doc else None,
                        "filename": item["filename"],
                        "error": item["error"],
                    }
                )
            batch = Batch(items=items)
            db.add(batch)
            db.flush()
            if scoped_key:
                db.add(Idempotency(key=scoped_key, fingerprint=fingerprint, resource_id=batch.id))
            return batch_response(db, batch)
    finally:
        for item in prepared:
            if item.get("info"):
                storage_file(item["info"]["storage_key"]).unlink(missing_ok=True)


def batch_response(db, batch):
    """Serialize batch results with live document metadata, rejecting missing batches."""
    if not batch:
        raise AppError(404, "batch_not_found", "Upload batch not found.")
    items = []
    for item in batch.items:
        doc = db.get(Document, item["document_id"]) if item["document_id"] else None
        items.append(
            dict(
                filename=item["filename"],
                error=item["error"],
                document=document_response(db, doc) if doc and not doc.deleted_at else None,
            )
        )
    return {"id": batch.id, "items": items}


@router.get("/document-batches/{batch_id}")
def get_batch(batch_id: str):
    """Return the persisted batch results with current document metadata."""
    with session() as db:
        return batch_response(db, db.get(Batch, batch_id))


@router.get("/documents/{document_id}")
def document(document_id: str):
    """Return a live document's latest-version metadata, rejecting missing or deleted IDs."""
    with session() as db:
        return document_response(db, require_document(db, document_id))


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str):
    """Soft-delete a document, fence active version jobs, and persist a file-cleanup job."""
    with session() as db, db.begin():
        doc = require_document(db, document_id, lock=True)
        doc.deleted_at, doc.updated_at = now(), now()
        version_ids = list(db.scalars(select(Version.id).where(Version.document_id == document_id)))
        for job in db.scalars(
            select(Job).where(
                Job.resource_id.in_(version_ids), Job.status.in_(["queued", "enqueued", "running"])
            )
        ):
            job.status, job.token = "cancelled", job.token + 1
        db.add(Job(kind="cleanup", resource_id=document_id))
    notify_change()
    return Response(status_code=204)


@router.get("/documents/{document_id}/versions")
def versions(document_id: str):
    """List a live document's versions newest first, including processing and insight states."""
    with session() as db:
        require_document(db, document_id)
        return {
            "items": [
                dict(
                    id=v.id,
                    version_number=v.version_number,
                    status=v.status,
                    filename=v.filename,
                    created_at=v.created_at,
                    error=v.error,
                    insight_status=v.insight_status,
                )
                for v in db.scalars(
                    select(Version)
                    .where(Version.document_id == document_id)
                    .order_by(Version.version_number.desc())
                )
            ]
        }


@router.post("/documents/{document_id}/versions", status_code=202)
async def upload_version(
    document_id: str, file: Annotated[UploadFile, File()], idempotency_key: RequestKey = None
):
    """Validate the document, rate-limit the upload, and accept or reuse the uploaded version."""
    with session() as db:
        require_document(db, document_id)
    enforce_rate("upload", get_settings().upload_rate_per_hour, 3600)
    return accept_upload(await store_upload(file), idempotency_key, document_id)


@router.get("/versions/{version_id}")
def version_detail(version_id: str):
    """Return persisted version fields except the internal storage key."""
    with session() as db:
        v = require_version(db, version_id)
        return {
            column.name: getattr(v, column.name)
            for column in v.__table__.columns
            if column.name != "storage_key"
        }


@router.get("/versions/{version_id}/content")
def content(version_id: str, representation: str = "original"):
    """Serve original files or extracted text, rejecting invalid or unavailable content."""
    with session() as db:
        v = require_version(db, version_id)
        if representation == "extracted":
            path = storage_file(f"parsed/{v.id}.json")
            if not path.exists():
                raise AppError(409, "content_not_ready", "Extracted content is not ready yet.")
            return {"text": json.loads(path.read_text())["text"]}
        if representation != "original":
            raise AppError(422, "invalid_representation", "Choose original or extracted content.")
        path = storage_file(v.storage_key)
        if not path.exists():
            raise AppError(404, "source_unavailable", "The source file is unavailable.")
        return FileResponse(
            path, media_type=v.mime_type, filename=v.filename, content_disposition_type="inline"
        )


@router.get("/versions/{version_id}/insights")
def insights(version_id: str):
    """Return the version's persisted insight state, generated data, and error."""
    with session() as db:
        v = require_version(db, version_id)
        return {"status": v.insight_status, "data": v.insights, "error": v.insight_error}


@router.post("/versions/{version_id}/retry", status_code=202)
def retry_version(version_id: str):
    """Requeue the latest failed ingestion or insight job, rejecting unavailable retries."""
    from docvault.jobs import retry_job

    with session() as db:
        v = require_version(db, version_id)
        kind = "ingest" if v.status == "failed" else "insights"
        job = db.scalar(
            select(Job)
            .where(Job.resource_id == version_id, Job.kind == kind, Job.status == "failed")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        if not job:
            raise AppError(
                409, "retry_unavailable", "There is no failed processing stage to retry."
            )
        identifier = job.id
    retry_job(identifier)
    return {"id": identifier, "status": "queued"}


@router.get("/versions/{version_id}/chunks/{chunk_id}")
def source(version_id: str, chunk_id: str):
    """Return a citation for a chunk belonging to the requested ready document version."""
    with session() as db:
        v = require_version(db, version_id, ready=True)
        chunk = db.get(Chunk, chunk_id)
        if not chunk or chunk.version_id != v.id:
            raise AppError(404, "source_not_found", "Source passage not found.")
        return citation(chunk_evidence(chunk, v))


def make_artifact(kind: str, version_ids: list[str], options: dict):
    """Reuse or queue an artifact for ready sources, restarting previously failed results."""
    version_ids = list(dict.fromkeys(version_ids))
    if kind == "comparison" and len(version_ids) < 2:
        raise AppError(
            422, "comparison_sources", "Choose at least two distinct versions to compare."
        )
    fingerprint = signature(
        ["artifact-v1", kind, version_ids, options, get_settings().openrouter_chat_model]
    )
    with session() as db, db.begin():
        require_versions(db, version_ids)
        advisory_lock(db, f"artifact:{fingerprint}")
        artifact = db.scalar(select(Artifact).where(Artifact.signature == fingerprint))
        if artifact and artifact.status == "failed":
            artifact.status, artifact.error = "pending", None
            db.add(Job(kind=kind, resource_id=artifact.id))
        elif not artifact:
            artifact = Artifact(
                kind=kind, signature=fingerprint, version_ids=version_ids, options=options
            )
            db.add(artifact)
            db.flush()
            db.add(Job(kind=kind, resource_id=artifact.id))
        result = {"id": artifact.id, "status": artifact.status}
    notify_change()
    return result


@router.post("/versions/{version_id}/summaries", status_code=202)
def summarize(version_id: str, body: SummaryCreate):
    """Accept or reuse a customized summary job for a ready version."""
    return make_artifact("summary", [version_id], body.model_dump())


@router.post("/comparisons", status_code=202)
def compare(body: ComparisonCreate):
    """Accept or reuse a comparison job for at least two distinct ready versions."""
    return make_artifact("comparison", body.version_ids, {"dimensions": body.dimensions})


@router.get("/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    """Return persisted result state and data after revalidating its source versions."""
    with session() as db:
        item = db.get(Artifact, artifact_id)
        if not item:
            raise AppError(404, "artifact_not_found", "Result not found.")
        require_versions(db, item.version_ids)
        return {"id": item.id, "status": item.status, "data": item.data, "error": item.error}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str):
    """Return every persisted field of a processing job, or raise if it does not exist."""
    with session() as db:
        job = db.get(Job, job_id)
        if not job:
            raise AppError(404, "job_not_found", "Processing job not found.")
        return {column.name: getattr(job, column.name) for column in job.__table__.columns}
