import hashlib
import json
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from docvault.config import get_settings
from docvault.db import session
from docvault.models import MetricBucket


@lru_cache
def redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)


def signature(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def cache_get(key: str):
    try:
        value = redis_client().get(f"docvault:cache:{key}")
        return json.loads(value) if value else None
    except (RedisError, ValueError):
        return None


def cache_set(key: str, value, ttl: int = 900):
    try:
        redis_client().setex(f"docvault:cache:{key}", ttl, json.dumps(value))
    except RedisError:
        pass  # Cache availability is not a persistence requirement.


def count_metric(key: str, duration_ms: float = 0):
    with session() as db, db.begin():
        stmt = insert(MetricBucket).values(
            key=key, count=1, total_ms=duration_ms, max_ms=duration_ms
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[MetricBucket.key],
                set_={
                    "count": MetricBucket.count + 1,
                    "total_ms": MetricBucket.total_ms + duration_ms,
                "max_ms": func.greatest(MetricBucket.max_ms, duration_ms),
                },
            )
        )


def notify_change():
    try:
        client = redis_client()
        revision = client.incr("docvault:revision")
        client.publish("docvault:events", json.dumps({"type": "update", "revision": revision}))
    except RedisError:
        pass  # WebSocket snapshots also reconcile from the database.
