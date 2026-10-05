import os
from functools import lru_cache

from sqlalchemy import create_engine, event, exc
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import NullPool

from docvault.config import get_settings


class Base(DeclarativeBase):
    pass


def _create_database_engine(*, health_probe: bool = False) -> Engine:
    """Bound database waits and keep probe connections independent of the application pool."""
    settings = get_settings()
    connect_timeout = (
        settings.health_database_connect_timeout_seconds
        if health_probe
        else settings.database_connect_timeout_seconds
    )
    statement_timeout = (
        settings.health_database_statement_timeout_ms
        if health_probe
        else settings.database_statement_timeout_ms
    )
    existing_options = make_url(settings.database_url).query.get("options", "")
    if isinstance(existing_options, tuple):
        existing_options = " ".join(existing_options)
    arguments = dict(
        connect_timeout=connect_timeout,
        options=f"{existing_options} -c statement_timeout={statement_timeout} -c lock_timeout={min(settings.database_lock_timeout_ms, statement_timeout)}",
    )
    if health_probe:
        return create_engine(settings.database_url, poolclass=NullPool, connect_args=arguments)
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        connect_args=arguments,
    )

    @event.listens_for(engine, "connect")
    def record_connection_process(connection, record):
        """Remember the process that created each pooled connection."""
        record.info["pid"] = os.getpid()

    @event.listens_for(engine, "checkout")
    def reject_inherited_connection(connection, record, proxy):
        """Reconnect instead of sharing a parent's socket after an RQ fork."""
        if record.info.get("pid") != os.getpid():
            record.dbapi_connection = proxy.dbapi_connection = None
            raise exc.DisconnectionError("Database connection belongs to another process.")

    return engine


@lru_cache
def get_engine() -> Engine:
    """Return the shared, bounded, process-safe SQLAlchemy connection pool."""
    return _create_database_engine()


@lru_cache
def get_health_engine() -> Engine:
    """Return a no-pool engine with shorter query/connect waits for diagnostics."""
    return _create_database_engine(health_probe=True)


def session() -> Session:
    """Create a session; the caller owns transaction boundaries and must close it."""
    return Session(get_engine(), expire_on_commit=False)
