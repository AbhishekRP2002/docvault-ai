"""Run the durable job dispatcher: python -m docvault.dispatcher."""

import logging
import time

from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError

from docvault.jobs import dispatch_pending_jobs


def main() -> None:
    """Poll durable job dispatch and recovery, retrying dependency outages until interrupted."""
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            try:
                dispatch_pending_jobs()
            except (OperationalError, RedisError):
                logging.warning("Dispatcher is waiting for PostgreSQL or Redis.")
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
