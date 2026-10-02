"""Run the durable job dispatcher: python -m docvault.dispatcher."""

import logging
import time

from redis.exceptions import RedisError
from sqlalchemy.exc import OperationalError

from docvault.jobs import dispatch_once


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        while True:
            try:
                dispatch_once()
            except (OperationalError, RedisError):
                logging.warning("Dispatcher is waiting for PostgreSQL or Redis.")
            time.sleep(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
