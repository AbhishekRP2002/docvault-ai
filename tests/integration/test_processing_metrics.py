"""Persisted monitoring snapshots in disposable PostgreSQL schemas, without real consumers."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_processing import isolated_db as isolated_db
from test_processing import pytestmark as pytestmark

from docvault.api import metrics
from docvault.db import session
from docvault.models import Job, JobAttempt, JobStageRun

NOW = datetime(2026, 10, 5, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def measured_clock_and_consumers(monkeypatch):
    """Use one observation time and an independent fake consumer boundary."""

    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr(metrics, "datetime", Clock)
    monkeypatch.setattr(
        metrics,
        "_read_consumer_metrics",
        lambda: dict(healthy_workers=3, busy_workers=2, healthy_dispatchers=1),
    )


def record_job(status, *, due=NOW, attempts=0, lease=None, started=None, finished=None):
    """Persist a job snapshot without launching processing or touching application data."""
    with session() as db, db.begin():
        job = Job(
            kind="ingest",
            resource_id=str(uuid4()),
            status=status,
            next_at=due,
            attempts=attempts,
            lease_until=lease,
            started_at=started,
            finished_at=finished,
        )
        db.add(job)
        db.flush()
        return job.id


def record_stage(
    job_id, *, stage="conversion", status="complete", seconds=1, finished: datetime | None = NOW
):
    """Persist one attempt and a measured stage; nullable completion represents active work."""
    with session() as db, db.begin():
        attempt = JobAttempt(job_id=job_id, attempt=1, started_at=NOW)
        db.add(attempt)
        db.flush()
        db.add(
            JobStageRun(
                attempt_id=attempt.id,
                stage=stage,
                status=status,
                started_at=(finished or NOW) - timedelta(seconds=seconds),
                finished_at=finished,
            )
        )


def test_empty_processing_has_unknown_durations_not_fake_zero(isolated_db):
    data = metrics.get_processing_metrics()
    for key in [
        "completed",
        "failed",
        "dead_letters",
        "active",
        "queued",
        "due_queued",
        "retry_waiting",
        "enqueued",
        "expired_leases",
        "duration_sample_count",
        "automatic_retry_attempts",
    ]:
        assert data[key] == 0
    for key in [
        "average_duration_ms",
        "p50_duration_ms",
        "p95_duration_ms",
        "oldest_due_job_age_seconds",
    ]:
        assert data[key] is None
    assert data["stage_durations"] == []
    assert data["healthy_workers"] == 3 and data["busy_workers"] == 2


def test_backlog_lease_and_dead_letter_counts_follow_current_state(isolated_db):
    record_job("queued", due=NOW - timedelta(seconds=40))
    record_job("queued", due=NOW)  # Due boundary is inclusive.
    record_job("queued", due=NOW + timedelta(seconds=20), attempts=1)
    record_job("queued", due=NOW + timedelta(seconds=20))  # Scheduled first attempt, not a retry.
    record_job("enqueued", due=NOW - timedelta(seconds=90), lease=NOW)
    record_job("enqueued", lease=NOW + timedelta(seconds=1))
    record_job("running", lease=NOW - timedelta(seconds=1))
    record_job("running", lease=None)  # Historical missing leases are not invented.
    record_job("cancelled", lease=NOW - timedelta(days=1))
    record_job("failed")
    record_job("failed")  # Retained historical failures still belong to the DLQ.
    data = metrics.get_processing_metrics()
    assert data["queued"] == 6
    assert data["due_queued"] == 2
    assert data["retry_waiting"] == 1
    assert data["enqueued"] == 2
    assert data["active"] == 2
    assert data["expired_leases"] == 2
    assert data["oldest_due_job_age_seconds"] == 90
    assert data["dead_letters"] == data["failed"] == 2


def test_completed_job_duration_retains_lifetime_scope_and_counts_only_valid_samples(isolated_db):
    for seconds in [1, 3, 5]:
        record_job("complete", started=NOW - timedelta(seconds=seconds), finished=NOW)
    record_job(
        "complete", started=NOW - timedelta(days=100, seconds=7), finished=NOW - timedelta(days=100)
    )
    record_job("complete")  # Legacy unknown timestamps.
    record_job("complete", started=NOW, finished=NOW - timedelta(seconds=1))
    record_job("complete", started=NOW, finished=NOW + timedelta(seconds=1))
    record_job("failed", started=NOW - timedelta(seconds=500), finished=NOW)
    data = metrics.get_processing_metrics(days=1)
    assert data["completed"] == 7
    assert data["duration_sample_count"] == 4
    assert data["average_duration_ms"] == pytest.approx(4000)
    assert data["p50_duration_ms"] == pytest.approx(4000)
    assert data["p95_duration_ms"] == pytest.approx(6700)


def test_stage_samples_and_automatic_retries_respect_window_and_outcomes(isolated_db):
    job_id = record_job("complete")
    for seconds in [1, 3, 5]:
        record_stage(job_id, seconds=seconds)
    record_stage(job_id, stage="chunking", seconds=2, finished=NOW - timedelta(days=2))
    record_stage(job_id, finished=NOW - timedelta(days=2, microseconds=1))
    record_stage(job_id, finished=NOW + timedelta(microseconds=1))
    record_stage(job_id, seconds=-1)
    for status in ["running", "failed", "cancelled", "interrupted"]:
        record_stage(job_id, status=status, finished=None if status == "running" else NOW)
    with session() as db, db.begin():
        for attempt, at in [
            (1, NOW),
            (2, NOW),
            (3, NOW - timedelta(days=2)),
            (2, NOW - timedelta(days=2, microseconds=1)),
            (2, NOW + timedelta(microseconds=1)),
        ]:
            db.add(JobAttempt(job_id=job_id, attempt=attempt, started_at=at))
    data = metrics.get_processing_metrics(days=2)
    assert data["automatic_retry_attempts"] == 2
    assert data["window_start"] == (NOW - timedelta(days=2)).isoformat()
    assert data["window_end"] == NOW.isoformat()
    assert data["stage_durations"] == [
        dict(stage="chunking", sample_count=1, p50_ms=2000, p95_ms=2000),
        dict(stage="conversion", sample_count=3, p50_ms=3000, p95_ms=4800),
    ]


@pytest.mark.parametrize("days", ["0", "91", "nope"])
def test_processing_rejects_unbounded_window(isolated_db, days):
    app = FastAPI()
    app.include_router(metrics.router)
    with TestClient(app) as client:
        assert client.get(f"/v1/metrics/processing?days={days}").status_code == 422
