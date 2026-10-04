import time

from redis.exceptions import RedisError

from docvault.cache import redis_client
from docvault.errors import AppError


def enforce_request_rate_limit(operation: str, limit: int, window_seconds: int):
    """Apply atomic fixed-window admission in Redis, or disable it for a nonpositive limit.

    Reject excess requests with 429 and unavailable admission storage with 503.
    """
    if limit <= 0:
        return
    key = f"docvault:rate:{operation}:{int(time.time()) // window_seconds}"
    try:
        count = redis_client().eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n",
            1,
            key,
            window_seconds,
        )
    except RedisError as exc:
        raise AppError(
            503,
            "rate_service_unavailable",
            "Request admission is temporarily unavailable. Try again shortly.",
            True,
        ) from exc
    if count > limit:
        raise AppError(
            429, "rate_limit_exceeded", "Too many requests. Please wait before trying again.", True
        )
