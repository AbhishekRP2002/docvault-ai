"""Consumer metrics distinguish no healthy workers from an unavailable monitoring dependency."""

from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError

from docvault import health
from docvault.api.metrics import _read_consumer_metrics


def test_consumer_counts_exclude_stale_workers_and_dispatchers(monkeypatch):
    monkeypatch.setattr(
        health,
        "read_worker_status",
        lambda: [
            SimpleNamespace(healthy=True, state="busy"),
            SimpleNamespace(healthy=True, state="idle"),
            SimpleNamespace(healthy=False, state="busy"),
        ],
    )
    monkeypatch.setattr(
        health,
        "read_dispatcher_status",
        lambda: [SimpleNamespace(healthy=True), SimpleNamespace(healthy=False)],
    )
    assert _read_consumer_metrics() == dict(
        healthy_workers=2,
        busy_workers=1,
        healthy_dispatchers=1,
        consumer_monitoring_error_code=None,
    )


def test_empty_reachable_consumer_registry_is_measured_zero(monkeypatch):
    monkeypatch.setattr(health, "read_worker_status", lambda: [])
    monkeypatch.setattr(health, "read_dispatcher_status", lambda: [])
    assert _read_consumer_metrics() == dict(
        healthy_workers=0,
        busy_workers=0,
        healthy_dispatchers=0,
        consumer_monitoring_error_code=None,
    )


@pytest.mark.parametrize("failing_reader", ["read_worker_status", "read_dispatcher_status"])
def test_redis_outage_never_masquerades_as_no_consumers(monkeypatch, failing_reader):
    def unavailable():
        raise ConnectionError("Internal connection details must not reach the response.")

    monkeypatch.setattr(health, "read_worker_status", lambda: [])
    monkeypatch.setattr(health, "read_dispatcher_status", lambda: [])
    monkeypatch.setattr(health, failing_reader, unavailable)
    assert _read_consumer_metrics() == dict(
        healthy_workers=None,
        busy_workers=None,
        healthy_dispatchers=None,
        consumer_monitoring_error_code="redis_unavailable",
    )
