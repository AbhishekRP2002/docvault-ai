"""Run with RUN_INTEGRATION=1 against the dedicated local docvault_test database.

Each test owns a random PostgreSQL schema and temporary storage directory. No
application data is reset. Model calls are deterministic; PostgreSQL is real.
"""

import asyncio
import hashlib
import os
import threading
import time
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text

from docvault import cache, config, jobs, processing
from docvault import db as database
from docvault.ai.insights import DocumentInsights, KeyInsight
from docvault.ai.provider import ProviderError
from docvault.models import Artifact, Chat, Chunk, Document, Job, JobAttempt, Message, Version, now

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Set RUN_INTEGRATION=1 for local PostgreSQL tests.",
    ),
]


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    url = os.getenv(
        "TEST_DATABASE_URL", "postgresql+psycopg://docvault:docvault@localhost:15432/docvault_test"
    )
    if not url.split("?", 1)[0].endswith("/docvault_test"):
        pytest.fail("Integration tests require the dedicated docvault_test database.")
    schema = "processing_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    database.Base.metadata.create_all(engine)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:16379/14")
    config.get_settings.cache_clear()
    cache.redis_client.cache_clear()
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(processing, "cache_get", lambda key: None)
    monkeypatch.setattr(processing, "cache_set", lambda *args, **kwargs: None)
    yield engine
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()
    config.get_settings.cache_clear()
    cache.redis_client.cache_clear()


def seed_version(content="Payment is due in 30 days.", document_id=None, number=1):
    with database.session() as db, db.begin():
        document = db.get(Document, document_id) if document_id else Document(title="Contract")
        if not document_id:
            db.add(document)
            db.flush()
        key = f"sources/{uuid4()}.txt"
        path = processing.storage_file(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        version = Version(
            document_id=document.id,
            version_number=number,
            filename="contract.txt",
            mime_type="text/plain",
            size_bytes=len(content.encode()),
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            storage_key=key,
        )
        db.add(version)
        db.flush()
        document.latest_version_id = version.id
        job = Job(kind="ingest", resource_id=version.id)
        db.add(job)
        db.flush()
        return document.id, version.id, job.id


class FakeAI:
    context_tokens = 128000
    max_output_tokens = 4096

    def __init__(self, fail_call=None):
        self.batches = []
        self.fail_call = fail_call
        self.generations = 0

    async def embed(self, texts):
        self.batches.append(texts)
        if len(self.batches) == self.fail_call:
            raise ProviderError("Temporary provider failure.", retryable=True)
        return [[1.0] + [0.0] * 1535 for _ in texts]

    async def structured(self, schema, messages):
        import json

        self.generations += 1
        payload = json.loads(messages[-1]["content"])
        ids = payload["sections"][0]["citation_ids"]
        return DocumentInsights(
            summary="Payment is due in 30 days.",
            category="Contract",
            tags=["payment"],
            key_insights=[KeyInsight(text="Payment term", citation_ids=ids)],
            suggestions=["When is renewal?"],
            citation_ids=ids,
        )

    async def close(self):
        pass


def test_ingest_persists_complete_index_and_insights_are_a_separate_job(isolated_db, monkeypatch):
    ai = FakeAI()
    monkeypatch.setattr(processing, "create_ai", lambda resource: ai)
    document_id, version_id, job_id = seed_version()
    jobs.run_job(job_id)
    jobs.run_job(job_id)  # Duplicate queue delivery must not make a second provider call.
    with database.session() as db:
        version = db.get(Version, version_id)
        assert version.status == "ready" and version.chunk_count == 1
        assert db.get(Document, document_id).current_version_id == version_id
        assert db.get(Job, job_id).status == "complete"
        assert db.scalar(select(Chunk).where(Chunk.version_id == version_id)).embedding is not None
        insight_job = db.scalar(
            select(Job).where(Job.kind == "insights", Job.resource_id == version_id)
        )
        assert insight_job.status == "queued" and version.insight_status == "pending"
        insight_job_id = insight_job.id
    assert len(ai.batches) == 1
    assert processing.parsed_path(version_id).exists()
    jobs.run_job(insight_job_id)
    with database.session() as db:
        version = db.get(Version, version_id)
        assert version.status == "ready" and version.insight_status == "ready"
        assert version.insights["summary"] == "Payment is due in 30 days."
    # Simulate a crash after publishing insights but before acknowledging the job.
    with database.session() as db, db.begin():
        db.get(Job, insight_job_id).status = "queued"
    jobs.run_job(insight_job_id)
    assert ai.generations == 1


def test_missing_artifact_is_rejected_before_provider_work(isolated_db, monkeypatch):
    def unexpected_provider(resource):
        pytest.fail("A missing artifact must not start provider work.")

    monkeypatch.setattr(processing, "create_ai", unexpected_provider)
    with pytest.raises(jobs.SourceDeleted, match="artifact no longer exists"):
        asyncio.run(processing._artifact(str(uuid4()), 1, str(uuid4())))


def test_summary_artifact_persists_after_guarded_lookups(isolated_db, monkeypatch):
    ai = FakeAI()
    monkeypatch.setattr(processing, "create_ai", lambda resource: ai)
    _, version_id, ingest_job_id = seed_version()
    jobs.run_job(ingest_job_id)
    with database.session() as db, db.begin():
        artifact = Artifact(kind="summary", signature=uuid4().hex, version_ids=[version_id])
        db.add(artifact)
        db.flush()
        job = Job(kind="summary", resource_id=artifact.id)
        db.add(job)
        db.flush()
        artifact_id, job_id = artifact.id, job.id
    jobs.run_job(job_id)
    with database.session() as db:
        artifact = db.get(Artifact, artifact_id)
        assert artifact is not None and artifact.status == "ready"
        assert artifact.data is not None
        assert artifact.data["summary"] == "Payment is due in 30 days."
        job = db.get(Job, job_id)
        assert job is not None and job.status == "complete"
    assert ai.generations == 1


def test_retry_reuses_completed_embedding_batches(isolated_db, monkeypatch):
    ai = FakeAI(fail_call=2)
    monkeypatch.setattr(processing, "create_ai", lambda resource: ai)
    content = "\n\n".join(f"Section {index}: invoice reference ABC-{index}." for index in range(70))
    _, version_id, job_id = seed_version(content)
    jobs.run_job(job_id)
    with database.session() as db, db.begin():
        assert db.get(Job, job_id).status == "queued"
        completed = db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.version_id == version_id, Chunk.embedding.is_not(None))
        )
        assert completed == 64
        db.get(Job, job_id).next_at = now()
    jobs.run_job(job_id)
    with database.session() as db:
        assert db.get(Version, version_id).status == "ready"
        assert db.get(Job, job_id).attempts == 2
        attempts = list(
            db.scalars(
                select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt)
            )
        )
        assert [attempt.status for attempt in attempts] == ["failed", "complete"]
    assert [len(batch) for batch in ai.batches] == [64, 6, 6]


def test_older_completed_version_does_not_replace_newer_ready_version(isolated_db, monkeypatch):
    monkeypatch.setattr(processing, "create_ai", lambda resource: FakeAI())
    document_id, first_id, first_job = seed_version("Old payment term is 30 days.")
    _, second_id, second_job = seed_version("New payment term is 45 days.", document_id, 2)
    jobs.run_job(second_job)
    jobs.run_job(first_job)
    with database.session() as db:
        assert db.get(Version, first_id).status == "ready"
        assert db.get(Document, document_id).current_version_id == second_id


def test_expired_claim_is_fenced_before_recovery_dispatch(isolated_db, monkeypatch):
    published = []

    class Queue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            published.append((args, kwargs))

    monkeypatch.setattr(jobs, "Queue", Queue)
    _, _, job_id = seed_version()
    old_token = jobs._claim(job_id)
    with database.session() as db, db.begin():
        db.get(Job, job_id).lease_until = now() - timedelta(seconds=1)
    assert jobs.dispatch_once() == 1
    assert len(published) == 1
    with database.session() as db, db.begin(), pytest.raises(jobs.LostClaim):
        jobs.job_checkpoint(db, job_id, old_token)
    new_token = jobs._claim(job_id)
    assert new_token > old_token
    with database.session() as db, db.begin():
        assert jobs.job_checkpoint(db, job_id, new_token).attempts == 2


def test_deleted_source_cannot_publish_embedding_and_cleanup_removes_owned_files(
    isolated_db, monkeypatch
):
    document_id, version_id, job_id = seed_version()
    ai = FakeAI()
    original = ai.embed

    async def delete_during_embed(texts):
        with database.session() as db, db.begin():
            db.get(Document, document_id).deleted_at = now()
        return await original(texts)

    ai.embed = delete_during_embed
    monkeypatch.setattr(processing, "create_ai", lambda resource: ai)
    jobs.run_job(job_id)
    with database.session() as db, db.begin():
        assert db.get(Job, job_id).status == "cancelled"
        assert db.get(Document, document_id).current_version_id is None
        key = db.get(Version, version_id).storage_key
        cleanup = Job(kind="cleanup", resource_id=document_id)
        db.add(cleanup)
        db.flush()
        cleanup_id = cleanup.id
    jobs.run_job(cleanup_id)
    with database.session() as db:
        assert db.get(Job, cleanup_id).status == "complete"
        assert db.get(Version, version_id).status == "deleted"
        assert (
            db.scalar(select(func.count()).select_from(Chunk).where(Chunk.version_id == version_id))
            == 0
        )
    assert not processing.storage_file(key).exists()
    assert not processing.parsed_path(version_id).exists()


def test_heartbeat_keeps_a_slow_runner_claimed(isolated_db, monkeypatch):
    monkeypatch.setenv("LEASE_SECONDS", "2")
    config.get_settings.cache_clear()
    ai = FakeAI()
    original = ai.embed

    async def slow_embedding(texts):
        time.sleep(3)
        return await original(texts)

    ai.embed = slow_embedding
    monkeypatch.setattr(processing, "create_ai", lambda resource: ai)
    _, version_id, job_id = seed_version()
    jobs.run_job(job_id)
    with database.session() as db:
        assert db.get(Job, job_id).status == "complete"
        assert db.get(Version, version_id).status == "ready"


def test_heartbeat_stops_when_its_claim_has_been_reassigned(isolated_db):
    _, _, job_id = seed_version()
    token = jobs._claim(job_id)
    assert token is not None
    with database.session() as db, db.begin():
        job = db.get(Job, job_id)
        assert job is not None
        job.token += 1
        lease = job.lease_until

    class ControlledStop(threading.Event):
        def __init__(self):
            super().__init__()
            self.waits = 0

        def wait(self, timeout: float | None = None) -> bool:
            self.waits += 1
            return self.waits > 1

    stop = ControlledStop()
    jobs._heartbeat(job_id, token, stop)
    assert stop.waits == 1
    with database.session() as db:
        job = db.get(Job, job_id)
        assert job is not None and job.lease_until == lease


def test_only_transient_failures_retry_and_stop_after_three_attempts(isolated_db, monkeypatch):
    ai = FakeAI()

    async def broken_embedding(texts):
        raise ProviderError("Temporary provider failure.", retryable=True)

    ai.embed = broken_embedding
    monkeypatch.setattr(processing, "create_ai", lambda resource: ai)
    _, version_id, job_id = seed_version()
    for _ in range(3):
        jobs.run_job(job_id)
        with database.session() as db, db.begin():
            db.get(Job, job_id).next_at = now()
    with database.session() as db:
        assert db.get(Job, job_id).status == "failed"
        assert db.get(Job, job_id).attempts == 3
        assert db.get(Version, version_id).status == "failed"
    jobs.retry_job(job_id)
    with database.session() as db:
        assert db.get(Job, job_id).status == "queued"


def test_real_rq_delivery_executes_claimed_job(isolated_db, monkeypatch):
    from rq import Queue, SimpleWorker

    connection = cache.redis_client()
    name = "docvault-test-" + uuid4().hex
    queue = Queue(name, connection=connection)
    monkeypatch.setattr(jobs, "Queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr(processing, "create_ai", lambda resource: FakeAI())
    _, version_id, job_id = seed_version()
    try:
        assert jobs.dispatch_once() == 1
        assert queue.count == 1
        worker = SimpleWorker([queue], connection=connection)
        worker.work(burst=True, logging_level="WARNING")
        with database.session() as db:
            assert db.get(Job, job_id).status == "complete"
            assert db.get(Version, version_id).status == "ready"
    finally:
        queue.delete(delete_jobs=True)


def test_dispatcher_recovers_stale_generation_without_interrupting_live_stream(
    isolated_db, monkeypatch
):
    notifications = []
    monkeypatch.setattr(jobs, "notify_change", lambda: notifications.append(True))
    with database.session() as db, db.begin():
        old_chat, live_chat = Chat(title="Old", version_ids=[]), Chat(title="Live", version_ids=[])
        db.add_all([old_chat, live_chat])
        db.flush()
        old_message = Message(
            chat_id=old_chat.id,
            role="assistant",
            status="streaming",
            updated_at=now() - timedelta(minutes=11),
        )
        live_message = Message(chat_id=live_chat.id, role="assistant", status="streaming")
        db.add_all([old_message, live_message])
        db.flush()
        old_id, live_id, old_chat_id = old_message.id, live_message.id, old_chat.id
    assert jobs.dispatch_once() == 0
    assert notifications == [True]
    with database.session() as db, db.begin():
        assert db.get(Message, old_id).status == "failed"
        assert "retry" in db.get(Message, old_id).error
        assert db.get(Message, live_id).status == "streaming"
        # The stale generation no longer occupies the partial unique constraint.
        db.add(Message(chat_id=old_chat_id, role="assistant", status="pending"))
    # With nothing stale or queued, an empty RETURNING result causes no update event.
    assert jobs.dispatch_once() == 0
    assert notifications == [True]
