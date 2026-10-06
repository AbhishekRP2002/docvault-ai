"""Committed snapshots and multi-client WebSockets in disposable migrated schemas."""

from datetime import timedelta

import pytest
from alembic import command
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import insert, select, update
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocketDisconnect
from test_hybrid_retrieval import isolated_database as isolated_database
from test_hybrid_retrieval import migrated_database as migrated_database
from test_hybrid_retrieval import pytestmark as pytestmark

from docvault import db as database
from docvault import main
from docvault.models import Chat, Document, Job, Message, MetricBucket, WorkspaceRevision, now
from docvault.workspace_events import read_workspace_revision


def add_chat() -> str:
    """Commit a chat without sending Redis hints, representing missed notification delivery."""
    with database.session() as db, db.begin():
        chat = Chat(title="Revision fixture", version_ids=[])
        db.add(chat)
        db.flush()
        return chat.id


def receive_changed(socket, previous: str) -> dict:
    """Read a bounded number of periodic snapshots until the committed revision changes."""
    for _ in range(5):
        event = socket.receive_json()
        if event["revision"] != previous:
            return event
    pytest.fail("Committed change was not reconciled within five snapshots.")


def test_commit_rollback_noop_and_metrics_have_correct_revisions(migrated_database):
    """Only committed, observable changes increment the revision, once per transaction."""
    original = read_workspace_revision()
    with database.session() as db, db.begin():
        db.add(MetricBucket(key="requests", count=1, total_ms=1, max_ms=1))
    assert read_workspace_revision() == original
    with database.session() as db:
        db.add(Document(title="Rolled back"))
        db.flush()
        assert read_workspace_revision() == original
        db.rollback()
    assert read_workspace_revision() == original
    with database.session() as db, db.begin():
        chat = Chat(title="Committed", version_ids=[])
        db.add(chat)
        db.flush()
        db.add(Message(chat_id=chat.id, role="user", content="A question"))
        identifier = chat.id
    current = read_workspace_revision()
    assert current != original
    assert int(current.rsplit(":", 1)[1]) == int(original.rsplit(":", 1)[1]) + 1
    with database.session() as db, db.begin():
        db.execute(update(Chat).where(Chat.id == identifier).values(title="Committed"))
        db.execute(update(Chat).where(Chat.id == "absent").values(title="Ignored"))
    assert read_workspace_revision() == current


def test_out_of_order_commits_cannot_hide_a_change(migrated_database):
    """An older transaction committed after a newer one still changes the snapshot."""
    engine = migrated_database.engine
    with engine.connect() as older:
        transaction = older.begin()
        older.execute(insert(Document).values(title="Older transaction"))
        original = read_workspace_revision()
        with engine.begin() as newer:
            newer.execute(insert(Document).values(title="Newer transaction"))
        newer_revision = read_workspace_revision()
        assert newer_revision != original
        transaction.commit()
    assert read_workspace_revision() != newer_revision


def test_job_heartbeat_does_not_force_workspace_reload(migrated_database):
    """Lease refresh alone is not a visible state transition; stage changes are."""
    with database.session() as db, db.begin():
        job = Job(kind="ingest", resource_id="fixture", status="running", stage="conversion")
        db.add(job)
        db.flush()
        identifier = job.id
    original = read_workspace_revision()
    with database.session() as db, db.begin():
        db.execute(
            update(Job)
            .where(Job.id == identifier)
            .values(lease_until=now() + timedelta(seconds=120))
        )
    assert read_workspace_revision() == original
    with database.session() as db, db.begin():
        db.execute(update(Job).where(Job.id == identifier).values(stage="chunking"))
    assert read_workspace_revision() != original


def test_two_clients_reconcile_messages_and_reconnect_without_redis_hint(migrated_database):
    """Idle tabs observe committed messages through stable snapshots and reconnect."""
    identifier = add_chat()
    with TestClient(main.app) as client:
        with (
            client.websocket_connect("/v1/events") as first,
            client.websocket_connect("/v1/events") as second,
        ):
            original = first.receive_json()
            assert original["type"] == "snapshot"
            assert second.receive_json()["revision"] == original["revision"]
            assert first.receive_json()["revision"] == original["revision"]
            with database.session() as db, db.begin():
                db.add(Message(chat_id=identifier, role="assistant", content="Persisted answer"))
            changed = receive_changed(first, original["revision"])
            assert receive_changed(second, original["revision"])["revision"] == changed["revision"]
            messages = client.get(f"/v1/chats/{identifier}/messages").json()["items"]
            assert any(message["content"] == "Persisted answer" for message in messages)
        with client.websocket_connect("/v1/events") as reconnected:
            assert reconnected.receive_json()["revision"] == changed["revision"]


def test_revision_epoch_changes_on_reinitialization(migrated_database):
    """The generated migration seeds a fresh epoch when its table is re-created."""
    original = read_workspace_revision()
    command.downgrade(migrated_database.alembic, "4642d4fae103")
    command.upgrade(migrated_database.alembic, "head")
    assert read_workspace_revision().split(":")[0] != original.split(":")[0]
    with database.session() as db:
        assert db.scalar(select(WorkspaceRevision.transaction_id)) == 0


def test_websocket_rejects_untrusted_origin(migrated_database):
    """The revision channel retains the existing origin boundary."""
    with TestClient(main.app) as client:
        with pytest.raises(WebSocketDisconnect) as captured:
            with client.websocket_connect(
                "/v1/events", headers={"Origin": "https://untrusted.test"}
            ):
                pass
        assert captured.value.code == 1008


def test_database_failure_closes_channel_for_polling_fallback(migrated_database, monkeypatch):
    """A revision read failure cannot falsely advertise an up-to-date connection."""

    def unavailable():
        """Fail only the revision observation, without stopping any real service."""
        raise OperationalError("SELECT revision", {}, Exception("confidential"))

    monkeypatch.setattr(main, "read_workspace_revision", unavailable)
    with TestClient(main.app) as client, client.websocket_connect("/v1/events") as socket:
        with pytest.raises(WebSocketDisconnect) as captured:
            socket.receive_json()
        assert captured.value.code == 1013


def test_sql_snapshots_reconcile_while_redis_is_unavailable(migrated_database, monkeypatch):
    """Redis wake-up delivery is optional; committed SQL changes still reach connected tabs."""

    class UnavailableSubscriber:
        """Simulate a Redis outage without changing any running service."""

        async def __aenter__(self):
            """Return the bounded subscription fixture."""
            return self

        async def __aexit__(self, *args):
            """No connection resources are allocated."""

        async def subscribe(self, *args):
            """Subscription fails throughout the outage."""
            raise RedisConnectionError("Fixture outage")

        async def get_message(self, **kwargs):
            """Hint delivery also fails while SQL remains available."""
            raise RedisConnectionError("Fixture outage")

    class UnavailableClient:
        """Provide the existing async Redis boundary with no network access."""

        def pubsub(self):
            """Return a failed subscription context."""
            return UnavailableSubscriber()

        async def aclose(self):
            """No actual client needs cleanup."""

    monkeypatch.setattr(main.AsyncRedis, "from_url", lambda *args, **kwargs: UnavailableClient())
    with TestClient(main.app) as client, client.websocket_connect("/v1/events") as socket:
        original = socket.receive_json()
        add_chat()
        changed = receive_changed(socket, original["revision"])
        assert changed["type"] == "snapshot"
