import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.asyncio import Redis as AsyncRedis

from docvault.api import chats, diagnostics, documents, metrics
from docvault.cache import count_metric
from docvault.config import get_settings
from docvault.errors import AppError
from docvault.health import SystemDiagnostics, read_system_diagnostics

logger = logging.getLogger("docvault")
logging.basicConfig(level=logging.INFO, format="%(message)s")


@asynccontextmanager
async def initialize_application_storage(app):
    """Create the configured storage directory before the application serves requests."""
    get_settings().storage_path.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="DocVault LLM",
    version="0.1.0",
    lifespan=initialize_application_storage,
    description="Local shared document workspace. IAM is deferred; use on localhost.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(documents.router)
app.include_router(chats.router)
app.include_router(metrics.router)
app.include_router(diagnostics.router)


@app.exception_handler(AppError)
async def handle_application_error(request, exc):
    """Render an application error with its request ID and a retry header for HTTP 429."""
    headers = {"Retry-After": "60"} if exc.status == 429 else None
    return JSONResponse(
        status_code=exc.status,
        headers=headers,
        content={
            "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(request, exc):
    """Return field-level validation errors without echoing submitted values."""
    # Do not echo inputs: they may contain document text or confidential questions.
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Check the request fields and try again.",
                "retryable": False,
                "details": [
                    {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                    for e in exc.errors()
                ],
            },
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    """Attach request IDs, record response-creation timing, and normalize unexpected errors."""
    request.state.request_id = str(uuid4())
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        logger.error('{"event":"request_failed","request_id":"%s"}', request.state.request_id)
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "The request could not be completed.",
                    "retryable": True,
                },
                "request_id": request.state.request_id,
            },
        )
    duration = (time.monotonic() - start) * 1000
    route_path = getattr(request.scope.get("route"), "path", None)
    route = route_path if isinstance(route_path, str) else "unmatched"
    if route.startswith("/v1/"):
        bucket = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        with contextlib.suppress(Exception):
            count_metric(f"http:{bucket}:{request.method}:{route}:{response.status_code}", duration)
    response.headers["X-Request-ID"] = request.state.request_id
    logger.info(
        '{"event":"request","request_id":"%s","route":"%s","status":%s,"duration_ms":%.2f}',
        request.state.request_id,
        route,
        response.status_code,
        duration,
    )
    return response


@app.get("/v1/config", tags=["configuration"])
def get_public_configuration():
    """Expose selected model names and whether a provider key is configured, never the key."""
    settings = get_settings()
    return dict(
        provider="openrouter",
        configured=bool(settings.openrouter_api_key.get_secret_value()),
        chat_model=settings.openrouter_chat_model,
        embedding_model=settings.openrouter_embedding_model,
        task_models={
            task: settings.generation_model(task).model
            for task in ("chat", "input_query_rewrite", "summary", "comparison")
        },
    )


@app.get("/health/live", tags=["health"])
def check_liveness():
    """Report that the API process can respond without checking its dependencies."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
def check_readiness():
    """Require usable storage, current schema/indexes, Redis and healthy consumers/dispatcher."""
    snapshot = read_system_diagnostics()
    return JSONResponse(
        snapshot.model_dump(mode="json"), status_code=200 if snapshot.status == "ready" else 503
    )


@app.get("/v1/diagnostics/system", response_model=SystemDiagnostics, tags=["diagnostics"])
def get_system_diagnostics() -> SystemDiagnostics:
    """Return a safe dependency snapshot even when the system is degraded."""
    return read_system_diagnostics()


@app.get("/metrics", response_class=PlainTextResponse, tags=["metrics"])
def export_prometheus_metrics():
    """Render available workspace metrics as newline-delimited Prometheus samples."""
    data = {
        **metrics.get_document_metrics(),
        **metrics.get_processing_metrics(),
        **metrics.get_llm_usage_metrics(),
    }
    return (
        "\n".join(f"docvault_{key} {value}" for key, value in data.items() if value is not None)
        + "\n"
    )


@app.websocket("/v1/events")
async def stream_workspace_events(websocket: WebSocket):
    """Reject disallowed origins, then emit Redis update hints and periodic snapshot revisions."""
    origin = websocket.headers.get("origin")
    if origin and origin not in get_settings().cors_origins:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client = AsyncRedis.from_url(
        get_settings().redis_url, socket_connect_timeout=2, socket_timeout=3
    )
    try:
        await websocket.send_json({"type": "snapshot", "revision": time.time_ns()})
        # Notifications accelerate updates; regular snapshots recover a lost Redis event.
        async with client.pubsub() as subscriber:
            with contextlib.suppress(Exception):
                await subscriber.subscribe("docvault:events")
            while True:
                try:
                    event = await subscriber.get_message(ignore_subscribe_messages=True, timeout=2)
                except Exception:
                    event = None
                    await asyncio.sleep(2)
                await websocket.send_json(
                    jsonable_encoder(
                        {"type": "update" if event else "snapshot", "revision": time.time_ns()}
                    )
                )
                await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await client.aclose()
