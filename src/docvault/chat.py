import asyncio
import contextlib
import json

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from docvault.cache import (
    cache_get,
    cache_set,
    calculate_json_fingerprint,
    count_metric,
    notify_change,
)
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_document, require_version, require_versions
from docvault.errors import AppError
from docvault.llm.graphs import run_document_agent_workflow, validate_document_agent_answer
from docvault.llm.history import build_conversation_summary_turn, compact_chat_history
from docvault.llm.models import ConversationSummaryLLMResponse, Evidence
from docvault.llm.prompts import (
    CHAT_SYSTEM_PROMPT,
    build_generation_identity,
)
from docvault.llm.provider import ProviderError
from docvault.models import Chat, Message, Version, now
from docvault.retrieval import (
    RETRIEVAL_CONFIGURATION,
    build_public_citation,
)
from docvault.tools.definitions import build_agent_tool_catalog_prompt, build_agent_tool_definitions
from docvault.tools.models import AgentChatGenerationLLMResponse
from docvault.tools.runtime import DocumentToolRuntime

ACTIVE_TASKS: dict[str, asyncio.Task] = {}


def require_chat(db: Session, chat_id: str, lock: bool = False) -> Chat:
    """Return a chat, optionally row-locking it, or raise a not-found API error."""
    query = select(Chat).where(Chat.id == chat_id)
    if lock:
        query = query.with_for_update()
    chat = db.scalar(query)
    if not chat:
        raise AppError(404, "chat_not_found", "Chat session not found.")
    return chat


def require_message(
    db: Session, message_id: str | None, chat_id: str | None = None, *, lock: bool = False
) -> Message:
    """Return an existing message in the optional chat scope, or raise a not-found error."""
    message = db.get(Message, message_id, with_for_update=lock) if message_id is not None else None
    if message is None or (chat_id is not None and message.chat_id != chat_id):
        raise AppError(404, "message_not_found", "Message not found.")
    return message


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
        agent_trace=message.agent_trace,
    )


def load_visible_chat_messages(db: Session, chat_id: str) -> list[Message]:
    """Return each user turn and its newest assistant attempt in chronological order."""
    messages = list(
        db.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.created_at))
    )
    latest = {m.parent_id: m.id for m in messages if m.role == "assistant"}
    return [m for m in messages if m.role == "user" or latest.get(m.parent_id) == m.id]


def require_chat_versions(
    db: Session, version_ids: list[str], *, ready: bool = True
) -> list[Version]:
    """Allow source-free chat while validating every explicitly selected version."""
    return require_versions(db, version_ids, ready=ready) if version_ids else []


def require_current_chat_versions(db: Session, version_ids: list[str]) -> list[Version]:
    """Resolve each selected document to one active ready version for a new chat turn.

    Older input IDs identify the document, not a pinned historical Q&A source. Historical
    messages, retries and explicit comparison/summary requests retain their version IDs.
    """
    selected = require_chat_versions(db, version_ids, ready=False)
    versions, document_ids = [], set()
    for selection in selected:
        if selection.document_id in document_ids:
            continue
        document = require_document(db, selection.document_id)
        if document.current_version_id is None:
            raise AppError(409, "document_not_ready", "This document has no ready version yet.")
        current = require_version(db, document.current_version_id, ready=True)
        if current.document_id != document.id:
            raise AppError(409, "document_not_ready", "The document's active version is invalid.")
        versions.append(current)
        document_ids.add(document.id)
    return versions


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
            user = db.get(Message, previous.parent_id) if previous.parent_id is not None else None
            last_user = db.scalar(
                select(Message)
                .where(Message.chat_id == chat_id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            if (
                user is None
                or last_user is None
                or user.id != last_user.id
                or user.chat_id != chat_id
                or user.role != "user"
            ):
                raise AppError(
                    409,
                    "retry_latest_turn",
                    "Regenerate the latest reply, or create a new chat for an earlier question.",
                )
            versions = previous.version_ids
        else:
            if content is None or not content.strip():
                raise AppError(422, "invalid_message", "Enter a question.")
            question = content.strip()
            versions = [
                version.id for version in require_current_chat_versions(db, chat.version_ids)
            ]
            chat.version_ids = versions
            user = Message(chat_id=chat_id, role="user", content=question, version_ids=versions)
            db.add(user)
            db.flush()
            if chat.title == "New chat":
                chat.title = question[:80]
        require_chat_versions(db, versions)
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

    llm = None

    async def emit_response_delta(value: str):
        """Recheck active status, refresh its heartbeat, and emit a provisional text delta."""
        with session() as db, db.begin():
            current = db.get(Message, message_id)
            if not current or current.status != "streaming":
                raise asyncio.CancelledError
            current.updated_at = now()
        await emit_generation_event("answer.delta", {"text": value})

    try:
        with session() as db, db.begin():
            message = db.get(Message, message_id, with_for_update=True)
            if not message or message.status != "pending":
                return
            user = require_message(db, message.parent_id, message.chat_id)
            if user.role != "user":
                raise AppError(
                    409, "invalid_message_context", "The original question is unavailable."
                )
            message.status = "streaming"
            message.updated_at = now()
            history = [
                dict(
                    role=m.role,
                    content=m.content,
                    agent_trace=m.agent_trace,
                    message_id=m.id,
                    version_ids=m.version_ids,
                    citation_ids=[
                        citation["chunk_id"] for citation in m.citations if "chunk_id" in citation
                    ],
                )
                for m in load_visible_chat_messages(db, message.chat_id)
                if m.created_at < user.created_at and m.status == "complete"
            ]
            checkpoint = require_chat(db, message.chat_id).context_summary or {}
            question, version_ids = user.content, list(message.version_ids)
            is_retry = message.request_key is not None and message.request_key.startswith("retry:")
        llm = create_llm_client(message_id)

        settings = get_settings()
        runtime = DocumentToolRuntime(version_ids, message_id, llm)
        deadline = asyncio.get_running_loop().time() + settings.generation_timeout_seconds
        metadata = await asyncio.to_thread(runtime.metadata)
        full_history = history
        compacted = await asyncio.wait_for(
            compact_chat_history(
                full_history,
                llm,
                question=question,
                selected_documents=metadata,
                prior_summary=ConversationSummaryLLMResponse.model_validate(checkpoint["summary"])
                if checkpoint.get("summary")
                else None,
                compacted_message_ids=checkpoint.get("compacted_message_ids", []),
                fixed_context={
                    "system": CHAT_SYSTEM_PROMPT,
                    "tools": build_agent_tool_definitions(),
                    "tool_catalog": build_agent_tool_catalog_prompt(),
                    "answer_schema": AgentChatGenerationLLMResponse.model_json_schema(),
                },
            ),
            max(0, deadline - asyncio.get_running_loop().time()),
        )
        history = compacted.history
        if compacted.summary is not None:
            history = [
                build_conversation_summary_turn(
                    compacted.summary,
                    [
                        item
                        for item in full_history
                        if item["message_id"] in compacted.compacted_message_ids
                    ],
                ),
                *history,
            ]
            with session() as db, db.begin():
                # Match cancellation/admission lock order: chat before message.
                chat = require_chat(db, message.chat_id, lock=True)
                current = require_message(db, message_id, chat.id, lock=True)
                if current.status != "streaming":
                    raise asyncio.CancelledError
                require_chat_versions(db, version_ids)
                chat.context_summary = {
                    "summary": compacted.summary.model_dump(),
                    "compacted_message_ids": compacted.compacted_message_ids,
                }
        with session() as db:
            versions = require_chat_versions(db, version_ids)
            key = "answer:" + calculate_json_fingerprint(
                [
                    "document-agent-v1",
                    metadata,
                    [(v.id, v.sha256, v.embedding_model) for v in versions],
                    question,
                    history,
                    build_generation_identity(llm.generation_model("chat"), "chat"),
                    settings.openrouter_embedding_model,
                    RETRIEVAL_CONFIGURATION,
                ]
            )
        cached = None if is_retry else cache_get(key)
        if cached:
            answer = AgentChatGenerationLLMResponse.model_validate(cached["answer"])
            evidence = [Evidence.model_validate(item) for item in cached["evidence"]]
            rewritten = cached["query"]
            traces = cached["trace"]
            # Only retrieval-grounded document answers are stored; verify reuse at the boundary.
            validate_document_agent_answer(answer, evidence, [])
            count_metric("answer_cache_hits")
            # A cache hit returns its completed result; do not pretend to stream model tokens.
        else:

            async def persist_tool_trace(trace: dict) -> None:
                """Persist and stream compact tool progress, fenced against cancellation."""
                with session() as db, db.begin():
                    current = db.get(Message, message_id, with_for_update=True)
                    if current is None or current.status != "streaming":
                        raise asyncio.CancelledError
                    require_chat_versions(db, version_ids)
                    current.agent_trace = [
                        item
                        for item in current.agent_trace
                        if item.get("tool_call_id") != trace.get("tool_call_id")
                    ] + [trace]
                    current.updated_at = now()
                await emit_generation_event(
                    "tool.updated", {"message_id": message_id, "trace": trace}
                )
                notify_change()

            answer, evidence, rewritten, traces = await run_document_agent_workflow(
                question,
                history,
                runtime,
                llm,
                emit_response_delta,
                persist_tool_trace,
                max_tool_rounds=settings.agent_max_tool_rounds,
                timeout_seconds=max(0.001, deadline - asyncio.get_running_loop().time()),
            )
        selected = {item.id: item for item in evidence}
        with session() as db, db.begin():
            current = db.get(Message, message_id, with_for_update=True)
            if not current or current.status != "streaming":
                raise asyncio.CancelledError
            require_chat_versions(db, version_ids)
            current.content = answer.response
            current.suggestions = answer.suggestions
            current.citations = [
                build_public_citation(selected[identifier]) for identifier in answer.citation_ids
            ]
            current.outcome, current.rewritten_query = answer.outcome, rewritten
            if cached:
                # Cached references retain their original observation time and explicit provenance.
                current.agent_trace = [{**trace, "answer_cache_reused": True} for trace in traces]
            current.status, current.updated_at = "complete", now()
            result = serialize_message_response(current)
        if (
            not cached
            and answer.outcome == "answered"
            and answer.response_kind == "document_answer"
            and traces
            and all(
                trace["tool"] == "retrieve_relevant_chunks" and trace["status"] == "ok"
                for trace in traces
            )
        ):
            cache_set(
                key,
                {
                    "answer": answer.model_dump(),
                    "evidence": [e.model_dump() for e in evidence],
                    "query": rewritten,
                    "trace": traces,
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
        # A persisted cancellation discovered by a node is a normal terminal API result.
        # External task cancellation (disconnect/shutdown) must still propagate.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
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
        if llm is not None:
            await llm.close()


def encode_sse_event(name: str, data: dict) -> str:
    """Encode a named SSE event with a JSON payload using API-compatible value conversion."""
    return f"event: {name}\ndata: {json.dumps(jsonable_encoder(data), ensure_ascii=False)}\n\n"


async def stream_message_events(message_id: str, replay=False):
    """Yield admission, response, and terminal SSE events for an assistant attempt.

    Replay stored terminal results; cancel a newly started local generation on disconnect.
    """
    with session() as db:
        message = require_message(db, message_id)
        user = require_message(db, message.parent_id, message.chat_id)
        if message.role != "assistant" or user.role != "user":
            raise AppError(409, "invalid_message_context", "The original question is unavailable.")
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
