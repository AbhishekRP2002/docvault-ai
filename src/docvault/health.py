"""Bounded dependency probes and safe, read-only system diagnostics."""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, Field
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from rq import Queue, Worker, worker_registration
from rq.registry import FailedJobRegistry
from sqlalchemy import text

from docvault.config import get_settings
from docvault.db import get_health_engine

DISPATCHER_PREFIX = "docvault:dispatcher:"


class DiagnosticWorker(Worker):
    """Read RQ metadata without applying blocking-consumer socket timeouts to probes."""

    def _set_connection(self, connection: Redis) -> Redis:
        """Keep probe deadlines intact; this metadata instance never consumes jobs."""
        return connection


class DependencyCheck(BaseModel):
    """One dependency's readiness result without raw exceptions or connection URLs."""

    ready: bool
    error_code: str | None = None
    latency_ms: float = 0
    details: dict[str, Any] = Field(default_factory=dict)


class WorkerStatus(BaseModel):
    """Native RQ consumer identity, state and last observed heartbeat."""

    id: str
    state: str = "unknown"
    healthy: bool = False
    last_heartbeat: datetime | None = None
    heartbeat_age_seconds: float | None = None
    current_job_id: str | None = None
    pid: int | None = None
    hostname: str | None = None


class DispatcherStatus(BaseModel):
    """Last completed dispatcher cycle and dependency failure for one instance."""

    id: str
    healthy: bool = False
    last_cycle_at: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str | None = None


class ProviderStatus(BaseModel):
    """Configuration and recorded activity; no live or paid provider verification."""

    configured: bool
    verification: Literal["unverified"] = "unverified"
    recent_failure_count: int | None = None
    observation_window_seconds: int = 3600


class SystemDiagnostics(BaseModel):
    """A best-effort snapshot, including degraded dependencies and unknown observations."""

    status: Literal["ready", "degraded"]
    checked_at: datetime
    checks: dict[str, bool]
    details: dict[str, DependencyCheck]
    workers: list[WorkerStatus]
    dispatchers: list[DispatcherStatus]
    healthy_worker_count: int
    busy_worker_count: int
    queue: dict[str, int | float | None]
    provider: ProviderStatus
    parser: dict[str, int | str]


@lru_cache
def expected_schema_heads() -> set[str]:
    """Resolve repository migration heads instead of treating any version table as current."""
    root = Path(__file__).resolve().parents[2]
    configuration = Config(str(root / "alembic.ini"))
    configuration.set_main_option("script_location", str(root / "migrations"))
    return set(ScriptDirectory.from_config(configuration).get_heads())


@lru_cache
def health_redis_client() -> Redis:
    """Avoid command retries and bound connection/socket waits for diagnostic probes."""
    return Redis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )


def heartbeat_age(timestamp: datetime | None, *, current: datetime | None = None) -> float | None:
    """Reject missing, naive, future and stale timestamps rather than reporting freshness."""
    if timestamp is None or timestamp.tzinfo is None:
        return None
    age = ((current or datetime.now(UTC)) - timestamp).total_seconds()
    return age if age >= 0 else None


def read_worker_status(client: Redis | None = None) -> list[WorkerStatus]:
    """Read native per-consumer RQ registrations; malformed records remain unhealthy."""
    client = client or health_redis_client()
    workers = []
    keys = worker_registration.get_keys(queue=Queue("docvault", connection=client))
    for key in sorted(keys):
        try:
            worker = DiagnosticWorker.find_by_key(key, connection=client)
            if worker is None:
                continue
            age = heartbeat_age(worker.last_heartbeat)
            state = worker.get_state()
            workers.append(
                WorkerStatus(
                    id=worker.name,
                    state=state,
                    healthy=age is not None
                    and age < get_settings().health_heartbeat_max_age_seconds
                    and state in {"idle", "busy"},
                    last_heartbeat=worker.last_heartbeat,
                    heartbeat_age_seconds=age,
                    current_job_id=worker.get_current_job_id(),
                    pid=worker.pid,
                    hostname=worker.hostname,
                )
            )
        except (ValueError, TypeError, OverflowError):
            workers.append(WorkerStatus(id=key.removeprefix("rq:worker:")))
    return workers


def read_dispatcher_status(client: Redis | None = None) -> list[DispatcherStatus]:
    """Read expiring dispatcher records; recent successful cycles are required for health."""
    client = client or health_redis_client()
    statuses = []
    for key in client.scan_iter(match=f"{DISPATCHER_PREFIX}*"):
        raw = client.get(key)
        if not isinstance(raw, (str, bytes, bytearray)):
            continue
        identifier = key.decode() if isinstance(key, bytes) else str(key)
        identifier = identifier.removeprefix(DISPATCHER_PREFIX)
        try:
            value = json.loads(raw)
            cycle = datetime.fromisoformat(value["last_cycle_at"])
            success = (
                datetime.fromisoformat(value["last_success_at"])
                if value.get("last_success_at")
                else None
            )
            age = heartbeat_age(cycle)
            success_age = heartbeat_age(success)
            error = value.get("error_code")
            statuses.append(
                DispatcherStatus(
                    id=identifier,
                    last_cycle_at=cycle,
                    last_success_at=success,
                    error_code=error,
                    healthy=error is None
                    and age is not None
                    and success_age is not None
                    and max(age, success_age) < get_settings().health_heartbeat_max_age_seconds,
                )
            )
        except (ValueError, TypeError, KeyError, OverflowError):
            statuses.append(DispatcherStatus(id=identifier, error_code="invalid_dispatcher_record"))
    return statuses


def publish_dispatcher_status(
    identifier: str, success_at: datetime | None, error_code: str | None
) -> None:
    """Publish a completed cycle from the dispatcher loop, never from an independent ticker."""
    health_redis_client().setex(
        f"{DISPATCHER_PREFIX}{identifier}",
        get_settings().health_heartbeat_max_age_seconds,
        json.dumps(
            dict(
                last_cycle_at=datetime.now(UTC).isoformat(),
                last_success_at=success_at.isoformat() if success_at else None,
                error_code=error_code,
            )
        ),
    )


def remove_dispatcher_status(identifier: str) -> None:
    """Remove only this dispatcher's observation when its consumer loop shuts down cleanly."""
    health_redis_client().delete(f"{DISPATCHER_PREFIX}{identifier}")


def probe_storage() -> DependencyCheck:
    """Verify source storage read/write/delete access and report actual free capacity."""
    start = time.monotonic()
    path = get_settings().storage_path
    try:
        capacity = shutil.disk_usage(path)
        with tempfile.TemporaryDirectory(prefix=".health-", dir=path) as temporary:
            target = Path(temporary) / "probe"
            target.write_bytes(b"docvault-health")
            if target.read_bytes() != b"docvault-health":
                raise OSError("Storage readback failed")
            target.unlink()
        ready = capacity.free >= get_settings().storage_min_free_bytes
        return DependencyCheck(
            ready=ready,
            error_code=None if ready else "storage_capacity_low",
            latency_ms=(time.monotonic() - start) * 1000,
            details=dict(free_bytes=capacity.free, total_bytes=capacity.total),
        )
    except OSError:
        return DependencyCheck(
            ready=False,
            error_code="storage_access_failed",
            latency_ms=(time.monotonic() - start) * 1000,
        )


def probe_database() -> dict[str, DependencyCheck]:
    """Check connectivity, exact migration heads and valid hybrid-retrieval indexes."""
    start = time.monotonic()
    failed = {
        name: DependencyCheck(ready=False, error_code="database_unavailable")
        for name in ("database", "schema", "vector_index")
    }
    try:
        with get_health_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
            failed["database"] = DependencyCheck(ready=True)
            try:
                actual = set(
                    connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
                )
                expected = expected_schema_heads()
                failed["schema"] = DependencyCheck(
                    ready=actual == expected,
                    error_code=None if actual == expected else "schema_outdated",
                    details=dict(
                        expected_revisions=sorted(expected), actual_revisions=sorted(actual)
                    ),
                )
            except Exception:
                failed["schema"] = DependencyCheck(ready=False, error_code="schema_check_failed")
                connection.rollback()
            try:
                extension = bool(
                    connection.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')")
                    )
                )
                indexes = dict(
                    connection.execute(
                        text("""
                    SELECT c.relname, i.indisvalid AND i.indisready
                        AND ((c.relname IN ('ix_chunks_embedding_hnsw', 'ix_document_overviews_embedding_hnsw') AND a.amname='hnsw')
                        OR (c.relname IN ('ix_chunks_search', 'ix_document_overviews_search') AND a.amname='gin'))
                    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                    JOIN pg_class t ON t.oid=i.indrelid
                    JOIN pg_namespace n ON n.oid=t.relnamespace
                    JOIN pg_am a ON a.oid=c.relam
                    WHERE t.relname IN ('chunks', 'document_overviews') AND n.nspname=current_schema()
                    AND c.relname IN ('ix_chunks_embedding_hnsw', 'ix_chunks_search',
                                     'ix_document_overviews_embedding_hnsw', 'ix_document_overviews_search')
                """)
                    ).all()
                )
                valid = extension and all(
                    indexes.get(name, False)
                    for name in (
                        "ix_chunks_embedding_hnsw",
                        "ix_chunks_search",
                        "ix_document_overviews_embedding_hnsw",
                        "ix_document_overviews_search",
                    )
                )
                failed["vector_index"] = DependencyCheck(
                    ready=valid,
                    error_code=None if valid else "retrieval_index_unavailable",
                    details=dict(vector_extension=extension, indexes=indexes),
                )
            except Exception:
                failed["vector_index"] = DependencyCheck(
                    ready=False, error_code="retrieval_index_check_failed"
                )
    except Exception:
        pass
    elapsed = (time.monotonic() - start) * 1000
    for check in failed.values():
        check.latency_ms = elapsed
    return failed


def read_system_diagnostics() -> SystemDiagnostics:
    """Return safe degraded results; never make provider calls or initialize parser models."""
    from docvault.parsing import CHUNK_TOKENS

    details = probe_database()
    details["storage"] = probe_storage()
    workers: list[WorkerStatus] = []
    dispatchers: list[DispatcherStatus] = []
    start = time.monotonic()
    try:
        client = health_redis_client()
        details["redis"] = DependencyCheck(ready=bool(client.ping()))
        details["redis"].latency_ms = (time.monotonic() - start) * 1000
        start = time.monotonic()
        try:
            workers = read_worker_status(client)
            ready = any(item.healthy for item in workers)
            details["worker"] = DependencyCheck(
                ready=ready, error_code=None if ready else "worker_unavailable"
            )
        except RedisError:
            details["worker"] = DependencyCheck(
                ready=False, error_code="worker_observation_unavailable"
            )
        details["worker"].latency_ms = (time.monotonic() - start) * 1000
        start = time.monotonic()
        try:
            dispatchers = read_dispatcher_status(client)
            ready = any(item.healthy for item in dispatchers)
            details["dispatcher"] = DependencyCheck(
                ready=ready, error_code=None if ready else "dispatcher_unavailable"
            )
        except RedisError:
            details["dispatcher"] = DependencyCheck(
                ready=False, error_code="dispatcher_observation_unavailable"
            )
        details["dispatcher"].latency_ms = (time.monotonic() - start) * 1000
    except RedisError:
        for name in ("redis", "worker", "dispatcher"):
            details[name] = DependencyCheck(ready=False, error_code="redis_unavailable")
    queue: dict[str, int | float | None] = dict(
        due_jobs=None,
        retry_waiting=None,
        dead_letter_count=None,
        expired_leases=None,
        oldest_due_job_age_seconds=None,
    )
    provider = ProviderStatus(configured=bool(get_settings().openrouter_api_key.get_secret_value()))
    if details["schema"].ready:
        try:
            with get_health_engine().connect() as connection:
                row = connection.execute(
                    text("""
                    SELECT count(*) FILTER (WHERE status='queued' AND next_at <= now()),
                        count(*) FILTER (WHERE status='queued' AND attempts > 0 AND next_at > now()),
                        count(*) FILTER (WHERE status='failed'),
                        count(*) FILTER (WHERE status IN ('running','enqueued') AND lease_until <= now()),
                        EXTRACT(EPOCH FROM now()-min(next_at) FILTER (WHERE status IN ('queued','enqueued') AND next_at <= now()))
                    FROM jobs
                """)
                ).one()
                queue = dict(
                    zip(
                        queue,
                        (
                            int(row[0]),
                            int(row[1]),
                            int(row[2]),
                            int(row[3]),
                            float(row[4]) if row[4] is not None else None,
                        ),
                        strict=True,
                    )
                )
                provider.recent_failure_count = int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM llm_calls WHERE status='failed' AND created_at >= now()-interval '1 hour'"
                        )
                    )
                    or 0
                )
        except Exception:
            pass
    queue["rq_failed_deliveries"] = None
    if details["redis"].ready:
        try:
            queue["rq_failed_deliveries"] = FailedJobRegistry(
                "docvault", connection=health_redis_client()
            ).get_job_count(cleanup=False)
        except RedisError:
            pass  # Transport observations remain unknown; SQL owns replay/retry decisions.
    settings = get_settings()
    checks = {name: value.ready for name, value in details.items()}
    return SystemDiagnostics(
        status="ready" if all(checks.values()) else "degraded",
        checked_at=datetime.now(UTC),
        checks=checks,
        details=details,
        workers=workers,
        dispatchers=dispatchers,
        healthy_worker_count=sum(worker.healthy for worker in workers),
        busy_worker_count=sum(worker.healthy and worker.state == "busy" for worker in workers),
        queue=queue,
        provider=provider,
        parser=dict(
            chunk_tokens=CHUNK_TOKENS,
            inference_threads=settings.docling_num_threads,
            decoder_threads=settings.docling_parser_threads or settings.docling_num_threads,
            layout_batch_size=settings.docling_layout_batch_size,
            ocr_batch_size=settings.docling_ocr_batch_size,
            table_batch_size=settings.docling_table_batch_size,
        ),
    )


def identity_file(role: str) -> Path:
    """Locate this container's process identity; replicas have isolated temporary directories."""
    return Path(tempfile.gettempdir()) / f"docvault-{role}-{socket.gethostname()}.json"


def save_process_identity(role: str, identifier: str) -> None:
    """Record the exact consumer/dispatcher ID used by own-instance container probes."""
    identity_file(role).write_text(json.dumps(dict(id=identifier, pid=os.getpid())))


def remove_process_identity(role: str, identifier: str) -> None:
    """Remove only this process's record, preserving a newer process's identity."""
    path = identity_file(role)
    try:
        if json.loads(path.read_text()).get("id") == identifier:
            path.unlink()
    except (OSError, ValueError):
        pass


def check_own_process(role: str) -> bool:
    """Require the recorded local PID and exact Redis identity to be healthy."""
    try:
        record = json.loads(identity_file(role).read_text())
        os.kill(int(record["pid"]), 0)
        reader = read_worker_status if role == "worker" else read_dispatcher_status
        return any(item.id == record["id"] and item.healthy for item in reader())
    except (OSError, ValueError, TypeError, KeyError, RedisError):
        return False


def main() -> None:
    """Exit nonzero for an unhealthy own worker/dispatcher, without exposing secrets."""
    if len(sys.argv) != 2 or sys.argv[1] not in {"worker", "dispatcher"}:
        raise SystemExit("Usage: python -m docvault.health worker|dispatcher")
    healthy = check_own_process(sys.argv[1])
    print("healthy" if healthy else "unhealthy")
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
