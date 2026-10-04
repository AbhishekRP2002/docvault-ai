import asyncio
import contextlib
import json

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from docvault.cache import (
    cache_get,
    cache_set,
    calculate_json_fingerprint,
    count_metric,
    notify_change,
)
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.errors import AppError
from docvault.llm.graphs import run_document_chat_workflow
from docvault.llm.prompts import build_generation_identity
from docvault.llm.provider import ProviderError
from docvault.llm.types import ChatGenerationLLMResponse, Evidence
from docvault.models import Chat, Message, now
from docvault.retrieval import build_public_citation, retrieve_relevant_chunks

ACTIVE_TASKS: dict[str, asyncio.Task] = {}


def require_chat(db, chat_id: str, lock=False) -> Chat:
    """Return a chat, optionally row-locking it, or raise a not-found API error."""
    query = select(Chat).where(Chat.id == chat_id)
    if lock:
        query = query.with_for_update()
    chat = db.scalar(query)
    if not chat:
        raise AppError(404, "chat_not_found", "Chat session not found.")
    return chat


def serialize_message_response(message: Message) -> dict:
    """Build the public message payload with status, sources, suggestions, and outcome."""
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


def load_visible_chat_messages(db, chat_id: str) -> list[Message]:
    """Return each user turn and its newest assistant attempt in chronological order."""
    messages = list(
        db.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at))
    )
    latest = {m.parent_id: m.id for m in messages if m.role == "assistant"}
    return [m for m in messages if m.role == "user" or latest.get(m.parent_id) == m.id]


def reserve_assistant_response(
    chat_id: str, content: str | None, request_key: str, retry_of: str | None = None
):
    """Admit one assistant attempt with idempotency and ready-source checks.

    Return its ID and a replay flag; regeneration uses only the latest turn's original sources.
    """
    fingerprint = calculate_json_fingerprint([content, retry_of])
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
    """Persist cancellation of an active reply and cancel its local task when present.

    Return the stored status so clients do not infer cancellation from a stop request alone.
    """
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
        result = serialize_message_response(message)
    notify_change()
    if cancelled and (task := ACTIVE_TASKS.get(message_id)):
        task.cancel()
    return result


def mark_message_failed(message_id: str, error: str, status="failed") -> dict | None:
    """Record failure or cancellation only while the message is still active.

    Return its persisted payload, or None if it no longer exists.
    """
    with session() as db, db.begin():
        message = db.get(Message, message_id, with_for_update=True)
        if not message:
            return None
        if message.status in {"pending", "streaming"}:
            message.status, message.error, message.updated_at = status, error, now()
        result = serialize_message_response(message)
    notify_change()
    return result


async def generate_assistant_response(message_id: str, emit_generation_event):
    """Stream a reserved answer, validate it, and persist its canonical completion.

    Regeneration bypasses answer reuse; cancellation or deleted sources prevent late publication.
    """
    from docvault.integrations import create_llm_client

    with session() as db, db.begin():
        message = db.get(Message, message_id, with_for_update=True)
        if not message or message.status != "pending":
            return
        message.status = "streaming"
        message.updated_at = now()
        user = db.get(Message, message.parent_id)
        history = [
            dict(role=m.role, content=m.content)
            for m in load_visible_chat_messages(db, message.chat_id)
            if m.created_at < user.created_at and m.status == "complete"
        ]
        # Retain persisted history; send recent turns without a fixed evidence-token budget.
        history = history[-20:]
        question, version_ids = user.content, list(message.version_ids)
        is_retry = message.request_key.startswith("retry:")
    llm = create_llm_client(message_id)

    async def emit_response_delta(value: str):
        """Recheck active status, refresh its heartbeat, and emit a provisional text delta."""
        with session() as db, db.begin():
            current = db.get(Message, message_id)
            if not current or current.status != "streaming":
                raise asyncio.CancelledError
            current.updated_at = now()
        await emit_generation_event("answer.delta", {"text": value})

    try:
        settings = get_settings()
        with session() as db:
            versions = require_versions(db, version_ids)
            key = "answer:" + calculate_json_fingerprint(
                [
                    "chat-v2",
                    [(v.id, v.sha256, v.embedding_model) for v in versions],
                    question,
                    history,
                    [
                        build_generation_identity(llm.generation_model(task), task)
                        for task in ("chat", "rewrite")
                    ],
                    settings.openrouter_embedding_model,
                ]
            )
        cached = None if is_retry else cache_get(key)
        if cached:
            answer = ChatGenerationLLMResponse.model_validate(cached["answer"])
            evidence = [Evidence.model_validate(item) for item in cached["evidence"]]
            rewritten = cached["query"]
            count_metric("answer_cache_hits")
            # A cache hit returns its completed result; do not pretend to stream model tokens.
        else:

            async def retrieve_turn_evidence(query: str):
                """Retrieve evidence only from the versions captured by this assistant attempt."""
                return await retrieve_relevant_chunks(query, version_ids, llm)

            answer, evidence, rewritten = await run_document_chat_workflow(
                question, history, retrieve_turn_evidence, llm, emit_response_delta
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
                build_public_citation(selected[identifier]) for identifier in answer.citation_ids
            ]
            current.outcome, current.rewritten_query = answer.outcome, rewritten
            current.status, current.updated_at = "complete", now()
            result = serialize_message_response(current)
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
        await emit_generation_event("answer.completed", {"message": result})
    except asyncio.CancelledError:
        result = mark_message_failed(
            message_id, "Generation stopped. You can retry this response.", "cancelled"
        )
        if result:
            await emit_generation_event("message.failed", {"message": result})
        raise
    except Exception as exc:
        # Provider exceptions are sanitized at the boundary; don't expose arbitrary tracebacks.
        safe = (
            str(exc)
            if isinstance(exc, (AppError, ProviderError, ValueError))
            else "The response could not be completed. Please retry."
        )
        result = mark_message_failed(message_id, safe)
        if result:
            await emit_generation_event("message.failed", {"message": result})
    finally:
        await llm.close()


def encode_sse_event(name: str, data: dict) -> str:
    """Encode a named SSE event with a JSON payload using API-compatible value conversion."""
    return f"event: {name}\ndata: {json.dumps(jsonable_encoder(data), ensure_ascii=False)}\n\n"


async def stream_message_events(message_id: str, replay=False):
    """Yield admission, response, and terminal SSE events for an assistant attempt.

    Replay stored terminal results; cancel a newly started local generation on disconnect.
    """
    with session() as db:
        message = db.get(Message, message_id)
        user = db.get(Message, message.parent_id)
        started = {
            "message": serialize_message_response(message),
            "user_message": serialize_message_response(user),
        }
        status = message.status
    yield encode_sse_event("message.started", started)
    if replay:
        if status == "complete":
            yield encode_sse_event("answer.completed", {"message": started["message"]})
        elif status in {"failed", "cancelled"}:
            yield encode_sse_event("message.failed", {"message": started["message"]})
        # Active replay doesn't start another generation. Client recovers via GET history.
        return
    queue = asyncio.Queue()

    async def enqueue_response_event(name, payload):
        """Queue a generation event for the SSE iterator to deliver."""
        await queue.put((name, payload))

    async def run_generation_and_close_queue():
        """Run generation and always signal the end of its event queue."""
        try:
            await generate_assistant_response(message_id, enqueue_response_event)
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_generation_and_close_queue())
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
            yield encode_sse_event(*event)
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        ACTIVE_TASKS.pop(message_id, None)
