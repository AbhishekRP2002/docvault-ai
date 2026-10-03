"""Run one queue consumer: python -m docvault.worker."""

import sys
import threading
import time

from redis.exceptions import RedisError
from rq import Worker
from rq.worker import SpawnWorker

from docvault.cache import redis_client


def _heartbeat(stop: threading.Event) -> None:
    """Refresh the worker's expiring Redis heartbeat until stopped, tolerating Redis outages."""
    while not stop.is_set():
        try:
            redis_client().setex("docvault:worker:heartbeat", 30, str(time.time()))
        except RedisError:
            pass
        stop.wait(10)


def main() -> None:
    """Run the document queue consumer with a background heartbeat and graceful thread cleanup."""
    worker_type = SpawnWorker if sys.platform == "darwin" else Worker
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
    heartbeat.start()
    try:
        worker_type(["docvault"], connection=redis_client()).work()
    finally:
        stop.set()
        heartbeat.join(timeout=5)


if __name__ == "__main__":
    main()
