"""Real PostgreSQL API contracts with deterministic provider and cache boundaries.

Run sequentially against an explicitly named docvault_test database. Each test
owns a temporary schema and storage directory; no public application tables or
real provider accounts are used.
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from docvault import cache, integrations
from docvault.ai.types import Answer
from docvault.config import get_settings
from docvault.db import Base, get_engine, session
from docvault.models import Chat, Chunk, Document, Job, Message, Version, now

pytestmark = pytest.mark.integration


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.values.get(key)

    def setex(self, key, ttl, value):
        with self.lock:
            self.values[key] = value

    def incr(self, key):
        with self.lock:
            value = int(self.values.get(key, 0)) + 1
            self.values[key] = value
            return value

    def publish(self, channel, value):
        return 0


class FakeAI:
    def __init__(self):
        self.calls = []
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()

    async def embed(self, inputs):
        return [[1.0] + [0.0] * 1535 for _ in inputs]

    async def structured(self, schema, messages):
        payload = json.loads(messages[-1]["content"])
        return schema(question=payload["question"], needs_clarification=False, clarification="")

    async def stream_answer(self, messages, on_delta):
        payload = json.loads(messages[-1]["content"])
        self.calls.append(payload)
        self.started.set()
        if self.block:
            if not await asyncio.to_thread(self.release.wait, 10):
                raise TimeoutError("Test provider release was not signalled.")
        evidence = payload["evidence"][0]
        response = f"{evidence['text']} [{evidence['id']}]"
        split = len(response) // 2
        await on_delta(response[:split])
        await on_delta(response[split:])
        return Answer(
            response=response,
            suggestions=["What is the renewal date?"],
            citation_ids=[evidence["id"]],
            outcome="answered",
        )

    async def close(self):
        pass


@pytest.fixture(scope="module")
def test_database_url():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated docvault_test database.")
    url = make_url(value)
    if "docvault_test" not in (url.database or ""):
        pytest.fail("Refusing to create/drop schemas outside a docvault_test database.")
    return url


@pytest.fixture
def api(test_database_url, monkeypatch, tmp_path):
    schema = f"api_test_{uuid4().hex}"
    admin = create_engine(test_database_url)
    with admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    options = dict(test_database_url.query)
    options["options"] = f"-csearch_path={schema},public"
    scoped_url = test_database_url.set(query=options)
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", scoped_url.render_as_string(hide_password=False))
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-provider-key-never-sent-to-network")
    monkeypatch.setenv("CHAT_RATE_PER_MINUTE", "0")
    monkeypatch.setenv("UPLOAD_RATE_PER_HOUR", "0")
    monkeypatch.setenv("DAILY_BUDGET_USD", "0")
    redis = MemoryRedis()
    provider = FakeAI()
    monkeypatch.setattr(cache, "redis_client", lambda: redis)
    monkeypatch.setattr(integrations, "create_ai", lambda resource_id=None: provider)
    engine = get_engine()
    try:
        Base.metadata.create_all(engine)
        from docvault.main import app

        with TestClient(app) as client:
            yield SimpleNamespace(client=client, ai=provider, storage=tmp_path)
    finally:
        provider.release.set()
        engine.dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def ready_source(filename="terms.txt", content="Payment is due in 30 days."):
    identifier = str(uuid4())
    key = f"sources/{identifier}.txt"
    path = get_settings().storage_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    with session() as db, db.begin():
        doc = Document(title=filename)
        db.add(doc)
        db.flush()
        version = Version(
            id=identifier,
            document_id=doc.id,
            version_number=1,
            filename=filename,
            mime_type="text/plain",
            size_bytes=len(content.encode()),
            sha256=digest,
            storage_key=key,
            status="ready",
            embedding_model=get_settings().openrouter_embedding_model,
            chunk_count=1,
            ready_at=now(),
        )
        db.add(version)
        db.flush()
        doc.current_version_id = doc.latest_version_id = version.id
        chunk = Chunk(
            version_id=version.id,
            ordinal=0,
            text=content,
            embedding_text=content,
            input_hash=digest,
            location={"line_start": 1, "char_start": 0, "char_end": len(content)},
            token_count=12,
            embedding=[1.0] + [0.0] * 1535,
        )
        db.add(chunk)
        db.flush()
        return SimpleNamespace(document=doc.id, version=version.id, chunk=chunk.id, text=content)


def create_chat(api, sources, title="New chat"):
    result = api.client.post(
        "/v1/chats", json={"title": title, "version_ids": [s.version for s in sources]}
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


def ask(api, chat_id, question="When is payment due?", key=None, accept="application/json"):
    return api.client.post(
        f"/v1/chats/{chat_id}/messages",
        headers={"Idempotency-Key": key or str(uuid4()), "Accept": accept},
        json={"content": question},
    )


def test_upload_persists_original_bytes_and_exactly_one_durable_job(api):
    original = b"Payment is due in 30 days.\n"
    response = api.client.post(
        "/v1/documents",
        files={"file": ("../../terms.txt", original, "text/plain")},
        headers={"Idempotency-Key": "upload-one"},
    )
    assert response.status_code == 202, response.text
    result = response.json()
    with session() as db:
        version = db.get(Version, result["latest_version_id"])
        assert version.document_id == result["id"]
        assert version.filename == "terms.txt"
        assert version.status == "queued"
        assert (api.storage / version.storage_key).read_bytes() == original
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        job = db.scalar(select(Job))
        assert job.resource_id == version.id and job.status == "queued"
    content = api.client.get(f"/v1/versions/{result['latest_version_id']}/content")
    assert content.status_code == 200 and content.content == original


@pytest.mark.parametrize(
    ("filename", "payload", "status", "code"),
    [
        ("fake.pdf", b"This is not a PDF.", 415, "invalid_pdf"),
        ("bad.docx", b"not a zip container", 415, "invalid_docx"),
        ("bad.txt", b"\xff\xfe", 422, "invalid_encoding"),
        ("binary.txt", b"terms\x00and conditions", 422, "invalid_text"),
        ("empty.txt", b"", 422, "empty_file"),
        ("script.exe", b"executable", 415, "unsupported_format"),
    ],
)
def test_invalid_bytes_leave_no_document_job_or_file(api, filename, payload, status, code):
    response = api.client.post("/v1/documents", files={"file": (filename, payload)})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Document)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert not [path for path in api.storage.rglob("*") if path.is_file()]


def test_upload_replay_remains_the_original_acceptance_after_a_new_version(api):
    headers = {"Idempotency-Key": "stable-upload"}
    first = api.client.post(
        "/v1/documents", files={"file": ("terms.txt", b"Original terms.")}, headers=headers
    )
    assert first.status_code == 202, first.text
    original = first.json()
    changed = api.client.post(
        f"/v1/documents/{original['id']}/versions",
        files={"file": ("terms.txt", b"Revised terms.")},
        headers={"Idempotency-Key": "new-version"},
    )
    assert changed.status_code == 202, changed.text
    assert changed.json()["latest_version_id"] != original["latest_version_id"]
    replay = api.client.post(
        "/v1/documents", files={"file": ("terms.txt", b"Original terms.")}, headers=headers
    )
    assert replay.status_code == 202, replay.text
    assert replay.json() == original
    conflict = api.client.post(
        "/v1/documents", files={"file": ("terms.txt", b"Conflicting terms.")}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 2
        assert db.scalar(select(func.count()).select_from(Job)) == 2
    assert len([p for p in (api.storage / "sources").iterdir() if p.is_file()]) == 2


def test_batch_accepts_valid_file_without_hiding_rejected_file(api):
    response = api.client.post(
        "/v1/document-batches",
        files=[("files", ("terms.txt", b"Valid terms.")), ("files", ("bad.pdf", b"bad"))],
        headers={"Idempotency-Key": "mixed-batch"},
    )
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["items"][0]["document"]["filename"] == "terms.txt"
    assert result["items"][0]["error"] is None
    assert result["items"][1]["document"] is None
    assert result["items"][1]["error"]
    assert api.client.get(f"/v1/document-batches/{result['id']}").json() == result
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1


def test_cited_chat_and_idempotency_read_back_persisted_history(api):
    source = ready_source()
    chat = create_chat(api, [source])
    answer = ask(api, chat, key="same-message")
    assert answer.status_code == 200, answer.text
    message = answer.json()
    assert message["status"] == "complete" and message["outcome"] == "answered"
    assert source.text in message["content"]
    assert message["version_ids"] == [source.version]
    assert message["citations"][0]["chunk_id"] == source.chunk
    assert message["suggestions"] == ["What is the renewal date?"]
    assert ask(api, chat, key="same-message").json() == message
    assert len(api.ai.calls) == 1
    assert ask(api, chat, question="Different question", key="same-message").status_code == 409
    history = api.client.get(f"/v1/chats/{chat}/messages").json()["items"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[-1] == message
    assert api.client.get(f"/v1/chats/{chat}/messages/{message['id']}").json() == message
    citation = api.client.get(f"/v1/versions/{source.version}/chunks/{source.chunk}")
    assert citation.status_code == 200 and citation.json()["quote"] == source.text


def test_sse_contains_real_deltas_then_the_persisted_frontend_message(api):
    source = ready_source()
    chat = create_chat(api, [source])
    result = ask(api, chat, accept="text/event-stream")
    assert result.status_code == 200, result.text
    frames = []
    for frame in result.text.split("\n\n"):
        lines = frame.splitlines()
        if not lines or not lines[0].startswith("event: "):
            continue
        event_name = lines[0][7:]
        payload = json.loads("\n".join(line[6:] for line in lines if line.startswith("data: ")))
        frames.append((event_name, payload))
    names = [name for name, _ in frames]
    assert names[0] == "message.started" and names[-1] == "answer.completed"
    assert names.count("answer.delta") == 2
    message = frames[-1][1]["message"]
    assert (
        "".join(payload["text"] for name, payload in frames if name == "answer.delta")
        == message["content"]
    )
    assert api.client.get(f"/v1/chats/{chat}/messages/{message['id']}").json() == message


def test_retry_keeps_original_sources_after_selection_edit_and_hides_old_attempt(api):
    original = ready_source("original.txt", "Payment is due in 30 days.")
    replacement = ready_source("replacement.txt", "Payment is due in 90 days.")
    chat = create_chat(api, [original])
    first = ask(api, chat).json()
    assert first["status"] == "complete"
    update = api.client.patch(f"/v1/chats/{chat}", json={"version_ids": [replacement.version]})
    assert update.status_code == 200, update.text
    retry_path = f"/v1/chats/{chat}/messages/{first['id']}/retry"
    retry = api.client.post(retry_path, headers={"Idempotency-Key": "explicit-regeneration"})
    assert retry.status_code == 200, retry.text
    retried = retry.json()
    assert retried["id"] != first["id"] and retried["parent_id"] == first["parent_id"]
    assert retried["version_ids"] == [original.version]
    assert retried["citations"][0]["version_id"] == original.version
    assert len(api.ai.calls) == 2, "Regeneration must bypass completed-answer reuse."
    assert (
        api.client.post(retry_path, headers={"Idempotency-Key": "explicit-regeneration"}).json()
        == retried
    )
    assert len(api.ai.calls) == 2
    history = api.client.get(f"/v1/chats/{chat}/messages").json()["items"]
    assert len(history) == 2 and history[-1]["id"] == retried["id"]
    assert api.client.get(f"/v1/chats/{chat}/messages/{first['id']}").json() == first
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 3
    next_turn = ask(api, chat, "What terms apply now?").json()
    assert next_turn["version_ids"] == [replacement.version]
    assert next_turn["citations"][0]["version_id"] == replacement.version
    older_retry = api.client.post(retry_path, headers={"Idempotency-Key": "too-old"})
    assert older_retry.status_code == 409
    assert older_retry.json()["error"]["code"] == "retry_latest_turn"


def test_sessions_keep_independent_histories_and_selection_changes(api):
    first_source = ready_source("first.txt", "First contract pays in 30 days.")
    second_source = ready_source("second.txt", "Second contract pays in 90 days.")
    first = create_chat(api, [first_source], "First session")
    second = create_chat(api, [second_source], "Second session")
    assert ask(api, first).json()["version_ids"] == [first_source.version]
    assert api.client.get(f"/v1/chats/{second}/messages").json()["items"] == []
    assert ask(api, second).json()["version_ids"] == [second_source.version]
    updated = api.client.patch(
        f"/v1/chats/{first}", json={"title": "Renamed", "version_ids": [second_source.version]}
    )
    assert updated.status_code == 200 and updated.json()["title"] == "Renamed"
    sessions = {item["id"]: item for item in api.client.get("/v1/chats").json()["items"]}
    assert sessions[second]["title"] == "Second session"
    first_history = api.client.get(f"/v1/chats/{first}/messages").json()["items"]
    assert all(message["version_ids"] == [first_source.version] for message in first_history)
    second_history = api.client.get(f"/v1/chats/{second}/messages").json()["items"]
    assert all(message["chat_id"] == second for message in second_history)


def test_deleted_source_cannot_be_retrieved_or_reused_for_a_new_answer(api):
    source = ready_source()
    chat = create_chat(api, [source])
    answer = ask(api, chat).json()
    assert answer["status"] == "complete"
    deleted = api.client.delete(f"/v1/documents/{source.document}")
    assert deleted.status_code in {202, 204}, deleted.text
    assert api.client.get(f"/v1/versions/{source.version}/content").status_code == 404
    assert api.client.get(f"/v1/versions/{source.version}/chunks/{source.chunk}").status_code == 404
    unavailable = ask(api, chat)
    assert unavailable.status_code == 404, unavailable.text
    assert len(api.ai.calls) == 1
    history = api.client.get(f"/v1/chats/{chat}/messages").json()["items"]
    assert history[-1]["content"] == answer["content"]
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 2


def test_overlapping_requests_admit_only_one_generation(api):
    source = ready_source()
    chat = create_chat(api, [source])
    api.ai.block = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(ask, api, chat, key="first-running")
        try:
            assert api.ai.started.wait(5), "First generation never reached the provider."
            conflict = pool.submit(ask, api, chat, key="second-running").result(timeout=5)
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["code"] == "generation_active"
        finally:
            api.ai.release.set()
        assert running.result(timeout=5).json()["status"] == "complete"
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 2
    assert len(api.ai.calls) == 1


def test_cancel_fences_active_answer_and_allows_explicit_retry(api):
    source = ready_source()
    chat = create_chat(api, [source])
    api.ai.block = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(ask, api, chat)
        try:
            assert api.ai.started.wait(5)
            history = api.client.get(f"/v1/chats/{chat}/messages").json()["items"]
            active = history[-1]["id"]
            cancelled = api.client.post(f"/v1/chats/{chat}/messages/{active}/cancel")
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        finally:
            api.ai.release.set()
        assert running.result(timeout=5).json()["status"] == "cancelled"
    api.ai.block = False
    retry = api.client.post(
        f"/v1/chats/{chat}/messages/{active}/retry", headers={"Idempotency-Key": "after-cancel"}
    )
    assert retry.status_code == 200 and retry.json()["status"] == "complete", retry.text
    assert retry.json()["version_ids"] == [source.version]
    assert api.client.get(f"/v1/chats/{chat}/messages/{active}").json()["status"] == "cancelled"


def test_deletion_during_generation_prevents_a_completed_answer(api):
    source = ready_source()
    chat = create_chat(api, [source])
    api.ai.block = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(ask, api, chat)
        try:
            assert api.ai.started.wait(5)
            deleted = api.client.delete(f"/v1/documents/{source.document}")
            assert deleted.status_code in {202, 204}
        finally:
            api.ai.release.set()
        result = running.result(timeout=5).json()
    assert result["status"] == "failed" and result["citations"] == []
    assert api.client.get(f"/v1/chats/{chat}/messages/{result['id']}").json()["status"] == "failed"


def test_cancellation_waiting_for_completion_cannot_overwrite_the_winner(api):
    source = ready_source()
    chat = create_chat(api, [source])
    with session() as db, db.begin():
        user = Message(chat_id=chat, role="user", content="Question", version_ids=[source.version])
        db.add(user)
        db.flush()
        assistant = Message(
            chat_id=chat,
            role="assistant",
            parent_id=user.id,
            status="streaming",
            version_ids=[source.version],
        )
        db.add(assistant)
        db.flush()
        identifier = assistant.id
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session() as finalizer, finalizer.begin():
            assistant = finalizer.get(Message, identifier, with_for_update=True)
            assistant.status, assistant.content = "complete", "Committed answer"
            finalizer.flush()
            cancel = pool.submit(api.client.post, f"/v1/chats/{chat}/messages/{identifier}/cancel")
            # Observe a real database lock wait, not a scheduling-delay assumption.
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline and not cancel.done():
                with session() as observer:
                    blocked = bool(
                        observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                                "AND cardinality(pg_blocking_pids(pid)) > 0 "
                                "AND query ILIKE '%messages%')"
                            )
                        )
                    )
                if blocked:
                    break
                time.sleep(0.01)
        response = cancel.result(timeout=5)
    assert blocked, "Cancellation never overlapped the uncommitted completion."
    assert response.status_code == 200 and response.json()["status"] == "complete", response.text
    with session() as db:
        persisted = db.get(Message, identifier)
        assert persisted.status == "complete" and persisted.content == "Committed answer"


def test_selection_counts_are_not_limited_to_old_chat_or_comparison_caps(api):
    sources = [ready_source(f"source-{index}.txt", f"Fact number {index}.") for index in range(11)]
    chat = create_chat(api, sources)
    with session() as db:
        assert len(db.get(Chat, chat).version_ids) == 11
    comparison = api.client.post(
        "/v1/comparisons",
        json={"version_ids": [source.version for source in sources[:5]], "dimensions": ["Terms"]},
    )
    assert comparison.status_code == 202, comparison.text
