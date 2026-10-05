"""Durable processing timelines and caller-safe failure descriptions."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from docvault.db import session
from docvault.errors import AppError
from docvault.llm.provider import ContextLimitError, ProviderError
from docvault.models import JobAttempt, JobStageRun, now

log = logging.getLogger(__name__)


class ProcessingError(BaseModel):
    """Safe failure information, separate from whether manual replay is currently allowed."""

    code: str
    message: str
    retryable: bool | None
    remediation: str


REMEDIATIONS = {
    "source_deleted": "The source is unavailable. Upload the document again if needed.",
    "lease_expired": "Check worker availability; recovery retries eligible jobs automatically.",
    "attempts_exhausted": "Resolve the failure shown in the attempt history, then retry manually.",
    "parser_dependency_missing": "Install the parsing extra or rebuild the worker image.",
    "storage_unavailable": "Check the worker storage mount and file permissions before retrying.",
    "source_file_missing": "Restore the source file or upload the document again.",
    "context_limit": "Select a model with sufficient context capacity and submit a new request.",
    "generation_configuration_changed": "Submit a new artifact with the current model configuration.",
    "provider_unavailable": "Check provider configuration, connectivity and quota before retrying.",
    "dependency_unavailable": "Check PostgreSQL and Redis availability; eligible jobs retry automatically.",
    "conversion_failed": "Check the PDF/DOCX and worker parser assets; retry after fixing the cause.",
    "chunking_failed": "Check source structure and whether headings/table headers fit the chunk budget.",
    "invalid_processing_input": "Check the source format and request configuration before retrying.",
    "processing_failed": "Check the failed stage and worker logs before retrying.",
    "legacy_failure": "Detailed error classification was not recorded for this historical attempt.",
}


def build_processing_error(
    code: str | None, message: str | None, retryable: bool | None
) -> ProcessingError | None:
    """Describe a recorded error without inventing a classification for historical failures."""
    if not message:
        return None
    code = code or "legacy_failure"
    return ProcessingError(
        code=code,
        message=message,
        retryable=retryable,
        remediation=REMEDIATIONS.get(code, REMEDIATIONS["processing_failed"]),
    )


def classify_processing_failure(exc: Exception, stage: str) -> ProcessingError:
    """Map exceptions to stable safe descriptions without exposing exception payloads."""
    from redis.exceptions import RedisError

    from docvault.jobs import SourceDeleted

    retryable = isinstance(exc, (OperationalError, RedisError, TimeoutError, ConnectionError)) or (
        isinstance(exc, (ProviderError, AppError)) and exc.retryable
    )
    if isinstance(exc, SourceDeleted):
        code, message = "source_deleted", "A source document was deleted."
    elif isinstance(exc, ContextLimitError):
        code, message = "context_limit", "The request exceeds the configured model context."
    elif isinstance(exc, ProviderError):
        code, message = "provider_unavailable", str(exc)
    elif isinstance(exc, (ImportError, ModuleNotFoundError)):
        code, message = "parser_dependency_missing", "Required processing dependencies are missing."
    elif isinstance(exc, FileNotFoundError):
        code, message = "source_file_missing", "A required source file is unavailable."
    elif isinstance(exc, PermissionError):
        code, message = "storage_unavailable", "Processing could not access the configured storage."
    elif isinstance(exc, (OperationalError, RedisError, TimeoutError, ConnectionError)):
        code, message = (
            "dependency_unavailable",
            "A processing dependency is unavailable or timed out.",
        )
    elif isinstance(exc, AppError):
        code, message = exc.code, exc.message
    elif stage == "conversion":
        code, message = "conversion_failed", "Document conversion did not complete."
    elif stage == "chunking":
        code, message = (
            "chunking_failed",
            "Document chunking could not preserve the required content.",
        )
    elif isinstance(exc, ValueError):
        code, message = "invalid_processing_input", "Processing input or configuration is invalid."
    else:
        code, message = "processing_failed", "Processing failed unexpectedly."
    return ProcessingError(
        code=code,
        message=message[:1000],
        retryable=retryable,
        remediation=REMEDIATIONS.get(code, REMEDIATIONS["processing_failed"]),
    )


def finish_attempt_stages(db, attempt_id: str, status: str, timestamp: datetime) -> None:
    """Close all still-running stages when failure, deletion or recovery ends an attempt."""
    from sqlalchemy import update

    db.execute(
        update(JobStageRun)
        .where(JobStageRun.attempt_id == attempt_id, JobStageRun.status == "running")
        .values(status=status, finished_at=timestamp)
    )


@contextmanager
def record_job_stage(job_id: str, token: int, stage: str) -> Iterator[None]:
    """Record a stage under the execution claim and fence late timeline writes."""
    from docvault.jobs import LostClaim, job_checkpoint

    with session() as db, db.begin():
        job_checkpoint(db, job_id, token, stage)
        attempt = db.scalar(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id, JobAttempt.status == "running")
            .order_by(JobAttempt.started_at.desc(), JobAttempt.id.desc())
            .limit(1)
        )
        if attempt is None:
            raise LostClaim("The running attempt no longer exists.")
        row = JobStageRun(attempt_id=attempt.id, stage=stage)
        db.add(row)
        db.flush()
        identifier = row.id
    failed = False
    try:
        yield
    except Exception:
        failed = True
        raise
    finally:
        try:
            with session() as db, db.begin():
                job_checkpoint(db, job_id, token)
                row = db.get(JobStageRun, identifier)
                if row is not None and row.status == "running":
                    row.status, row.finished_at = "failed" if failed else "complete", now()
        except LostClaim:
            pass  # Recovery/deletion owns finalizing the old attempt and its stages.
        except OperationalError:
            if not failed:
                raise
            log.warning("Stage finalization unavailable for job %s", job_id)
