"""Processing timelines and dead-letter replay in disposable PostgreSQL schemas."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import select
from support import require_persisted_row
from test_processing import FakeLLM, seed_version
from test_processing import isolated_db as isolated_db
from test_processing import pytestmark as pytestmark

from docvault import db as database
from docvault import jobs, parsing, processing
from docvault.errors import AppError
from docvault.main import app
from docvault.models import Document, Job, JobAttempt, JobStageRun, now


@pytest.fixture
def client(isolated_db, monkeypatch):
    """Use the isolated engine and deterministic provider; never contact OpenRouter."""
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: FakeLLM())
    monkeypatch.setattr(jobs, "notify_change", lambda: None)
    with TestClient(app) as client:
        yield client


def test_completed_ingestion_has_persisted_stage_timeline(client):
    """Expose actual measured stages and keep insights a separate durable job."""
    _, version_id, job_id = seed_version()
    jobs.run_job(job_id)
    response = client.get(f"/v1/versions/{version_id}/diagnostics")
    assert response.status_code == 200
    items = response.json()["items"]
    ingestion = next(item for item in items if item["id"] == job_id)
    assert ingestion["status"] == "complete" and not ingestion["retry_eligible"]
    stages = ingestion["history"][0]["stages"]
    assert [stage["stage"] for stage in stages] == [
        "conversion",
        "chunking",
        "persisting",
        "embedding",
        "activation",
    ]
    assert all(stage["status"] == "complete" and stage["duration_ms"] >= 0 for stage in stages)
    assert any(item["kind"] == "insights" and item["status"] == "queued" for item in items)
    assert client.get(f"/v1/jobs/{job_id}/diagnostics").json() == ingestion


def test_chunking_failure_dead_letter_and_manual_replay_keep_history(client, monkeypatch):
    """Retain classified failure and old timeline across a successful manual retry."""
    _, version_id, job_id = seed_version()
    original = parsing.parse_text_document

    def fail(*args, **kwargs):
        """Simulate a native chunker exception carrying confidential content."""
        raise ValueError("CONFIDENTIAL SOURCE CONTENT")

    monkeypatch.setattr(parsing, "parse_text_document", fail)
    jobs.run_job(job_id)
    error = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
    assert error["dead_letter"] and error["retry_eligible"]
    assert error["error"]["code"] == "chunking_failed"
    assert "CONFIDENTIAL" not in str(error)
    assert [stage["status"] for stage in error["history"][0]["stages"]] == ["complete", "failed"]
    assert client.get("/v1/diagnostics/dead-letters").json()["total"] == 1
    monkeypatch.setattr(parsing, "parse_text_document", original)
    replay = client.post(f"/v1/jobs/{job_id}/retry")
    assert replay.status_code == 202 and replay.json()["status"] == "queued"
    queued = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
    assert queued["started_at"] is None and queued["finished_at"] is None
    assert queued["history"][0]["started_at"] == error["history"][0]["started_at"]
    assert client.post(f"/v1/jobs/{job_id}/retry").status_code == 409
    jobs.run_job(job_id)
    result = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
    assert result["status"] == "complete" and not result["dead_letter"]
    assert len(result["history"]) == 2
    assert result["history"][1]["error"]["code"] == "chunking_failed"
    assert result["history"][0]["id"] != result["history"][1]["id"]
    assert client.get("/v1/diagnostics/dead-letters").json()["total"] == 0


def test_transient_errors_enter_dead_letters_only_after_retry_exhaustion(client, monkeypatch):
    """SQL retries alone own admission; terminal transient failures keep their classification."""
    _, _, job_id = seed_version()

    def fail_parse(*args):
        """Simulate a temporary dependency outage without calling a provider."""
        raise TimeoutError("secret network details")

    monkeypatch.setattr(processing, "parse_document_file", fail_parse)
    for attempt in range(1, 4):
        jobs.run_job(job_id)
        payload = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
        assert payload["attempts"] == attempt
        assert payload["dead_letter"] == (attempt == 3)
        if attempt < 3:
            assert payload["queue_state"] == "retry_waiting" and payload["next_retry_at"]
            with database.session() as db, db.begin():
                require_persisted_row(db, Job, job_id).next_at = now() - timedelta(seconds=1)
    assert len(payload["history"]) == 3
    assert payload["error"]["code"] == "dependency_unavailable"
    assert payload["error"]["retryable"] is True


def test_lease_recovery_closes_running_stage_as_interrupted(client, monkeypatch):
    """A dead worker leaves recoverable evidence rather than a forever-running timeline."""
    _, _, job_id = seed_version()
    token = jobs._claim_pending_job(job_id)
    assert token is not None
    with database.session() as db, db.begin():
        job = require_persisted_row(db, Job, job_id)
        job.stage, job.lease_until = "conversion", now() - timedelta(seconds=1)
        attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
        assert attempt is not None
        db.add(JobStageRun(attempt_id=attempt.id, stage="conversion"))

    class Queue:
        """Capture delivery without writing to application Redis."""

        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            pass

    monkeypatch.setattr(jobs, "Queue", Queue)
    assert jobs.dispatch_pending_jobs() == 1
    payload = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
    assert payload["history"][0]["error"]["code"] == "lease_expired"
    assert payload["history"][0]["stages"][0]["status"] == "interrupted"
    assert payload["history"][0]["stages"][0]["finished_at"]


def test_deleted_sources_disable_replay_but_retain_failure_evidence(client):
    """Deletion cannot resurrect a failed job or make its original version accessible."""
    document_id, version_id, job_id = seed_version()
    with database.session() as db, db.begin():
        require_persisted_row(db, Job, job_id).status = "failed"
        require_persisted_row(db, Document, document_id).deleted_at = now()
    assert not client.get(f"/v1/jobs/{job_id}/diagnostics").json()["retry_eligible"]
    assert client.post(f"/v1/jobs/{job_id}/retry").status_code == 404
    assert client.get(f"/v1/versions/{version_id}/diagnostics").status_code == 404
    assert client.get("/v1/diagnostics/dead-letters").json()["total"] == 1


def test_concurrent_replays_claim_one_sql_transition(client):
    """Two manual replays cannot both queue the same terminal job."""
    _, _, job_id = seed_version()
    with database.session() as db, db.begin():
        require_persisted_row(db, Job, job_id).status = "failed"

    def retry():
        """Return the accepted transition or its existing conflict code."""
        try:
            jobs.retry_job(job_id)
            return 202
        except AppError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: retry(), range(2))) == [202, 409]
    with database.session() as db:
        assert require_persisted_row(db, Job, job_id).token == 1


def test_historical_attempts_are_unmeasured_and_history_is_bounded(client):
    """Do not fabricate old stage measurements or return unbounded replay history."""
    _, _, job_id = seed_version()
    with database.session() as db, db.begin():
        db.add_all(
            [
                JobAttempt(
                    job_id=job_id,
                    attempt=1,
                    status="failed",
                    error="Old error.",
                    started_at=now() - timedelta(seconds=index),
                )
                for index in range(102)
            ]
        )
    payload = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
    assert len(payload["history"]) == 100 and payload["history_truncated"]
    assert all(
        item["stages"] == [] and item["error"]["code"] == "legacy_failure"
        for item in payload["history"]
    )
    assert client.get("/v1/diagnostics/dead-letters?limit=0").status_code == 422
    assert client.get("/v1/diagnostics/dead-letters?offset=-1").status_code == 422
    assert client.get("/v1/jobs/missing/diagnostics").status_code == 404
    assert client.post("/v1/jobs/missing/retry").status_code == 404


def test_dispatcher_publication_failure_restores_durable_queue_and_reports_failure(
    client, monkeypatch
):
    """A completed poll with failed publication must not be advertised as healthy."""
    _, _, job_id = seed_version()

    class Queue:
        """Simulate Redis failing after the SQL dispatch intent committed."""

        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            raise RedisError("unavailable")

    monkeypatch.setattr(jobs, "Queue", Queue)
    with pytest.raises(RedisError):
        jobs.dispatch_pending_jobs(raise_publish_errors=True)
    with database.session() as db:
        job = require_persisted_row(db, Job, job_id)
        assert job.status == "queued" and job.lease_until is None
        assert job.attempts == 0 and job.next_at > now()


def test_source_deletion_cancels_running_attempt_and_stage_without_dead_letter(client):
    """Cancellation remains separate from terminal processing failures."""
    document_id, _, job_id = seed_version()
    token = jobs._claim_pending_job(job_id)
    assert token is not None
    with database.session() as db, db.begin():
        attempt = db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
        assert attempt is not None
        db.add(JobStageRun(attempt_id=attempt.id, stage="conversion"))
        require_persisted_row(db, Document, document_id).deleted_at = now()
    jobs._handle_job_failure(job_id, token, jobs.SourceDeleted("deleted"))
    result = client.get(f"/v1/jobs/{job_id}/diagnostics").json()
    assert result["status"] == "cancelled" and not result["dead_letter"]
    assert result["history"][0]["stages"][0]["status"] == "cancelled"
    assert client.get("/v1/diagnostics/dead-letters").json()["total"] == 0


def test_rq_delivery_success_does_not_hide_application_dead_letter(client, monkeypatch):
    """Verify the actual transport boundary: a handled failure is still a SQL dead letter."""
    from rq import Queue, SimpleWorker
    from rq.registry import FailedJobRegistry

    from docvault.cache import redis_client

    _, _, job_id = seed_version()

    def fail(*args, **kwargs):
        """Return a deterministic terminal native parser failure."""
        raise ValueError("invalid content")

    monkeypatch.setattr(parsing, "parse_text_document", fail)
    queue = Queue("diagnostics-" + uuid4().hex, connection=redis_client())
    delivery = queue.enqueue("docvault.jobs.run_job", job_id)
    try:
        SimpleWorker([queue], connection=redis_client()).work(burst=True, logging_level="WARNING")
        assert delivery.get_status(refresh=True).value == "finished"
        assert FailedJobRegistry(queue=queue).get_job_count(cleanup=False) == 0
        assert client.get(f"/v1/jobs/{job_id}/diagnostics").json()["dead_letter"]
    finally:
        delivery.delete()
        queue.delete(delete_jobs=True)
