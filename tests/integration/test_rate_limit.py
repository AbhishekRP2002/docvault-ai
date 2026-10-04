"""Verify Redis Lua admission against a uniquely named, disposable test counter."""

import os
from uuid import uuid4

import pytest
from redis import Redis

from docvault import limits
from docvault.errors import AppError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1", reason="Enable local integration tests."
    ),
]


def test_real_redis_rate_counter_enforces_boundary_and_expires(monkeypatch):
    client = Redis.from_url("redis://127.0.0.1:16379/14", decode_responses=True)
    operation = f"typing_test_{uuid4().hex}"
    timestamp, window = 1_000_000, 60
    key = f"docvault:rate:{operation}:{timestamp // window}"
    monkeypatch.setattr(limits, "redis_client", lambda: client)
    monkeypatch.setattr(limits.time, "time", lambda: timestamp)
    try:
        limits.enforce_request_rate_limit(operation, 2, window)
        limits.enforce_request_rate_limit(operation, 2, window)
        with pytest.raises(AppError) as failure:
            limits.enforce_request_rate_limit(operation, 2, window)
        assert (failure.value.status, failure.value.code) == (429, "rate_limit_exceeded")
        assert client.get(key) == "3"
        ttl = client.ttl(key)
        assert isinstance(ttl, int) and 0 < ttl <= window
    finally:
        client.delete(key)
        client.close()
