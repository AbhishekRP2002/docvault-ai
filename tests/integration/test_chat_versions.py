"""Verify active document replacement without rewriting historical turn snapshots."""

import hashlib
import math
from uuid import uuid4

import pytest
from sqlalchemy import select
from support import require_persisted_row
from test_api import api as api
from test_api import ask, create_chat, ready_source
from test_api import test_database_url as test_database_url

from docvault import chat as chat_module
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.models import Chat, Chunk, Document, Message, Version, now

pytestmark = pytest.mark.integration


def upload_replacement(api, source, *, activate: bool):
    """Accept an explicit new version and optionally seed its completed index without a worker."""
    content = "Payment is due in 60 days."
    response = api.client.post(
        f"/v1/documents/{source.document}/versions",
        files={"file": ("terms.txt", content.encode(), "text/plain")},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["latest_version_id"]
    if activate:
        with session() as db, db.begin():
            version = require_persisted_row(db, Version, identifier)
            version.status = "ready"
            version.ready_at = now()
            version.embedding_model = get_settings().openrouter_embedding_model
            version.chunk_count = 1
            db.add(
                Chunk(
                    version_id=identifier,
                    ordinal=0,
                    text=content,
                    embedding_text=content,
                    input_hash=hashlib.sha256(content.encode()).hexdigest(),
                    location={"line_start": 1},
                    token_count=8,
                    embedding=[1.0] + [0.0] * 1535,
                )
            )
            require_persisted_row(db, Document, source.document).current_version_id = identifier
    return identifier


def test_new_questions_follow_active_version_while_retry_and_history_stay_pinned(api):
    source = ready_source()
    chat_id = create_chat(api, [source])
    first = ask(api, chat_id, question="When is payment due?").json()
    assert first["version_ids"] == [source.version]
    assert "30 days" in first["content"]
    replacement_id = upload_replacement(api, source, activate=True)
    listed = api.client.get("/v1/chats")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["version_ids"] == [replacement_id]
    with session() as db:
        # Read projections do not rewrite stored turn or session snapshots.
        assert require_persisted_row(db, Chat, chat_id).version_ids == [source.version]

    retry = api.client.post(
        f"/v1/chats/{chat_id}/messages/{first['id']}/retry",
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["version_ids"] == [source.version]
    assert "30 days" in retry.json()["content"]

    next_turn = ask(api, chat_id, question="When is payment due?")
    assert next_turn.status_code == 200, next_turn.text
    assert next_turn.json()["version_ids"] == [replacement_id]
    assert "60 days" in next_turn.json()["content"]
    assert "30 days" not in next_turn.json()["content"]
    assert {citation["version_id"] for citation in next_turn.json()["citations"]} == {
        replacement_id
    }
    with session() as db:
        assert require_persisted_row(db, Chat, chat_id).version_ids == [replacement_id]
        original = require_persisted_row(db, Message, first["id"])
        assert original.version_ids == [source.version] and "30 days" in original.content
        user = db.scalar(
            select(Message)
            .where(Message.chat_id == chat_id, Message.role == "user")
            .order_by(Message.created_at)
        )
        assert user is not None and user.version_ids == [source.version]


def test_chat_selection_collapses_same_document_versions_but_comparison_retains_them(api):
    source = ready_source()
    replacement_id = upload_replacement(api, source, activate=True)
    selection = [source.version, replacement_id, source.version]
    created = api.client.post("/v1/chats", json={"version_ids": selection})
    assert created.status_code == 201, created.text
    assert created.json()["version_ids"] == [replacement_id]
    updated = api.client.patch(f"/v1/chats/{created.json()['id']}", json={"version_ids": selection})
    assert updated.status_code == 200 and updated.json()["version_ids"] == [replacement_id]
    with session() as db:
        assert [version.id for version in require_versions(db, selection)] == [
            source.version,
            replacement_id,
        ]
    compared = api.client.post(
        "/v1/comparisons",
        json={"version_ids": selection, "dimensions": ["Payment deadline"]},
    )
    assert compared.status_code == 202, compared.text


def test_pending_replacement_uses_previous_ready_version_until_activation(api):
    source = ready_source()
    pending_id = upload_replacement(api, source, activate=False)
    created = api.client.post("/v1/chats", json={"version_ids": [pending_id]})
    assert created.status_code == 201, created.text
    assert created.json()["version_ids"] == [source.version]
    answer = ask(api, created.json()["id"])
    assert answer.status_code == 200 and answer.json()["version_ids"] == [source.version]
    assert "30 days" in answer.json()["content"]


@pytest.mark.parametrize(
    "key,value",
    [("semantic_min_cosine_similarity", 0.8), ("pipeline", "changed-retrieval-policy")],
)
def test_retrieval_policy_change_invalidates_completed_answer_reuse(api, monkeypatch, key, value):
    """Reuse an identical source/question profile but call the provider after policy changes."""
    source = ready_source()
    first_id = create_chat(api, [source])
    assert ask(api, first_id).status_code == 200
    assert len(api.llm.calls) == 1
    second_id = create_chat(api, [source])
    assert ask(api, second_id).status_code == 200
    assert len(api.llm.calls) == 1
    monkeypatch.setitem(chat_module.RETRIEVAL_CONFIGURATION, key, value)
    third_id = create_chat(api, [source])
    assert ask(api, third_id).status_code == 200
    assert len(api.llm.calls) == 2


@pytest.mark.parametrize("similarity,question", [(0.65, "deadline"), (0.2, "payment")])
def test_semantic_or_lexical_evidence_reaches_agent_and_persisted_citation(
    api, similarity, question
):
    """Both relaxed semantic matches and independent keyword matches support real chat turns."""
    source = ready_source()
    with session() as db, db.begin():
        chunk = db.scalar(select(Chunk).where(Chunk.version_id == source.version))
        assert chunk is not None
        chunk.embedding = [similarity, math.sqrt(1 - similarity**2)] + [0.0] * 1534
        chunk_id = chunk.id
    chat_id = create_chat(api, [source])
    response = ask(api, chat_id, question=question)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "complete"
    assert "30 days" in result["content"]
    assert [item["chunk_id"] for item in result["citations"]] == [chunk_id]
    assert result["agent_trace"][0]["evidence_ids"] == [chunk_id]
    history = api.client.get(f"/v1/chats/{chat_id}/messages").json()["items"]
    assert history[-1] == result
