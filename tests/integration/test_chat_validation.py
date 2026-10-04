"""Check nullable chat boundaries against isolated real database schemas."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support import require_persisted_row
from test_api import api as api
from test_api import create_chat, ready_source
from test_api import test_database_url as test_database_url

from docvault.chat import (
    generate_assistant_response,
    require_message,
    reserve_assistant_response,
    stream_message_events,
)
from docvault.db import session
from docvault.errors import AppError
from docvault.models import Chat, Message

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("content", [None, "", " \n\t "])
def test_empty_question_is_rejected_without_persisting_a_turn(api, content):
    chat_id = create_chat(api, [ready_source()])
    with pytest.raises(AppError) as failure:
        reserve_assistant_response(chat_id, content, str(uuid4()))
    assert (failure.value.status, failure.value.code) == (422, "invalid_message")
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        assert require_persisted_row(db, Chat, chat_id).title == "New chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_kind", ["null", "missing", "wrong_chat", "wrong_role"])
async def test_invalid_parent_fails_before_provider_work_and_persists_failure(api, parent_kind):
    source = ready_source()
    chat_id = create_chat(api, [source])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, db.begin():
        message = require_persisted_row(db, Message, message_id)
        if parent_kind == "null":
            message.parent_id = None
        elif parent_kind == "missing":
            message.parent_id = str(uuid4())
        else:
            parent_chat = create_chat(api, [source]) if parent_kind == "wrong_chat" else chat_id
            parent = Message(
                chat_id=parent_chat,
                role="user" if parent_kind == "wrong_chat" else "assistant",
                content="Earlier message",
            )
            db.add(parent)
            db.flush()
            message.parent_id = parent.id
    events = []

    async def record_event(name, payload):
        events.append((name, payload))

    await generate_assistant_response(message_id, record_event)
    assert api.llm.calls == []
    with session() as db:
        message = require_persisted_row(db, Message, message_id)
        assert message.status == "failed" and message.error
        assert message.content == ""
    assert len(events) == 1
    assert events[0][0] == "message.failed"
    assert events[0][1]["message"]["status"] == "failed"


@pytest.mark.asyncio
async def test_legacy_attempt_without_request_key_generates_a_persisted_reply(api):
    chat_id = create_chat(api, [ready_source()])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, db.begin():
        require_persisted_row(db, Message, message_id).request_key = None
    events = []

    async def record_event(name, payload):
        events.append((name, payload))

    await generate_assistant_response(message_id, record_event)
    with session() as db:
        message = require_persisted_row(db, Message, message_id)
        assert message.status == "complete"
        assert "30 days" in message.content
        assert message.citations
    assert events[-1][0] == "answer.completed"
    assert len(api.llm.calls) == 1


def test_retry_without_an_original_user_turn_rejects_without_another_attempt(api):
    chat_id = create_chat(api, [ready_source()])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, db.begin():
        message = require_persisted_row(db, Message, message_id)
        assert message.parent_id is not None
        parent = require_persisted_row(db, Message, message.parent_id)
        message.status = "failed"
        db.delete(parent)
    with pytest.raises(AppError) as failure:
        reserve_assistant_response(chat_id, None, str(uuid4()), retry_of=message_id)
    assert (failure.value.status, failure.value.code) == (409, "retry_latest_turn")
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 1


@pytest.mark.asyncio
async def test_missing_message_is_rejected_by_lookup_and_sse_before_start_event(api):
    missing_id = str(uuid4())
    with session() as db, pytest.raises(AppError) as lookup:
        require_message(db, missing_id)
    assert lookup.value.status == 404
    stream = stream_message_events(missing_id)
    with pytest.raises(AppError) as failure:
        await anext(stream)
    assert (failure.value.status, failure.value.code) == (404, "message_not_found")


def test_message_lookup_rejects_another_chat_scope(api):
    source = ready_source()
    chat_id = create_chat(api, [source])
    other_id = create_chat(api, [source])
    message_id, _ = reserve_assistant_response(chat_id, "When is payment due?", str(uuid4()))
    with session() as db, pytest.raises(AppError) as failure:
        require_message(db, message_id, other_id)
    assert failure.value.status == 404
