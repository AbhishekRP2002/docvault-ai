from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from docvault.cache import notify_change
from docvault.chat import (
    cancel_message,
    generate_message,
    message_events,
    message_response,
    require_chat,
    reserve_message,
    visible_messages,
)
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.errors import AppError
from docvault.limits import enforce_rate
from docvault.models import Chat, Message, now
from docvault.schemas import ChatCreate, ChatUpdate, MessageCreate

router = APIRouter(prefix="/v1/chats", tags=["chats"])
RequestKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def chat_response(chat):
    return dict(
        id=chat.id,
        title=chat.title,
        version_ids=chat.version_ids,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


@router.get("")
def chats():
    with session() as db:
        return {
            "items": [
                chat_response(chat)
                for chat in db.scalars(select(Chat).order_by(Chat.updated_at.desc()))
            ]
        }


@router.post("", status_code=201)
def create_chat(body: ChatCreate):
    with session() as db, db.begin():
        versions = require_versions(db, body.version_ids)
        chat = Chat(title=body.title.strip() or "New chat", version_ids=[v.id for v in versions])
        db.add(chat)
        db.flush()
        return chat_response(chat)


@router.patch("/{chat_id}")
def update_chat(chat_id: str, body: ChatUpdate):
    with session() as db, db.begin():
        chat = require_chat(db, chat_id, lock=True)
        if body.title is not None:
            if not body.title.strip():
                raise AppError(422, "invalid_title", "Enter a session name.")
            chat.title = body.title.strip()
        if body.version_ids is not None:
            # Running turns already hold an immutable scope snapshot.
            chat.version_ids = [v.id for v in require_versions(db, body.version_ids)]
        chat.updated_at = now()
        result = chat_response(chat)
    notify_change()
    return result


@router.delete("/{chat_id}", status_code=204)
def delete_chat(chat_id: str):
    with session() as db, db.begin():
        chat = require_chat(db, chat_id, lock=True)
        active = db.scalar(
            select(Message.id).where(
                Message.chat_id == chat_id, Message.status.in_(["pending", "streaming"])
            )
        )
        if active:
            raise AppError(
                409, "generation_active", "Stop the current response before deleting this session."
            )
        db.delete(chat)
    notify_change()
    return Response(status_code=204)


@router.get("/{chat_id}/messages")
def history(chat_id: str):
    with session() as db:
        require_chat(db, chat_id)
        return {"items": [message_response(m) for m in visible_messages(db, chat_id)]}


@router.get("/{chat_id}/messages/{message_id}")
def message(chat_id: str, message_id: str):
    with session() as db:
        require_chat(db, chat_id)
        item = db.get(Message, message_id)
        if not item or item.chat_id != chat_id:
            raise AppError(404, "message_not_found", "Message not found.")
        return message_response(item)


async def respond(
    request: Request,
    chat_id: str,
    content: str | None,
    key: str | None,
    retry_of: str | None = None,
):
    settings = get_settings()
    if not settings.openrouter_api_key.get_secret_value():
        raise AppError(
            503,
            "provider_not_configured",
            "Set OPENROUTER_API_KEY on the backend before asking questions.",
        )
    enforce_rate("chat", settings.chat_rate_per_minute, 60)
    identifier, replay = reserve_message(chat_id, content, key or str(uuid4()), retry_of)
    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            message_events(identifier, replay),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if not replay:

        async def ignore_event(name, payload):
            pass

        await generate_message(identifier, ignore_event)
    with session() as db:
        return message_response(db.get(Message, identifier))


@router.post("/{chat_id}/messages")
async def ask(
    chat_id: str, body: MessageCreate, request: Request, idempotency_key: RequestKey = None
):
    return await respond(request, chat_id, body.content, idempotency_key)


@router.post("/{chat_id}/messages/{message_id}/retry")
async def retry(
    chat_id: str, message_id: str, request: Request, idempotency_key: RequestKey = None
):
    return await respond(request, chat_id, None, idempotency_key, message_id)


@router.post("/{chat_id}/messages/{message_id}/cancel")
async def cancel(chat_id: str, message_id: str):
    return cancel_message(chat_id, message_id)
