"""Bounded read-only processing diagnostics and explicitly requested dead-letter replay."""

from datetime import datetime
from enum import StrEnum

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docvault.db import session
from docvault.diagnostics import ProcessingError, build_processing_error
from docvault.documents import require_version
from docvault.errors import AppError
from docvault.jobs import SourceDeleted, _resolve_job_document_ids, retry_job
from docvault.models import Document, Job, JobAttempt, JobStageRun, now

router = APIRouter(prefix="/v1", tags=["diagnostics"])


class StageDiagnostic(BaseModel):
    """Measured stage timestamps; unfinished durations remain unknown."""

    id: str
    stage: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None


class AttemptDiagnostic(BaseModel):
    """An immutable attempt identity, including its measured stages and final error."""

    id: str
    attempt: int
    status: str
    stage: str
    started_at: datetime
    finished_at: datetime | None
    error: ProcessingError | None
    stages: list[StageDiagnostic]


class JobQueueState(StrEnum):
    """Validated public queue states from canonical job state and retry scheduling."""

    queued = "queued"
    retry_waiting = "retry_waiting"
    enqueued = "enqueued"
    running = "running"
    complete = "complete"
    failed = "failed"
    cancelled = "cancelled"


class JobDiagnostic(BaseModel):
    """Canonical job state and retry eligibility, independent of RQ transport status."""

    id: str
    kind: str
    resource_id: str
    status: str
    stage: str
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_retry_at: datetime | None
    lease_until: datetime | None
    queue_state: JobQueueState
    retry_eligible: bool
    error: ProcessingError | None
    dead_letter: bool
    history: list[AttemptDiagnostic]
    history_truncated: bool


class JobDiagnosticsPage(BaseModel):
    """Paginated jobs; totals count canonical jobs, not RQ delivery records."""

    items: list[JobDiagnostic]
    total: int


def _can_retry_job(db: Session, job: Job) -> bool:
    """Require failed work with available sources; replay still rechecks transactionally."""
    if job.status != "failed":
        return False
    try:
        ids = _resolve_job_document_ids(db, job)
    except SourceDeleted:
        return False
    documents = list(db.scalars(select(Document).where(Document.id.in_(ids))))
    return len(documents) == len(set(ids)) and (
        job.kind == "cleanup" or all(document.deleted_at is None for document in documents)
    )


def serialize_job_diagnostics(
    db: Session, jobs: list[Job], *, history: bool = True
) -> list[JobDiagnostic]:
    """Load bounded attempt/stage history in bulk, keeping old unmeasured stages empty."""
    attempts_by_job: dict[str, list[JobAttempt]] = {}
    stages_by_attempt: dict[str, list[StageDiagnostic]] = {}
    truncated: set[str] = set()
    if history and jobs:
        ranked = (
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
        attempts = list(
            db.scalars(
                select(JobAttempt)
                .join(ranked, JobAttempt.id == ranked.c.id)
                .where(ranked.c.rank <= 101)
                .order_by(JobAttempt.started_at.desc(), JobAttempt.id.desc())
            )
        )
        for attempt in attempts:
            items = attempts_by_job.setdefault(attempt.job_id, [])
            if len(items) == 100:
                truncated.add(attempt.job_id)
            else:
                items.append(attempt)
        ids = [a.id for group in attempts_by_job.values() for a in group]
        for stage in db.scalars(
            select(JobStageRun)
            .where(JobStageRun.attempt_id.in_(ids))
            .order_by(JobStageRun.started_at, JobStageRun.id)
        ):
            duration = (
                max(0.0, (stage.finished_at - stage.started_at).total_seconds() * 1000)
                if stage.finished_at is not None
                else None
            )
            stages_by_attempt.setdefault(stage.attempt_id, []).append(
                StageDiagnostic(
                    id=stage.id,
                    stage=stage.stage,
                    status=stage.status,
                    started_at=stage.started_at,
                    finished_at=stage.finished_at,
                    duration_ms=duration,
                )
            )
    timestamp = now()
    return [
        JobDiagnostic(
            id=job.id,
            kind=job.kind,
            resource_id=job.resource_id,
            status=job.status,
            stage=job.stage,
            attempts=job.attempts,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            next_retry_at=job.next_at
            if job.status == "queued" and job.next_at > timestamp
            else None,
            lease_until=job.lease_until,
            queue_state=JobQueueState(
                "retry_waiting"
                if job.status == "queued" and job.next_at > timestamp
                else job.status
            ),
            retry_eligible=_can_retry_job(db, job),
            dead_letter=job.status == "failed",
            error=build_processing_error(job.error_code, job.error, job.error_retryable),
            history=[
                AttemptDiagnostic(
                    id=attempt.id,
                    attempt=attempt.attempt,
                    status=attempt.status,
                    stage=attempt.stage,
                    started_at=attempt.started_at,
                    finished_at=attempt.finished_at,
                    error=build_processing_error(
                        attempt.error_code, attempt.error, attempt.error_retryable
                    ),
                    stages=stages_by_attempt.get(attempt.id, []),
                )
                for attempt in attempts_by_job.get(job.id, [])
            ],
            history_truncated=job.id in truncated,
        )
        for job in jobs
    ]


@router.get("/jobs/{job_id}/diagnostics", response_model=JobDiagnostic)
def get_job_diagnostics(job_id: str):
    """Return canonical state and the newest 100 attempts for one retained job."""
    with session() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise AppError(404, "job_not_found", "Processing job not found.")
        return serialize_job_diagnostics(db, [job])[0]


@router.get("/versions/{version_id}/diagnostics", response_model=JobDiagnosticsPage)
def get_version_diagnostics(
    version_id: str, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)
):
    """Show a live version's ingestion/insight jobs with original attempt identities."""
    with session() as db:
        require_version(db, version_id)
        filters = (
            Job.resource_id == version_id,
            Job.kind.in_(["ingest", "insights", "overview_index"]),
        )
        total = db.scalar(select(func.count()).select_from(Job).where(*filters)) or 0
        jobs = list(
            db.scalars(
                select(Job)
                .where(*filters)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return JobDiagnosticsPage(items=serialize_job_diagnostics(db, jobs), total=total)


@router.get("/diagnostics/dead-letters", response_model=JobDiagnosticsPage)
def list_dead_letter_jobs(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    """List retained terminal failures without loading unbounded attempt bodies."""
    with session() as db:
        total = db.scalar(select(func.count()).select_from(Job).where(Job.status == "failed")) or 0
        jobs = list(
            db.scalars(
                select(Job)
                .where(Job.status == "failed")
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        return JobDiagnosticsPage(
            items=serialize_job_diagnostics(db, jobs, history=False), total=total
        )


@router.post("/jobs/{job_id}/retry", status_code=202)
def replay_failed_job(job_id: str):
    """Request one SQL-authoritative replay; do not directly requeue an RQ delivery."""
    try:
        retry_job(job_id)
    except SourceDeleted as exc:
        raise AppError(404, "source_unavailable", "The source resource is unavailable.") from exc
    return {"id": job_id, "status": "queued"}
