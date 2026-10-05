"""Run the durable job dispatcher: python -m docvault.dispatcher."""

import logging
import os
import signal
import socket
import time
from datetime import UTC, datetime
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from docvault.health import (
    publish_dispatcher_status,
    remove_dispatcher_status,
    remove_process_identity,
    save_process_identity,
)
from docvault.jobs import dispatch_pending_jobs


def run_dispatcher_cycle(identifier: str, last_success: datetime | None) -> datetime | None:
    """Report successful recovery/publication or safe dependency failure from the actual loop."""
    error_code = None
    try:
        dispatch_pending_jobs(raise_publish_errors=True)
        last_success = datetime.now(UTC)
    except RedisError:
        error_code = "redis_unavailable"
    except SQLAlchemyError:
        error_code = "database_unavailable"
    if error_code:
        logging.warning("Dispatcher is waiting for PostgreSQL or Redis.")
    try:
        publish_dispatcher_status(identifier, last_success, error_code)
    except RedisError:
        # A stale/absent observation is unhealthy; do not publish a success elsewhere.
        logging.warning("Dispatcher health publication is unavailable.")
    return last_success


def request_dispatcher_stop(signum, frame) -> None:
    """Use the same cleanup path for container SIGTERM and terminal interruption."""
    raise KeyboardInterrupt


def main() -> None:
    """Poll durable dispatch and recovery, publishing only from completed dispatcher cycles."""
    logging.basicConfig(level=logging.INFO)
    identifier = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    save_process_identity("dispatcher", identifier)
    previous_handler = signal.signal(signal.SIGTERM, request_dispatcher_stop)
    last_success = None
    try:
        while True:
            last_success = run_dispatcher_cycle(identifier, last_success)
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        remove_process_identity("dispatcher", identifier)
        try:
            remove_dispatcher_status(identifier)
        except RedisError:
            logging.warning("Dispatcher observation cleanup is unavailable; TTL will expire it.")


if __name__ == "__main__":
    main()
