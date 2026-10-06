"""Dependency failure and freshness checks without external services."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError

from docvault import db, dispatcher, health
from docvault.config import Settings


@pytest.mark.parametrize("offset,healthy", [(0, True), (-119, True), (-121, False), (1, False)])
@pytest.mark.parametrize("state", ["idle", "busy", "suspended"])
def test_native_worker_heartbeat_and_consumer_state(monkeypatch, offset, healthy, state):
    """Busy consumers remain healthy; stale/future timestamps and suspension do not."""
    worker = SimpleNamespace(
        name="consumer-one",
        last_heartbeat=datetime.now(UTC) + timedelta(seconds=offset),
        get_state=lambda: state,
        get_current_job_id=lambda: "delivery-id",
        pid=123,
        hostname="host",
    )
    monkeypatch.setattr(
        health.worker_registration, "get_keys", lambda **kwargs: {"rq:worker:consumer-one"}
    )
    monkeypatch.setattr(health.Worker, "find_by_key", lambda *args, **kwargs: worker)
    result = health.read_worker_status(cast(Redis, SimpleNamespace()))[0]
    assert result.healthy is (healthy and state in {"idle", "busy"})
    assert result.id == "consumer-one"
    assert result.current_job_id == "delivery-id"


def test_malformed_worker_does_not_become_redis_outage(monkeypatch):
    """A damaged RQ record is unhealthy while unrelated Redis availability remains independent."""
    monkeypatch.setattr(health.worker_registration, "get_keys", lambda **kwargs: {"rq:worker:bad"})

    def fail(*args, **kwargs):
        raise ValueError("invalid RQ timestamp")

    monkeypatch.setattr(health.Worker, "find_by_key", fail)
    assert not health.read_worker_status(cast(Redis, SimpleNamespace()))[0].healthy


@pytest.mark.parametrize(
    "timestamp",
    [None, datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=10)],
)
def test_unusable_timestamp_never_counts_as_fresh(timestamp):
    """Missing, timezone-free and future timestamps cannot establish consumer health."""
    assert health.heartbeat_age(timestamp, current=datetime(2026, 1, 1, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    "offset,error,healthy",
    [(0, None, True), (-121, None, False), (10, None, False), (0, "redis_unavailable", False)],
)
def test_dispatcher_health_requires_successful_actual_cycle(offset, error, healthy):
    """Freshness alone cannot make a failed publication cycle healthy."""
    timestamp = (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()
    value = json.dumps(dict(last_cycle_at=timestamp, last_success_at=timestamp, error_code=error))
    client = SimpleNamespace(
        scan_iter=lambda **kwargs: [b"docvault:dispatcher:one"], get=lambda key: value
    )
    result = health.read_dispatcher_status(cast(Redis, client))[0]
    assert result.healthy is healthy
    assert result.id == "one"


@pytest.mark.parametrize("value", ['{"last_cycle_at": "inf"}', "null", "{}", "not-json"])
def test_invalid_dispatcher_record_is_unhealthy(value):
    """Malformed and nonfinite values cannot keep the dispatcher ready."""
    client = SimpleNamespace(
        scan_iter=lambda **kwargs: [b"docvault:dispatcher:one"], get=lambda key: value
    )
    assert not health.read_dispatcher_status(cast(Redis, client))[0].healthy


def test_storage_readback_cleanup_and_capacity(monkeypatch, tmp_path):
    """A health probe leaves no artifacts and respects the configured capacity floor."""
    settings = Settings(_env_file=None, storage_path=tmp_path)
    monkeypatch.setattr(health, "get_settings", lambda: settings)
    assert health.probe_storage().ready
    assert list(tmp_path.iterdir()) == []
    settings.storage_min_free_bytes = 2**63
    assert health.probe_storage().error_code == "storage_capacity_low"
    settings.storage_path = tmp_path / "missing"
    assert health.probe_storage().error_code == "storage_access_failed"


@pytest.mark.parametrize(
    "revision,index_valid,overview_valid,extension,ready",
    [
        ("head", True, True, True, True),
        ("old", True, True, True, False),
        ("head", False, True, True, False),
        ("head", True, True, False, False),
        ("head", True, False, True, False),
    ],
)
def test_database_requires_current_revision_and_valid_indexes(
    monkeypatch, revision, index_valid, overview_valid, extension, ready
):
    """Connectivity is distinct from migration and hybrid-index readiness."""

    def execute(statement):
        sql = str(statement)
        if "version_num" in sql:
            return SimpleNamespace(scalars=lambda: [revision])
        return SimpleNamespace(
            all=lambda: [
                ("ix_chunks_embedding_hnsw", index_valid),
                ("ix_chunks_search", True),
                ("ix_document_overviews_embedding_hnsw", overview_valid),
                ("ix_document_overviews_search", True),
            ]
        )

    connection = SimpleNamespace(execute=execute, scalar=lambda statement: extension)
    monkeypatch.setattr(
        health,
        "get_health_engine",
        lambda: SimpleNamespace(connect=lambda: nullcontext(connection)),
    )
    monkeypatch.setattr(health, "expected_schema_heads", lambda: {"head"})
    checks = health.probe_database()
    assert checks["database"].ready
    assert all(item.ready for item in checks.values()) is ready
    assert checks["schema"].ready is (revision == "head")
    assert checks["vector_index"].ready is (index_valid and overview_valid and extension)


def test_database_outage_is_safe_and_explicit(monkeypatch):
    """Never expose raw connection error strings containing credentials."""

    def fail():
        raise OperationalError("secret-url", {}, RuntimeError("password"))

    monkeypatch.setattr(health, "get_health_engine", lambda: SimpleNamespace(connect=fail))
    checks = health.probe_database()
    assert not checks["database"].ready
    assert "password" not in str(checks)


def test_pool_and_probe_have_independent_bounded_configuration(monkeypatch):
    """The probe bypasses a saturated application pool and uses shorter database waits."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://localhost/test?options=-csearch_path%3Disolated%2Cpublic",
    )
    calls = []
    listeners = {}
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    monkeypatch.setattr(
        db, "create_engine", lambda url, **kwargs: calls.append(kwargs) or SimpleNamespace()
    )

    def listen(engine, event):
        def decorate(function):
            listeners[event] = function
            return function

        return decorate

    monkeypatch.setattr(db.event, "listens_for", listen)
    db._create_database_engine()
    db._create_database_engine(health_probe=True)
    assert "-csearch_path=isolated,public" in calls[0]["connect_args"]["options"]
    assert "-csearch_path=isolated,public" in calls[1]["connect_args"]["options"]
    assert calls[0]["pool_timeout"] == 5
    assert calls[0]["max_overflow"] == 5
    assert calls[0]["connect_args"]["connect_timeout"] == 3
    assert "statement_timeout=30000" in calls[0]["connect_args"]["options"]
    assert calls[1]["poolclass"] is db.NullPool
    assert calls[1]["connect_args"]["connect_timeout"] == 2
    assert "statement_timeout=1000" in calls[1]["connect_args"]["options"]
    record = SimpleNamespace(info={"pid": -1}, dbapi_connection=object())
    proxy = SimpleNamespace(dbapi_connection=object())
    with pytest.raises(db.exc.DisconnectionError):
        listeners["checkout"](None, record, proxy)
    assert record.dbapi_connection is None and proxy.dbapi_connection is None


@pytest.mark.parametrize(
    "exception,error",
    [
        (None, None),
        (RedisError("private"), "redis_unavailable"),
        (OperationalError("private", {}, RuntimeError()), "database_unavailable"),
    ],
)
def test_dispatcher_publishes_cycle_failure_and_retains_last_success(monkeypatch, exception, error):
    """A partial publication failure must not advance the last successful cycle."""
    previous = datetime.now(UTC) - timedelta(seconds=20)
    calls = []

    def dispatch(**kwargs):
        assert kwargs == {"raise_publish_errors": True}
        if exception:
            raise exception

    monkeypatch.setattr(dispatcher, "dispatch_pending_jobs", dispatch)
    monkeypatch.setattr(dispatcher, "publish_dispatcher_status", lambda *args: calls.append(args))
    result = dispatcher.run_dispatcher_cycle("this-instance", previous)
    assert calls[0][0] == "this-instance" and calls[0][2] == error
    assert result is not None
    assert result == previous if exception else result > previous


def test_own_instance_probe_rejects_other_healthy_process(monkeypatch, tmp_path):
    """A replica's healthy record cannot satisfy this container's own health probe."""
    monkeypatch.setattr(health, "identity_file", lambda role: tmp_path / "identity")
    health.save_process_identity("worker", "own")
    monkeypatch.setattr(
        health, "read_worker_status", lambda: [health.WorkerStatus(id="other", healthy=True)]
    )
    assert not health.check_own_process("worker")
    monkeypatch.setattr(
        health, "read_worker_status", lambda: [health.WorkerStatus(id="own", healthy=True)]
    )
    assert health.check_own_process("worker")
    health.remove_process_identity("worker", "other")
    assert (tmp_path / "identity").exists()
    health.remove_process_identity("worker", "own")
    assert not (tmp_path / "identity").exists()


def test_redis_outage_preserves_independent_database_and_storage(monkeypatch, tmp_path):
    """System snapshots remain available and distinguish unknown consumer observations."""
    monkeypatch.setattr(
        health,
        "probe_database",
        lambda: {
            name: health.DependencyCheck(ready=name == "database")
            for name in ("database", "schema", "vector_index")
        },
    )
    monkeypatch.setattr(health, "probe_storage", lambda: health.DependencyCheck(ready=True))

    def fail():
        raise RedisError("private")

    monkeypatch.setattr(health, "health_redis_client", fail)
    snapshot = health.read_system_diagnostics()
    assert snapshot.status == "degraded"
    assert snapshot.checks["database"] and snapshot.checks["storage"]
    assert not snapshot.checks["redis"] and not snapshot.checks["worker"]
    assert snapshot.provider.verification == "unverified"
    assert snapshot.provider.recent_failure_count is None
    assert snapshot.queue["due_jobs"] is None


def test_rq_metadata_lookup_keeps_bounded_redis_probe_timeout(monkeypatch):
    """RQ metadata construction must not silently extend a diagnostic socket wait to minutes."""
    client = Redis(socket_timeout=1, socket_connect_timeout=1)
    monkeypatch.setattr(client, "exists", lambda key: True)
    monkeypatch.setattr(
        client,
        "hgetall",
        lambda key: {
            b"state": b"idle",
            b"queues": b"docvault",
            b"last_heartbeat": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode(),
        },
    )
    worker = health.DiagnosticWorker.find_by_key("rq:worker:consumer", connection=client)
    assert worker is not None and worker.get_state() == "idle"
    assert client.connection_pool.connection_kwargs["socket_timeout"] == 1
    client.close()


def test_prometheus_exports_numeric_samples_and_only_known_stage_labels(monkeypatch):
    """Rich JSON diagnostics must not produce invalid or user-controlled Prometheus samples."""
    from docvault import main

    monkeypatch.setattr(main.metrics, "get_document_metrics", lambda: {"documents": 3})
    monkeypatch.setattr(main.metrics, "get_llm_usage_metrics", lambda: {"input_tokens": 10})
    monkeypatch.setattr(
        main.metrics,
        "get_processing_metrics",
        lambda: {
            "healthy_workers": None,
            "expired_leases": 2,
            "window_start": "date",
            "consumer_monitoring_error_code": "redis_unavailable",
            "stage_durations": [
                {"stage": "conversion", "sample_count": 4, "p50_ms": 12.0, "p95_ms": 20.0},
                {"stage": 'private-user-text"', "sample_count": 9, "p50_ms": 5.0, "p95_ms": 8.0},
            ],
        },
    )
    output = main.export_prometheus_metrics()
    assert "docvault_documents 3\n" in output
    assert "docvault_expired_leases 2\n" in output
    assert 'docvault_stage_sample_count{stage="conversion"} 4' in output
    assert "private-user-text" not in output and "window_start" not in output
    assert "healthy_workers" not in output and "stage_durations" not in output


def test_index_probe_failure_does_not_mark_valid_schema_outdated(monkeypatch):
    """Independent readiness results preserve the already verified schema revision."""

    def execute(statement):
        if "version_num" in str(statement):
            return SimpleNamespace(scalars=lambda: ["head"])
        return None

    def fail(statement):
        raise OperationalError("private", {}, RuntimeError())

    connection = SimpleNamespace(execute=execute, scalar=fail)
    monkeypatch.setattr(
        health,
        "get_health_engine",
        lambda: SimpleNamespace(connect=lambda: nullcontext(connection)),
    )
    monkeypatch.setattr(health, "expected_schema_heads", lambda: {"head"})
    checks = health.probe_database()
    assert checks["database"].ready and checks["schema"].ready
    assert not checks["vector_index"].ready


def test_dispatcher_sigterm_removes_own_observation_and_identity(monkeypatch):
    """Clean SIGTERM cannot leave a fresh dispatcher record masking its stopped loop."""
    signals = []
    saved = []
    removed = []
    monkeypatch.setattr(
        dispatcher.signal,
        "signal",
        lambda signum, handler: signals.append((signum, handler)) or "previous",
    )
    monkeypatch.setattr(
        dispatcher, "save_process_identity", lambda role, identifier: saved.append(identifier)
    )
    monkeypatch.setattr(
        dispatcher,
        "remove_process_identity",
        lambda role, identifier: removed.append((role, identifier)),
    )
    monkeypatch.setattr(
        dispatcher,
        "remove_dispatcher_status",
        lambda identifier: removed.append(("observation", identifier)),
    )
    monkeypatch.setattr(
        dispatcher, "run_dispatcher_cycle", lambda identifier, previous: datetime.now(UTC)
    )

    def terminate(seconds):
        signals[0][1](dispatcher.signal.SIGTERM, None)

    monkeypatch.setattr(dispatcher.time, "sleep", terminate)
    dispatcher.main()
    assert signals[0][1] is dispatcher.request_dispatcher_stop
    assert signals[-1][1] == "previous"
    assert removed == [("dispatcher", saved[0]), ("observation", saved[0])]


@pytest.mark.parametrize("window,ttl,monitoring", [(30, 30, 10), (120, 90, 30), (300, 90, 30)])
def test_worker_heartbeat_intervals_fit_configured_freshness(monkeypatch, window, ttl, monitoring):
    """Both idle dequeue and busy-work monitoring fit the selected health window."""
    from docvault import worker

    calls = []

    def construct(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(work=lambda: None)

    monkeypatch.setattr(worker, "Worker", construct)
    monkeypatch.setattr(worker, "SpawnWorker", construct)
    monkeypatch.setattr(
        worker, "get_settings", lambda: SimpleNamespace(health_heartbeat_max_age_seconds=window)
    )
    monkeypatch.setattr(worker, "redis_client", lambda: None)
    monkeypatch.setattr(worker, "save_process_identity", lambda *args: None)
    monkeypatch.setattr(worker, "remove_process_identity", lambda *args: None)
    worker.main()
    assert calls[0]["worker_ttl"] == ttl
    assert calls[0]["job_monitoring_interval"] == monitoring


@pytest.mark.parametrize("count", [3, None])
def test_system_snapshot_keeps_rq_transport_failures_distinct_from_sql_dead_letters(
    monkeypatch, count
):
    """Native failed-registry inspection is read-only and is not another retry authority."""
    monkeypatch.setattr(
        health,
        "probe_database",
        lambda: {
            name: health.DependencyCheck(ready=name == "database")
            for name in ("database", "schema", "vector_index")
        },
    )
    monkeypatch.setattr(health, "probe_storage", lambda: health.DependencyCheck(ready=True))
    monkeypatch.setattr(health, "health_redis_client", lambda: SimpleNamespace(ping=lambda: True))
    monkeypatch.setattr(health, "read_worker_status", lambda client: [])
    monkeypatch.setattr(health, "read_dispatcher_status", lambda client: [])

    def read_count(*, cleanup):
        assert cleanup is False
        if count is None:
            raise RedisError("private")
        return count

    monkeypatch.setattr(
        health,
        "FailedJobRegistry",
        lambda *args, **kwargs: SimpleNamespace(get_job_count=read_count),
    )
    snapshot = health.read_system_diagnostics()
    assert snapshot.queue["rq_failed_deliveries"] == count
    assert snapshot.queue["dead_letter_count"] is None
    assert snapshot.checks["redis"]
