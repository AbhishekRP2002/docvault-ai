"""Files metadata reports persisted ingestion work without counting queue time."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from support import require_persisted_row
from test_api import api as api
from test_api import ready_source
from test_api import test_database_url as test_database_url

from docvault import documents, jobs
from docvault.db import get_engine, session
from docvault.models import Batch, Document, Job, JobAttempt, Version

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 5, 10, tzinfo=UTC)
START = NOW - timedelta(seconds=20)
FINISH = NOW - timedelta(seconds=5)


@pytest.fixture(autouse=True)
def fixed_metadata_clock(monkeypatch):
    monkeypatch.setattr(documents, "now", lambda: NOW)


def recorded_ingestion(*, status="running", with_attempt=True, finished_at=None):
    source = ready_source()
    with session() as db, db.begin():
        version = require_persisted_row(db, Version, source.version)
        version.status = "ready" if status == "complete" else status
        version.page_count, version.chunk_count, version.token_count = 3, 8, 640
        version.parser, version.embedding_model = "text", "test/embedding"
        job = Job(
            kind="ingest",
            resource_id=source.version,
            status=status,
            attempts=1,
            created_at=START - timedelta(minutes=1),
            started_at=START,
            finished_at=finished_at,
        )
        db.add(job)
        db.flush()
        if with_attempt:
            db.add(
                JobAttempt(
                    job_id=job.id,
                    attempt=1,
                    status=status,
                    started_at=START,
                    finished_at=finished_at,
                )
            )
        return SimpleNamespace(document=source.document, version=source.version, job=job.id)


def get_metadata(api, document_id):
    response = api.client.get(f"/v1/documents/{document_id}")
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_immediately_returns_persisted_ingestion_run_id(api):
    response = api.client.post(
        "/v1/documents",
        files={"file": ("terms.txt", b"Payment is due in thirty days.", "text/plain")},
        headers={"Idempotency-Key": "processing-metadata"},
    )
    assert response.status_code == 202, response.text
    data = response.json()
    with session() as db:
        job = db.scalar(select(Job).where(Job.resource_id == data["latest_version_id"]))
        assert job is not None
        assert data["processing"] == {
            "run_id": job.id,
            "status": "queued",
            "attempts": 0,
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
        }
    assert data["token_count"] == 0
    assert data["parser"] is None
    assert data["embedding_model"] is None
    assert get_metadata(api, data["id"])["processing"] == data["processing"]


@pytest.mark.parametrize("with_attempt", [True, False])
@pytest.mark.parametrize(
    ("status", "finished_at", "duration_ms"),
    [("running", None, 20_000), ("complete", FINISH, 15_000), ("failed", FINISH, 15_000)],
)
def test_latest_attempt_timing_and_document_statistics(
    api, monkeypatch, status, finished_at, duration_ms, with_attempt
):
    source = recorded_ingestion(status=status, finished_at=finished_at, with_attempt=with_attempt)
    data = get_metadata(api, source.document)
    assert data["processing"] == {
        "run_id": source.job,
        "status": status,
        "attempts": 1,
        "started_at": START.isoformat(),
        "finished_at": finished_at.isoformat() if finished_at else None,
        "duration_ms": duration_ms,
    }
    assert (data["page_count"], data["chunk_count"], data["token_count"]) == (3, 8, 640)
    assert (data["parser"], data["embedding_model"]) == ("text", "test/embedding")
    monkeypatch.setattr(documents, "now", lambda: NOW + timedelta(minutes=1))
    later = get_metadata(api, source.document)["processing"]["duration_ms"]
    assert later == (duration_ms + 60_000 if status == "running" else duration_ms)


def test_legacy_document_without_ingestion_does_not_borrow_insight_timing(api):
    source = ready_source()
    with session() as db, db.begin():
        db.add(Job(kind="insights", resource_id=source.version, status="running", started_at=START))
    assert get_metadata(api, source.document)["processing"] is None


@pytest.mark.parametrize("status", ["queued", "failed", "complete"])
def test_missing_finish_time_stays_unknown_instead_of_counting_idle_time(api, status):
    source = recorded_ingestion(status=status, with_attempt=False)
    processing = get_metadata(api, source.document)["processing"]
    assert processing["started_at"] == START.isoformat()
    assert processing["finished_at"] is None
    assert processing["duration_ms"] is None


def test_missing_start_time_stays_unknown(api):
    source = recorded_ingestion(status="complete", finished_at=FINISH, with_attempt=False)
    with session() as db, db.begin():
        require_persisted_row(db, Job, source.job).started_at = None
    processing = get_metadata(api, source.document)["processing"]
    assert processing["started_at"] is None
    assert processing["finished_at"] == FINISH.isoformat()
    assert processing["duration_ms"] is None


def test_metadata_uses_latest_ingest_job_for_the_serialized_version(api):
    source = recorded_ingestion(status="complete", finished_at=FINISH)
    with session() as db, db.begin():
        document = require_persisted_row(db, Document, source.document)
        latest = Version(
            document_id=document.id,
            version_number=2,
            filename="terms-v2.txt",
            mime_type="text/plain",
            size_bytes=20,
            sha256="b" * 64,
            storage_key="sources/new-version.txt",
        )
        db.add(latest)
        db.flush()
        document.latest_version_id = latest.id
        latest_job = Job(kind="ingest", resource_id=latest.id, created_at=NOW)
        db.add_all(
            [
                latest_job,
                Job(kind="ingest", resource_id=latest.id, created_at=START),
                Job(kind="insights", resource_id=latest.id, created_at=NOW + timedelta(seconds=1)),
            ]
        )
        db.flush()
        latest_id, latest_job_id = latest.id, latest_job.id
    data = get_metadata(api, source.document)
    assert data["latest_version_id"] == latest_id
    assert data["processing"]["run_id"] == latest_job_id
    assert data["processing"]["started_at"] is None
    with session() as db:
        supplied = documents.serialize_document_response(
            db,
            require_persisted_row(db, Document, source.document),
            require_persisted_row(db, Version, source.version),
        )
    assert supplied["processing"]["run_id"] == source.job
    assert supplied["processing"]["duration_ms"] == 15_000


def test_automatic_retry_preserves_finished_attempt_time_during_backoff(api, monkeypatch):
    source = recorded_ingestion()
    monkeypatch.setattr(jobs, "now", lambda: FINISH)
    jobs._handle_job_failure(source.job, 0, ConnectionError("Temporary provider failure"))
    with session() as db:
        persisted = require_persisted_row(db, Job, source.job)
        assert persisted.status == "queued"
        assert persisted.finished_at is None
    processing = get_metadata(api, source.document)["processing"]
    assert processing["status"] == "queued"
    assert processing["started_at"] == START.isoformat()
    assert processing["finished_at"] == FINISH.isoformat()
    assert processing["duration_ms"] == 15_000
    monkeypatch.setattr(documents, "now", lambda: NOW + timedelta(minutes=10))
    assert get_metadata(api, source.document)["processing"]["duration_ms"] == 15_000


def test_manual_retry_clears_displayed_timing_until_new_attempt_starts(api):
    source = recorded_ingestion(status="failed", finished_at=FINISH)
    response = api.client.post(f"/v1/versions/{source.version}/retry")
    assert response.status_code == 202, response.text
    with session() as db:
        persisted = require_persisted_row(db, Job, source.job)
        assert persisted.started_at is None and persisted.finished_at is None
        assert persisted.attempts == 0
        previous = db.scalar(select(JobAttempt).where(JobAttempt.job_id == source.job))
        assert previous is not None
        assert previous.started_at == START and previous.finished_at == FINISH
    assert get_metadata(api, source.document)["processing"] == {
        "run_id": source.job,
        "status": "queued",
        "attempts": 0,
        "started_at": None,
        "finished_at": None,
        "duration_ms": None,
    }


def test_new_retry_cycle_chooses_latest_time_despite_duplicate_or_higher_attempt_numbers(api):
    source = recorded_ingestion(status="failed", finished_at=FINISH)
    with session() as db, db.begin():
        job = require_persisted_row(db, Job, source.job)
        job.status, job.attempts, job.started_at, job.finished_at = "running", 1, FINISH, None
        db.add_all(
            [
                JobAttempt(
                    job_id=job.id,
                    attempt=3,
                    status="failed",
                    started_at=START + timedelta(seconds=1),
                    finished_at=START + timedelta(seconds=2),
                ),
                JobAttempt(job_id=job.id, attempt=1, status="running", started_at=FINISH),
            ]
        )
    processing = get_metadata(api, source.document)["processing"]
    assert processing["attempts"] == 1
    assert processing["started_at"] == FINISH.isoformat()
    assert processing["finished_at"] is None
    assert processing["duration_ms"] == 5_000


@pytest.mark.parametrize("resource", ["library", "batch"])
def test_multiple_documents_use_bounded_metadata_queries(api, resource):
    sources = [recorded_ingestion(status="complete", finished_at=FINISH) for _ in range(8)]
    path = "/v1/documents"
    if resource == "batch":
        with session() as db, db.begin():
            batch = Batch(
                items=[
                    {"document_id": source.document, "filename": "terms.txt", "error": None}
                    for source in sources
                ]
                + [{"document_id": None, "filename": "invalid.exe", "error": "Unsupported file"}]
            )
            db.add(batch)
            db.flush()
            path = f"/v1/document-batches/{batch.id}"
    queries = []

    def capture_select(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture_select)
    try:
        response = api.client.get(path)
    finally:
        event.remove(engine, "before_cursor_execute", capture_select)
    assert response.status_code == 200, response.text
    assert len(queries) == (4 if resource == "library" else 5), queries
    items = response.json()["items"]
    if resource == "batch":
        assert items[-1]["document"] is None
        assert items[-1]["error"] == "Unsupported file"
        items = [item["document"] for item in items[:-1]]
    assert len(items) == 8
    assert {item["processing"]["run_id"] for item in items} == {source.job for source in sources}
    assert all(item["processing"]["duration_ms"] == 15_000 for item in items)
