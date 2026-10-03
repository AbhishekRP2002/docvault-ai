from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from docvault.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine():
    """Return the shared SQLAlchemy engine with connection checks and a bounded pool."""
    return create_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10)


def session() -> Session:
    """Create a database session whose objects remain readable after commit.

    The caller owns transaction boundaries and must close the session.
    """
    return Session(get_engine(), expire_on_commit=False)
