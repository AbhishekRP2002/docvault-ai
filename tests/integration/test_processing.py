"""Run with RUN_INTEGRATION=1 against the dedicated local docvault_test database.

Each test owns a random PostgreSQL schema and temporary storage directory. No
application data is reset. Model calls are deterministic; PostgreSQL is real.
"""

import asyncio
import hashlib
import importlib.metadata
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from support import require_persisted_row

from docvault import cache, config, jobs, processing
from docvault import db as database
from docvault.documents import require_document
from docvault.llm.config import GenerationModelConfig
from docvault.llm.models import (
    CitedKeyInsight,
    InsightsGenerationLLMResponse,
)
from docvault.llm.provider import ProviderError
from docvault.models import Artifact, Chat, Chunk, Document, Job, JobAttempt, Message, Version, now
from docvault.parsing import ParsedChunk, ParsedDocument

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
        document = (
            require_persisted_row(db, Document, document_id)
            if document_id
            else Document(title="Contract")
        )
        if not document_id:
            db.add(document)
            db.flush()
        key = f"sources/{uuid4()}.txt"
        path = processing.resolve_storage_path(key)
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


class FakeLLM:
    async def resolve_model_context_tokens(self, task):
        """Use the fixture model's deterministic capacity without issuing metadata requests."""
        return self.generation_model(task).context_tokens

    def generation_model(self, task):
        return GenerationModelConfig(
            model=f"test/{task}", context_tokens=128000, max_output_tokens=4096
        )

    def __init__(self, fail_call=None):
        self.batches = []
        self.fail_call = fail_call
        self.generations = 0

    async def embed_texts(self, texts):
        self.batches.append(texts)
        if len(self.batches) == self.fail_call:
            raise ProviderError("Temporary provider failure.", retryable=True)
        return [[1.0] + [0.0] * 1535 for _ in texts]

    async def generate_structured_response(self, schema, messages, *, task):
        import json

        self.generations += 1
        payload = json.loads(messages[-1]["content"])
        ids = payload["sections"][0]["citation_ids"]
        return InsightsGenerationLLMResponse(
            summary="Payment is due in 30 days.",
            category="Contract",
            tags=["payment"],
            key_insights=[CitedKeyInsight(insight_text="Payment term", citation_ids=ids)],
            suggestions=["When is renewal?"],
            citation_ids=ids,
        )

    async def close(self):
        pass


def test_ingest_persists_complete_index_and_insights_are_a_separate_job(isolated_db, monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    document_id, version_id, job_id = seed_version()
    jobs.run_job(job_id)
    jobs.run_job(job_id)  # Duplicate queue delivery must not make a second provider call.
    with database.session() as db:
        version = require_persisted_row(db, Version, version_id)
        assert version.status == "ready" and version.chunk_count == 1
        assert require_persisted_row(db, Document, document_id).current_version_id == version_id
        assert require_persisted_row(db, Job, job_id).status == "complete"
        chunk = db.scalar(select(Chunk).where(Chunk.version_id == version_id))
        assert chunk is not None and chunk.embedding is not None
        insight_job = db.scalar(
            select(Job).where(Job.kind == "insights", Job.resource_id == version_id)
        )
        assert insight_job is not None
        assert insight_job.status == "queued" and version.insight_status == "pending"
        insight_job_id = insight_job.id
    assert len(llm.batches) == 1
    assert processing.get_parsed_document_path(version_id).exists()
    jobs.run_job(insight_job_id)
    with database.session() as db:
        version = require_persisted_row(db, Version, version_id)
        assert version.status == "ready" and version.insight_status == "ready"
        assert version.insights is not None
        assert version.insights["summary"] == "Payment is due in 30 days."
        persisted_insight = version.insights["key_insights"][0]
        assert persisted_insight["text"] == "Payment term"
        assert set(persisted_insight) == {"text", "citation_ids"}
        assert persisted_insight["citation_ids"]
    # Simulate a crash after publishing insights but before acknowledging the job.
    with database.session() as db, db.begin():
        require_persisted_row(db, Job, insight_job_id).status = "queued"
    jobs.run_job(insight_job_id)
    assert llm.generations == 1


def test_missing_artifact_is_rejected_before_provider_work(isolated_db, monkeypatch):
    def unexpected_provider(resource):
        pytest.fail("A missing artifact must not start provider work.")

    monkeypatch.setattr(processing, "create_llm_client", unexpected_provider)
    with pytest.raises(jobs.SourceDeleted, match="artifact no longer exists"):
        asyncio.run(processing._generate_requested_artifact(str(uuid4()), 1, str(uuid4())))


@pytest.mark.parametrize("with_generation_fingerprint", [False, True])
def test_summary_artifact_persists_after_guarded_lookups(
    isolated_db, monkeypatch, with_generation_fingerprint
):
    llm = FakeLLM()
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    _, version_id, ingest_job_id = seed_version()
    jobs.run_job(ingest_job_id)
    with database.session() as db, db.begin():
        identity = processing.build_generation_identity(llm.generation_model("summary"), "summary")
        options = (
            {"generation_fingerprint": cache.calculate_json_fingerprint(identity)}
            if with_generation_fingerprint
            else {}
        )
        artifact = Artifact(
            kind="summary", signature=uuid4().hex, version_ids=[version_id], options=options
        )
        db.add(artifact)
        db.flush()
        job = Job(kind="summary", resource_id=artifact.id)
        db.add(job)
        db.flush()
        artifact_id, job_id = artifact.id, job.id
    jobs.run_job(job_id)
    with database.session() as db:
        artifact = require_persisted_row(db, Artifact, artifact_id)
        assert artifact is not None and artifact.status == "ready"
        assert artifact.data is not None
        assert artifact.data["summary"] == "Payment is due in 30 days."
        assert artifact.data["generation"]["fingerprint"] == cache.calculate_json_fingerprint(
            identity
        )
        assert artifact.data["generation"]["model_config"]["model"] == "test/summary"
        job = require_persisted_row(db, Job, job_id)
        assert job is not None and job.status == "complete"
    assert llm.generations == 1


def test_retry_reuses_completed_embedding_batches(isolated_db, monkeypatch):
    llm = FakeLLM(fail_call=2)
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    # Distinct headings prevent native peer merging, so this crosses the 64-input batch boundary.
    content = "\n\n".join(
        f"# Section {index}\n" + (f"Invoice reference ABC-{index} is due in thirty days. " * 40)
        for index in range(70)
    )
    _, version_id, job_id = seed_version(content)
    jobs.run_job(job_id)
    with database.session() as db, db.begin():
        assert require_persisted_row(db, Job, job_id).status == "queued"
        completed = db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.version_id == version_id, Chunk.embedding.is_not(None))
        )
        assert completed == 64
        require_persisted_row(db, Job, job_id).next_at = now()
    jobs.run_job(job_id)
    with database.session() as db:
        assert require_persisted_row(db, Version, version_id).status == "ready"
        assert require_persisted_row(db, Job, job_id).attempts == 2
        attempts = list(
            db.scalars(
                select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.attempt)
            )
        )
        assert [attempt.status for attempt in attempts] == ["failed", "complete"]
    assert [len(batch) for batch in llm.batches] == [64, 6, 6]


@pytest.mark.parametrize(
    "cached_chunker",
    [
        "structure-600-v1",
        f"docling-hybrid-600-v2-core-{importlib.metadata.version('docling-core')}",
    ],
)
def test_chunker_upgrade_reparses_old_cached_artifacts(isolated_db, monkeypatch, cached_chunker):
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: FakeLLM())
    _, version_id, job_id = seed_version()
    with database.session() as db:
        version = require_persisted_row(db, Version, version_id)
        old_fingerprint = cache.calculate_json_fingerprint(
            {
                "sha256": version.sha256,
                "filename": version.filename,
                "parser": "utf8-v1",
                "chunker": cached_chunker,
                "ocr": "rapidocr-english-torch",
            }
        )
        new_fingerprint = processing.calculate_parser_fingerprint(version)
    old_artifact = ParsedDocument(
        text="Old cached artifact.",
        chunks=[
            ParsedChunk(
                text="Old cached artifact.",
                embedding_text="Old cached artifact.",
                location={"kind": "txt", "char_start": 0, "char_end": 20},
                token_count=4,
            )
        ],
        page_count=None,
        parser="utf8-v1",
    )
    processing._persist_parsed_document(version_id, old_artifact, old_fingerprint, 0)
    assert processing._load_cached_parsed_document(version_id, new_fingerprint) is None
    jobs.run_job(job_id)
    refreshed = processing._load_cached_parsed_document(version_id, new_fingerprint)
    assert refreshed is not None and refreshed.text == "Payment is due in 30 days."
    with database.session() as db:
        assert require_persisted_row(db, Version, version_id).status == "ready"
        assert all("Old cached" not in text for text in db.scalars(select(Chunk.text)))


def test_older_completed_version_does_not_replace_newer_ready_version(isolated_db, monkeypatch):
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: FakeLLM())
    document_id, first_id, first_job = seed_version("Old payment term is 30 days.")
    _, second_id, second_job = seed_version("New payment term is 45 days.", document_id, 2)
    jobs.run_job(second_job)
    jobs.run_job(first_job)
    with database.session() as db:
        assert require_persisted_row(db, Version, first_id).status == "ready"
        assert require_persisted_row(db, Document, document_id).current_version_id == second_id


def test_expired_claim_is_fenced_before_recovery_dispatch(isolated_db, monkeypatch):
    published = []

    class Queue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, *args, **kwargs):
            published.append((args, kwargs))

    monkeypatch.setattr(jobs, "Queue", Queue)
    _, _, job_id = seed_version()
    old_token = jobs._claim_pending_job(job_id)
    assert old_token is not None
    with database.session() as db, db.begin():
        require_persisted_row(db, Job, job_id).lease_until = now() - timedelta(seconds=1)
    assert jobs.dispatch_pending_jobs() == 1
    assert len(published) == 1
    with database.session() as db, db.begin(), pytest.raises(jobs.LostClaim):
        jobs.job_checkpoint(db, job_id, old_token)
    new_token = jobs._claim_pending_job(job_id)
    assert new_token is not None
    assert new_token > old_token
    with database.session() as db, db.begin():
        assert jobs.job_checkpoint(db, job_id, new_token).attempts == 2


def test_deleted_source_cannot_publish_embedding_and_cleanup_removes_owned_files(
    isolated_db, monkeypatch
):
    document_id, version_id, job_id = seed_version()
    llm = FakeLLM()
    original = llm.embed_texts

    async def delete_during_embed(texts):
        with database.session() as db, db.begin():
            require_persisted_row(db, Document, document_id).deleted_at = now()
        return await original(texts)

    llm.embed_texts = delete_during_embed
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    jobs.run_job(job_id)
    with database.session() as db, db.begin():
        assert require_persisted_row(db, Job, job_id).status == "cancelled"
        assert require_persisted_row(db, Document, document_id).current_version_id is None
        key = require_persisted_row(db, Version, version_id).storage_key
        cleanup = Job(kind="cleanup", resource_id=document_id)
        db.add(cleanup)
        db.flush()
        cleanup_id = cleanup.id
    jobs.run_job(cleanup_id)
    with database.session() as db:
        assert require_persisted_row(db, Job, cleanup_id).status == "complete"
        assert require_persisted_row(db, Version, version_id).status == "deleted"
        assert (
            db.scalar(select(func.count()).select_from(Chunk).where(Chunk.version_id == version_id))
            == 0
        )
    assert not processing.resolve_storage_path(key).exists()
    assert not processing.get_parsed_document_path(version_id).exists()


def test_heartbeat_keeps_a_slow_runner_claimed(isolated_db, monkeypatch):
    monkeypatch.setenv("LEASE_SECONDS", "2")
    config.get_settings.cache_clear()
    llm = FakeLLM()
    original = llm.embed_texts

    async def slow_embedding(texts):
        time.sleep(3)
        return await original(texts)

    llm.embed_texts = slow_embedding
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    _, version_id, job_id = seed_version()
    jobs.run_job(job_id)
    with database.session() as db:
        assert require_persisted_row(db, Job, job_id).status == "complete"
        assert require_persisted_row(db, Version, version_id).status == "ready"


def test_heartbeat_stops_when_its_claim_has_been_reassigned(isolated_db):
    _, _, job_id = seed_version()
    token = jobs._claim_pending_job(job_id)
    assert token is not None
    with database.session() as db, db.begin():
        job = require_persisted_row(db, Job, job_id)
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
    jobs._renew_job_lease(job_id, token, stop)
    assert stop.waits == 1
    with database.session() as db:
        job = require_persisted_row(db, Job, job_id)
        assert job is not None and job.lease_until == lease


def test_only_transient_failures_retry_and_stop_after_three_attempts(isolated_db, monkeypatch):
    llm = FakeLLM()

    async def broken_embedding(texts):
        raise ProviderError("Temporary provider failure.", retryable=True)

    llm.embed_texts = broken_embedding
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    _, version_id, job_id = seed_version()
    for _ in range(3):
        jobs.run_job(job_id)
        with database.session() as db, db.begin():
            require_persisted_row(db, Job, job_id).next_at = now()
    with database.session() as db:
        assert require_persisted_row(db, Job, job_id).status == "failed"
        assert require_persisted_row(db, Job, job_id).attempts == 3
        assert require_persisted_row(db, Version, version_id).status == "failed"
    jobs.retry_job(job_id)
    with database.session() as db:
        assert require_persisted_row(db, Job, job_id).status == "queued"


def test_real_rq_delivery_executes_claimed_job(isolated_db, monkeypatch):
    from rq import Queue, SimpleWorker

    connection = cache.redis_client()
    name = "docvault-test-" + uuid4().hex
    queue = Queue(name, connection=connection)
    monkeypatch.setattr(jobs, "Queue", lambda *args, **kwargs: queue)
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: FakeLLM())
    _, version_id, job_id = seed_version()
    try:
        assert jobs.dispatch_pending_jobs() == 1
        assert queue.count == 1
        worker = SimpleWorker([queue], connection=connection)
        worker.work(burst=True, logging_level="WARNING")
        with database.session() as db:
            assert require_persisted_row(db, Job, job_id).status == "complete"
            assert require_persisted_row(db, Version, version_id).status == "ready"
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
    assert jobs.dispatch_pending_jobs() == 0
    assert notifications == [True]
    with database.session() as db, db.begin():
        stale_message = require_persisted_row(db, Message, old_id)
        assert stale_message.status == "failed"
        assert stale_message.error is not None and "retry" in stale_message.error
        assert require_persisted_row(db, Message, live_id).status == "streaming"
        # The stale generation no longer occupies the partial unique constraint.
        db.add(Message(chat_id=old_chat_id, role="assistant", status="pending"))
    # With nothing stale or queued, an empty RETURNING result causes no update event.
    assert jobs.dispatch_pending_jobs() == 0
    assert notifications == [True]


def test_changed_generation_configuration_rejects_queued_artifact_before_llm_call(
    isolated_db, monkeypatch
):
    llm = FakeLLM()
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    _, version_id, ingest_job_id = seed_version()
    jobs.run_job(ingest_job_id)
    with database.session() as db, db.begin():
        artifact = Artifact(
            kind="summary",
            signature=uuid4().hex,
            version_ids=[version_id],
            options={"generation_fingerprint": "different-configuration"},
        )
        db.add(artifact)
        db.flush()
        job = Job(kind="summary", resource_id=artifact.id)
        db.add(job)
        db.flush()
        artifact_id, job_id = artifact.id, job.id
    jobs.run_job(job_id)
    with database.session() as db:
        artifact = require_persisted_row(db, Artifact, artifact_id)
        assert artifact.status == "failed" and artifact.data is None
        assert artifact.error is not None and "configuration changed" in artifact.error
        assert require_persisted_row(db, Job, job_id).status == "failed"
    assert llm.generations == 0


def test_new_version_reuses_unchanged_vectors_and_current_evidence_excludes_removed_chunks(
    isolated_db, monkeypatch
):
    """Embed only changed inputs while retaining historical chunks for their pinned citations."""
    retained = "Payment is due in thirty days."
    removed = "The outdated annual fee is nine hundred dollars."
    added = "The updated annual fee is twelve hundred dollars."

    def parse_line_chunks(path, mime_type, filename):
        """Give each fixture line an independent input hash and its version-specific location."""
        text = path.read_text()
        return ParsedDocument(
            text=text,
            chunks=[
                ParsedChunk(
                    text=line,
                    embedding_text=line,
                    location={"line_start": index + 1},
                    token_count=len(line.split()),
                )
                for index, line in enumerate(text.splitlines())
            ],
            page_count=None,
            parser="line-fixture",
        )

    class DistinctVectorLLM(FakeLLM):
        async def embed_texts(self, texts):
            """Record billed inputs and give each provider batch visibly different vectors."""
            self.batches.append(texts)
            return [
                [float(len(self.batches) * 10 + index + 1)] + [0.0] * 1535
                for index, _ in enumerate(texts)
            ]

    llm = DistinctVectorLLM()
    monkeypatch.setattr(processing, "parse_document_file", parse_line_chunks)
    monkeypatch.setattr(processing, "create_llm_client", lambda resource: llm)
    document_id, old_id, old_job = seed_version(f"{removed}\n{retained}")
    jobs.run_job(old_job)
    with database.session() as db:
        old_chunks = list(db.scalars(select(Chunk).where(Chunk.version_id == old_id)))
        old_retained = next(chunk for chunk in old_chunks if chunk.text == retained)
        retained_id = old_retained.id
        retained_hash = old_retained.input_hash
        retained_vector = old_retained.embedding
        assert retained_vector is not None
        assert old_retained.location == {"line_start": 2}
        assert require_persisted_row(db, Document, document_id).current_version_id == old_id
    _, new_id, new_job = seed_version(f"{retained}\n{added}", document_id, 2)
    with database.session() as db:
        document = require_persisted_row(db, Document, document_id)
        assert document.latest_version_id == new_id
        assert document.current_version_id == old_id
        assert require_persisted_row(db, Version, new_id).status == "queued"
    jobs.run_job(new_job)
    assert llm.batches == [[removed, retained], [added]]
    with database.session() as db:
        document = require_persisted_row(db, Document, document_id)
        assert document.current_version_id == new_id and document.latest_version_id == new_id
        assert require_persisted_row(db, Version, old_id).status == "ready"
        assert require_persisted_row(db, Version, new_id).status == "ready"
        current_chunks = list(db.scalars(select(Chunk).where(Chunk.version_id == new_id)))
        current_retained = next(chunk for chunk in current_chunks if chunk.text == retained)
        current_added = next(chunk for chunk in current_chunks if chunk.text == added)
        assert current_retained.id != retained_id
        assert current_retained.input_hash == retained_hash
        assert current_retained.embedding == retained_vector
        assert current_retained.location == {"line_start": 1}
        assert current_added.embedding is not None and current_added.embedding != retained_vector
        current_version_id = document.current_version_id
        assert current_version_id is not None
        current_evidence = processing.load_document_version_evidence(db, current_version_id)
        assert {item.text for item in current_evidence} == {retained, added}
        assert {item.version_id for item in current_evidence} == {new_id}
        assert removed not in {item.text for item in current_evidence}
        historical_evidence = processing.load_document_version_evidence(db, old_id)
        assert {item.text for item in historical_evidence} == {removed, retained}
        assert retained_id in {item.id for item in historical_evidence}


def test_older_activation_waits_for_document_lock_and_preserves_newer_committed_pointer(
    isolated_db, monkeypatch
):
    """Avoid a stale current-version read while a newer activation transaction is uncommitted."""
    document_id, old_id, old_job = seed_version("Older complete index.")
    _, new_id, _ = seed_version("Newer complete index.", document_id, 2)
    with database.session() as db, db.begin():
        for identifier in (old_id, new_id):
            version = require_persisted_row(db, Version, identifier)
            version.chunk_count = 1
            version.embedding_model = config.get_settings().openrouter_embedding_model
            db.add(
                Chunk(
                    version_id=identifier,
                    ordinal=0,
                    text=version.filename,
                    embedding_text=version.filename,
                    input_hash=identifier,
                    location={"line_start": 1},
                    token_count=1,
                    embedding=[1.0] + [0.0] * 1535,
                )
            )
    token = jobs._claim_pending_job(old_job)
    assert token is not None
    started = threading.Event()
    backend_pids = []

    def observe_checkpoint(db, identifier, claim_token, stage=None):
        """Identify the activation connection before checkpoint acquires its source lock."""
        backend_pid = db.scalar(text("SELECT pg_backend_pid()"))
        assert backend_pid is not None
        backend_pids.append(backend_pid)
        started.set()
        return jobs.job_checkpoint(db, identifier, claim_token, stage)

    monkeypatch.setattr(processing, "job_checkpoint", observe_checkpoint)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.session() as holder, holder.begin():
            document = require_document(holder, document_id, lock=True)
            require_persisted_row(holder, Version, new_id).status = "ready"
            document.current_version_id = new_id
            holder.flush()
            activation = pool.submit(processing._activate_ready_version, old_job, token)
            assert started.wait(timeout=5), "Older activation did not reach its job checkpoint."
            blocked = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not activation.done():
                with database.session() as observer:
                    blocked = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE pid = :pid AND cardinality(pg_blocking_pids(pid)) > 0)"
                            ),
                            {"pid": backend_pids[0]},
                        )
                    )
                if blocked:
                    break
                time.sleep(0.01)
        activation.result(timeout=5)
    assert blocked, "Older activation did not overlap the newer pointer's uncommitted update."
    with database.session() as db:
        assert require_persisted_row(db, Document, document_id).current_version_id == new_id
        assert require_persisted_row(db, Version, old_id).status == "ready"
        assert require_persisted_row(db, Version, new_id).status == "ready"
