"""Run one native RQ queue consumer: python -m docvault.worker."""

import os
import socket
import sys
from uuid import uuid4

from rq import Worker
from rq.worker import SpawnWorker

from docvault.cache import redis_client
from docvault.config import get_settings
from docvault.health import remove_process_identity, save_process_identity


def main() -> None:
    """Run one consumer with native RQ heartbeats; no independent liveness ticker."""
    worker_type = SpawnWorker if sys.platform == "darwin" else Worker
    identifier = f"docvault-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    # RQ's dequeue wait is worker_ttl minus 15 seconds; idle heartbeats must stay fresh.
    freshness_window = get_settings().health_heartbeat_max_age_seconds
    worker = worker_type(
        ["docvault"],
        name=identifier,
        connection=redis_client(),
        worker_ttl=min(90, freshness_window),
        job_monitoring_interval=min(30, freshness_window // 3),
    )
    save_process_identity("worker", identifier)
    try:
        worker.work()
    finally:
        remove_process_identity("worker", identifier)


if __name__ == "__main__":
    main()
