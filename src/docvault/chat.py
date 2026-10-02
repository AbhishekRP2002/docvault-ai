import asyncio
import contextlib
import json

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from docvault.ai.graphs import run_chat
from docvault.ai.provider import ProviderError
from docvault.ai.types import Answer, Evidence
from docvault.cache import cache_get, cache_set, count_metric, notify_change, signature
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.errors import AppError
from docvault.models import Chat, Message, now
from docvault.retrieval import citation, retrieve

ACTIVE_TASKS: dict[str, asyncio.Task] = {}


def require_chat(db, chat_id: str, lock=False) -> Chat:
    query = select(Chat).where(Chat.id == chat_id)
    if lock:
        query = query.with_for_update()
    chat = db.scalar(query)
    if not chat:
        raise AppError(404, "chat_not_found", "Chat session not found.")
    return chat


def message_response(message: Message) -> dict:
    return dict(
        id=message.id,
        chat_id=message.chat_id,
        role=message.role,
        status=message.status,
        content=message.content,
        suggestions=message.suggestions,
        citations=message.citations,
        error=message.error,
        created_at=message.created_at,
        parent_id=message.parent_id,
        version_ids=message.version_ids,
        outcome=message.outcome,
    )


def visible_messages(db, chat_id: str) -> list[Message]:
    messages = list(
        db.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at))
    )
    latest = {m.parent_id: m.id for m in messages if m.role == "assistant"}
    return [m for m in messages if m.role == "user" or latest.get(m.parent_id) == m.id]


def reserve_message(
    chat_id: str, content: str | None, request_key: str, retry_of: str | None = None
):
    fingerprint = signature([content, retry_of])
    request_key = f"retry:{retry_of}:{request_key}" if retry_of else f"new:{request_key}"
    with session() as db, db.begin():
        chat = require_chat(db, chat_id, lock=True)
        existing = db.scalar(
            select(Message).where(Message.chat_id == chat_id, Message.request_key == request_key)
        )
        if existing:
            if existing.request_hash != fingerprint:
                raise AppError(
                    409,
                    "idempotency_conflict",
                    "This request key was already used for another message.",
                )
            return existing.id, True
        active = db.scalar(
            select(Message).where(
                Message.chat_id == chat_id,
                Message.role == "assistant",
                Message.status.in_(["pending", "streaming"]),
            )
        )
        if active:
            raise AppError(
                409, "generation_active", "A response is already running. Stop it before retrying."
            )
        if retry_of:
            previous = db.get(Message, retry_of)
            if not previous or previous.chat_id != chat_id or previous.role != "assistant":
                raise AppError(404, "message_not_found", "Response not found.")
            user = db.get(Message, previous.parent_id)
            last_user = db.scalar(
                select(Message)
                .where(Message.chat_id == chat_id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            if not user or user.id != last_user.id:
                raise AppError(
                    409,
                    "retry_latest_turn",
                    "Regenerate the latest reply, or create a new chat for an earlier question.",
                )
            versions = previous.version_ids
        else:
            versions = chat.version_ids
            user = Message(
                chat_id=chat_id, role="user", content=content.strip(), version_ids=versions
            )
            db.add(user)
            db.flush()
            if chat.title == "New chat":
                chat.title = content.strip()[:80]
        require_versions(db, versions)
        assistant = Message(
            chat_id=chat_id,
            role="assistant",
            status="pending",
            parent_id=user.id,
            version_ids=versions,
            request_key=request_key,
            request_hash=fingerprint,
        )
        db.add(assistant)
        chat.updated_at = now()
        db.flush()
        identifier = assistant.id
    notify_change()
    return identifier, False


def cancel_message(chat_id: str, message_id: str) -> dict:
    cancelled = False
    with session() as db, db.begin():
        require_chat(db, chat_id, lock=True)
        message = db.get(Message, message_id, with_for_update=True)
        if not message or message.chat_id != chat_id or message.role != "assistant":
            raise AppError(404, "message_not_found", "Response not found.")
        if message.status in {"pending", "streaming"}:
            cancelled = True
            message.status = "cancelled"
            message.error = "Generation stopped. You can retry this response."
            message.updated_at = now()
        result = message_response(message)
    notify_change()
    if cancelled and (task := ACTIVE_TASKS.get(message_id)):
        task.cancel()
    return result


def set_failed(message_id: str, error: str, status="failed") -> dict | None:
    with session() as db, db.begin():
        message = db.get(Message, message_id, with_for_update=True)
        if not message:
            return None
        if message.status in {"pending", "streaming"}:
            message.status, message.error, message.updated_at = status, error, now()
        result = message_response(message)
    notify_change()
    return result


async def generate_message(message_id: str, emit):
    from docvault.integrations import create_ai

    with session() as db, db.begin():
        message = db.get(Message, message_id, with_for_update=True)
        if not message or message.status != "pending":
            return
        message.status = "streaming"
        message.updated_at = now()
        user = db.get(Message, message.parent_id)
        history = [
            dict(role=m.role, content=m.content)
            for m in visible_messages(db, message.chat_id)
            if m.created_at < user.created_at and m.status == "complete"
        ]
        # Retain persisted history; send recent turns without a fixed evidence-token budget.
        history = history[-20:]
        question, version_ids = user.content, list(message.version_ids)
        is_retry = message.request_key.startswith("retry:")
    ai = create_ai(message_id)

    async def on_delta(value: str):
        with session() as db, db.begin():
            current = db.get(Message, message_id)
            if not current or current.status != "streaming":
                raise asyncio.CancelledError
            current.updated_at = now()
        await emit("answer.delta", {"text": value})

    try:
        settings = get_settings()
        with session() as db:
            versions = require_versions(db, version_ids)
            key = "answer:" + signature(
                [
                    "chat-v1",
                    [(v.id, v.sha256, v.embedding_model) for v in versions],
                    question,
                    history,
                    settings.openrouter_chat_model,
                    settings.openrouter_embedding_model,
                ]
            )
        cached = None if is_retry else cache_get(key)
        if cached:
            answer = Answer.model_validate(cached["answer"])
            evidence = [Evidence.model_validate(item) for item in cached["evidence"]]
            rewritten = cached["query"]
            count_metric("answer_cache_hits")
            # A cache hit returns its completed result; do not pretend to stream model tokens.
        else:

            async def find_evidence(query: str):
                return await retrieve(query, version_ids, ai)

            answer, evidence, rewritten = await run_chat(
                question, history, find_evidence, ai, on_delta
            )
        selected = {item.id: item for item in evidence}
        with session() as db, db.begin():
            current = db.get(Message, message_id, with_for_update=True)
            if not current or current.status != "streaming":
                raise asyncio.CancelledError
            require_versions(db, version_ids)
            current.content = answer.response
            current.suggestions = answer.suggestions
            current.citations = [
                citation(selected[identifier]) for identifier in answer.citation_ids
            ]
            current.outcome, current.rewritten_query = answer.outcome, rewritten
            current.status, current.updated_at = "complete", now()
            result = message_response(current)
        if not cached and answer.outcome == "answered":
            cache_set(
                key,
                {
                    "answer": answer.model_dump(),
                    "evidence": [e.model_dump() for e in evidence],
                    "query": rewritten,
                },
            )
        notify_change()
        await emit("answer.completed", {"message": result})
    except asyncio.CancelledError:
        result = set_failed(
            message_id, "Generation stopped. You can retry this response.", "cancelled"
        )
        if result:
            await emit("message.failed", {"message": result})
        raise
    except Exception as exc:
        # Provider exceptions are sanitized at the boundary; don't expose arbitrary tracebacks.
        safe = (
            str(exc)
            if isinstance(exc, (AppError, ProviderError, ValueError))
            else "The response could not be completed. Please retry."
        )
        result = set_failed(message_id, safe)
        if result:
            await emit("message.failed", {"message": result})
    finally:
        await ai.close()


def encode_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(jsonable_encoder(data), ensure_ascii=False)}\n\n"


async def message_events(message_id: str, replay=False):
    with session() as db:
        message = db.get(Message, message_id)
        user = db.get(Message, message.parent_id)
        started = {"message": message_response(message), "user_message": message_response(user)}
        status = message.status
    yield encode_event("message.started", started)
    if replay:
        if status == "complete":
            yield encode_event("answer.completed", {"message": started["message"]})
        elif status in {"failed", "cancelled"}:
            yield encode_event("message.failed", {"message": started["message"]})
        # Active replay doesn't start another generation. Client recovers via GET history.
        return
    queue = asyncio.Queue()

    async def emit(name, payload):
        await queue.put((name, payload))

    async def run():
        try:
            await generate_message(message_id, emit)
        finally:
            await queue.put(None)

    task = asyncio.create_task(run())
    ACTIVE_TASKS[message_id] = task
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event is None:
                break
            yield encode_event(*event)
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        ACTIVE_TASKS.pop(message_id, None)
