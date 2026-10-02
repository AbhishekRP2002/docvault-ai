"""PostgreSQL owns job intent, execution leases, fencing, and retry decisions.

RQ transports at-least-once deliveries. A provider call can be billed twice if a
worker dies after the provider succeeds but before its result commits.
"""

import asyncio
import logging
import threading
from datetime import timedelta

from redis.exceptions import RedisError
from rq import Queue
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from docvault.ai.provider import ProviderError
from docvault.cache import notify_change, redis_client
from docvault.config import get_settings
from docvault.db import session
from docvault.errors import AppError
from docvault.models import Artifact, Document, Job, JobAttempt, Message, Version, now

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class LostClaim(RuntimeError):
    """This runner no longer owns the right to write results."""


class SourceDeleted(RuntimeError):
    pass


def _document_ids(db, job: Job) -> list[str]:
    if job.kind == "cleanup":
        return [job.resource_id]
    if job.kind in {"ingest", "insights"}:
        version = db.get(Version, job.resource_id)
        if not version:
            raise SourceDeleted("The document version no longer exists.")
        return [version.document_id]
    artifact = db.get(Artifact, job.resource_id)
    if not artifact:
        raise SourceDeleted("The artifact no longer exists.")
    return list(db.scalars(select(Version.document_id).where(Version.id.in_(artifact.version_ids))))


def job_checkpoint(db, job_id: str, token: int, stage: str | None = None) -> Job:
    """Call inside a transaction before every stage/result mutation.

    Lock documents before the job, matching the deletion path. Keeping the
    source locks until commit prevents cleanup racing a result publication.
    """
    candidate = db.get(Job, job_id)
    if candidate is None:
        raise LostClaim("The job no longer exists.")
    ids = _document_ids(db, candidate)
    documents = list(
        db.scalars(
            select(Document).where(Document.id.in_(ids)).order_by(Document.id).with_for_update()
        )
    )
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not job or job.status != "running" or job.token != token:
        raise LostClaim("Another runner owns this job.")
    if job.lease_until is None or job.lease_until <= now():
        raise LostClaim("The execution lease expired.")
    if job.kind != "cleanup" and (
        len(documents) != len(set(ids)) or any(doc.deleted_at for doc in documents)
    ):
        raise SourceDeleted("A source document was deleted.")
    if stage:
        job.stage = stage
        attempt = db.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == job.id,
                JobAttempt.attempt == job.attempts,
                JobAttempt.status == "running",
            )
        )
        if attempt:
            attempt.stage = stage
    return job


def _resource_state(db, job: Job, state: str, error: str | None = None) -> None:
    if job.kind in {"ingest", "insights"}:
        version = db.get(Version, job.resource_id)
        if version is None or version.status == "deleted":
            return
        if job.kind == "ingest":
            if version.status != "ready":
                version.status = state
                version.error = error
        else:
            version.insight_status = "pending" if state == "queued" else state
            version.insight_error = error
    elif job.kind in {"summary", "comparison"}:
        artifact = db.get(Artifact, job.resource_id)
        if artifact:
            artifact.status = "pending" if state == "queued" else state
            artifact.error = error


def _end_attempt(db, job: Job, status: str, error: str | None = None) -> None:
    attempt = db.scalar(
        select(JobAttempt).where(
            JobAttempt.job_id == job.id,
            JobAttempt.attempt == job.attempts,
            JobAttempt.status == "running",
        )
    )
    if attempt:
        attempt.status, attempt.error, attempt.finished_at = status, error, now()
        attempt.stage = job.stage


def _claim(job_id: str) -> int | None:
    with session() as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status not in {"queued", "enqueued"} or job.next_at > now():
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.status, job.error = "failed", "The maximum number of job attempts was reached."
            _resource_state(db, job, "failed", job.error)
            return None
        job.status = "running"
        job.attempts += 1
        job.token += 1
        job.started_at = now()
        job.finished_at = None
        job.error = None
        job.lease_until = now() + timedelta(seconds=get_settings().lease_seconds)
        db.add(JobAttempt(job_id=job.id, attempt=job.attempts, stage=job.stage))
        return job.token


def _heartbeat(job_id: str, token: int, stop: threading.Event) -> None:
    interval = max(1, min(30, get_settings().lease_seconds / 3))
    while not stop.wait(interval):
        try:
            with session() as db, db.begin():
                result = db.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.token == token,
                        Job.status == "running",
                    )
                    .values(lease_until=now() + timedelta(seconds=get_settings().lease_seconds))
                )
                if result.rowcount != 1:
                    return
        except OperationalError:
            log.warning("Job heartbeat unavailable for job %s", job_id)


def _fail(job_id: str, token: int, exc: Exception) -> None:
    if isinstance(exc, LostClaim):
        return
    cancelled = isinstance(exc, SourceDeleted)
    retryable = isinstance(exc, (OperationalError, RedisError, TimeoutError, ConnectionError)) or (
        isinstance(exc, (ProviderError, AppError)) and exc.retryable
    )
    if cancelled:
        message = "A source document was deleted."
    elif isinstance(exc, (ProviderError, AppError, ValueError)):
        message = str(exc)[:1000]
    else:
        message = "Processing failed. Check the parser configuration or retry the job."
    with session() as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.token != token or job.status != "running":
            return
        _end_attempt(db, job, "cancelled" if cancelled else "failed", message)
        retry = retryable and job.attempts < MAX_ATTEMPTS
        job.status = "cancelled" if cancelled else "queued" if retry else "failed"
        job.error = message
        job.lease_until = None
        job.finished_at = None if retry else now()
        if retry:
            job.next_at = now() + timedelta(seconds=2**job.attempts * 2)
        _resource_state(db, job, job.status, None if retry else message)
    log.warning("Job %s ended with %s", job_id, type(exc).__name__)


def run_job(job_id: str) -> None:
    """Synchronous RQ entry point; duplicate deliveries safely return without work."""
    token = _claim(job_id)
    if token is None:
        return
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(job_id, token, stop), daemon=True)
    heartbeat.start()
    notify_change()
    try:
        from docvault.processing import process_job

        asyncio.run(process_job(job_id, token))
        with session() as db, db.begin():
            job = job_checkpoint(db, job_id, token)
            job.status, job.stage, job.finished_at, job.lease_until = (
                "complete",
                "complete",
                now(),
                None,
            )
            _end_attempt(db, job, "complete")
    except Exception as exc:
        _fail(job_id, token, exc)
    finally:
        stop.set()
        heartbeat.join(timeout=5)
        notify_change()


def dispatch_once() -> int:
    """Recover expired claims, commit dispatch leases, then publish queue deliveries."""
    timestamp = now()
    with session() as db, db.begin():
        recovered_messages = db.execute(
            update(Message)
            .where(
                Message.role == "assistant",
                Message.status.in_(["pending", "streaming"]),
                Message.updated_at
                < timestamp
                - timedelta(
                    seconds=get_settings().generation_timeout_seconds,
                ),
            )
            .values(
                status="failed",
                updated_at=timestamp,
                error="The previous generation stopped responding. You can retry this response.",
            )
        ).rowcount
        abandoned = list(
            db.scalars(
                select(Job)
                .where(
                    Job.status.in_(["running", "enqueued"]),
                    Job.lease_until <= timestamp,
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for job in abandoned:
            job.token += 1  # Fence a stalled runner before making the job eligible again.
            _end_attempt(db, job, "failed", "The previous worker lease expired.")
            exhausted = job.attempts >= MAX_ATTEMPTS
            job.status = "failed" if exhausted else "queued"
            job.error = "The previous worker lease expired."
            job.lease_until = None
            job.next_at = timestamp
            job.finished_at = timestamp if exhausted else None
            _resource_state(db, job, job.status, job.error if exhausted else None)
        pending = list(
            db.scalars(
                select(Job)
                .where(
                    Job.status == "queued",
                    Job.next_at <= timestamp,
                )
                .order_by(Job.created_at)
                .with_for_update(skip_locked=True)
                .limit(50)
            )
        )
        deliveries = []
        for job in pending:
            job.dispatches += 1
            job.status = "enqueued"
            job.lease_until = timestamp + timedelta(seconds=get_settings().lease_seconds)
            deliveries.append((job.id, job.attempts + 1, job.dispatches))
    queue = Queue("docvault", connection=redis_client())
    count = 0
    for job_id, attempt, dispatch in deliveries:
        try:
            queue.enqueue(
                "docvault.jobs.run_job",
                job_id,
                job_id=f"docvault-{job_id}-{attempt}-{dispatch}",
                job_timeout=get_settings().job_timeout_seconds,
                result_ttl=3600,
                failure_ttl=86400,
            )
            count += 1
        except RedisError:
            with session() as db, db.begin():
                db.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == "enqueued",
                        Job.dispatches == dispatch,
                    )
                    .values(status="queued", lease_until=None, next_at=now() + timedelta(seconds=5))
                )
    if deliveries or abandoned or recovered_messages:
        notify_change()
    return count


def retry_job(job_id: str) -> None:
    with session() as db, db.begin():
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job:
            raise AppError(404, "job_not_found", "Job not found.")
        if job.status != "failed":
            raise AppError(409, "job_not_failed", "Only a failed job can be retried.")
        documents = list(
            db.scalars(select(Document).where(Document.id.in_(_document_ids(db, job))))
        )
        if job.kind != "cleanup" and any(doc.deleted_at for doc in documents):
            raise AppError(404, "document_not_found", "The source document was deleted.")
        job.status, job.stage, job.error = "queued", "queued", None
        job.attempts, job.token = 0, job.token + 1
        job.next_at, job.lease_until, job.finished_at = now(), None, None
        _resource_state(db, job, "queued")
    notify_change()
