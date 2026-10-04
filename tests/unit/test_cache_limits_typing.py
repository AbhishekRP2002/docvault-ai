from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from redis.exceptions import RedisError

from docvault import cache, limits, main
from docvault.errors import AppError


class RedisResponseFixture:
    """Supply synchronous Redis responses and outages without a Redis service."""

    def __init__(self, response: object = None, error: RedisError | None = None):
        """Store the response, optional failure, and captured Lua arguments."""
        self.response = response
        self.error = error
        self.eval_arguments: tuple = ()

    def get(self, key: str):
        """Return a stored response or raise the configured Redis failure."""
        if self.error is not None:
            raise self.error
        return self.response

    def ping(self):
        """Report Redis availability, raising the configured outage when present."""
        if self.error is not None:
            raise self.error
        return True

    def eval(self, *arguments):
        """Capture the atomic Lua request and supply its synchronous response."""
        self.eval_arguments = arguments
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (b'{"answer": "cached"}', {"answer": "cached"}),
        ('{"answer": "cached"}', {"answer": "cached"}),
        (None, None),
        (b"", None),
        (b"malformed", None),
        (object(), None),
    ],
)
def test_cache_reads_valid_json_and_tolerates_unusable_responses(monkeypatch, response, expected):
    """Treat missing, malformed, and non-serialized cache responses as cache misses."""
    client = RedisResponseFixture(response)
    monkeypatch.setattr(cache, "redis_client", lambda: client)
    assert cache.cache_get("answer") == expected


def test_cache_outage_remains_a_miss(monkeypatch):
    """Keep cache reads optional when the Redis service cannot be reached."""
    client = RedisResponseFixture(error=RedisError("unavailable"))
    monkeypatch.setattr(cache, "redis_client", lambda: client)
    assert cache.cache_get("answer") is None


@pytest.mark.parametrize(("count", "expected_status"), [(1, None), (3, None), (4, 429)])
def test_rate_limit_enforces_the_atomic_counter(monkeypatch, count, expected_status):
    """Admit counts through the configured limit and reject the next request."""
    client = RedisResponseFixture(count)
    monkeypatch.setattr(limits, "redis_client", lambda: client)
    monkeypatch.setattr(limits.time, "time", lambda: 120)
    if expected_status is None:
        limits.enforce_request_rate_limit("chat", 3, 60)
    else:
        with pytest.raises(AppError) as failure:
            limits.enforce_request_rate_limit("chat", 3, 60)
        assert failure.value.status == expected_status
        assert failure.value.code == "rate_limit_exceeded"
    assert client.eval_arguments[1:] == (1, "docvault:rate:chat:2", "60")


@pytest.mark.parametrize("response", [None, "1", True, 0, -1, object()])
def test_invalid_rate_counter_fails_closed(monkeypatch, response):
    """Reject invalid admission responses instead of admitting requests or leaking type errors."""
    client = RedisResponseFixture(response)
    monkeypatch.setattr(limits, "redis_client", lambda: client)
    with pytest.raises(AppError) as failure:
        limits.enforce_request_rate_limit("chat", 3, 60)
    assert failure.value.status == 503
    assert failure.value.code == "rate_service_unavailable"


def test_rate_limit_outage_fails_closed(monkeypatch):
    """Preserve unavailable-admission responses when Redis raises an operational error."""
    client = RedisResponseFixture(error=RedisError("unavailable"))
    monkeypatch.setattr(limits, "redis_client", lambda: client)
    with pytest.raises(AppError) as failure:
        limits.enforce_request_rate_limit("chat", 3, 60)
    assert failure.value.status == 503


def test_disabled_rate_limit_never_contacts_redis(monkeypatch):
    """Allow explicitly disabled admission without depending on the Redis service."""
    client = RedisResponseFixture(error=RedisError("unavailable"))
    monkeypatch.setattr(limits, "redis_client", lambda: client)
    limits.enforce_request_rate_limit("chat", 0, 60)
    assert client.eval_arguments == ()


@pytest.mark.parametrize(
    ("heartbeat", "expected_status"),
    [(b"950", 200), ("950", 200), (b"800", 503), (None, 503), (object(), 503), (b"bad", 503)],
)
def test_readiness_handles_missing_stale_and_invalid_heartbeats(
    monkeypatch, tmp_path, heartbeat, expected_status
):
    """Require a fresh serialized worker heartbeat while tolerating invalid Redis values."""
    client = RedisResponseFixture(heartbeat)
    connection = SimpleNamespace(execute=lambda statement: None)
    engine = SimpleNamespace(connect=lambda: nullcontext(connection))
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    monkeypatch.setattr(main, "redis_client", lambda: client)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(storage_path=tmp_path))
    monkeypatch.setattr(main.time, "time", lambda: 1_000)
    assert main.check_readiness().status_code == expected_status
