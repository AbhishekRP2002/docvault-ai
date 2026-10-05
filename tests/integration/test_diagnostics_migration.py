"""The generated diagnostics migration preserves previously persisted job history."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from test_hybrid_retrieval import isolated_database as isolated_database
from test_hybrid_retrieval import pytestmark as pytestmark

from docvault.db import Base


def test_generated_migration_preserves_old_jobs_and_attempts(isolated_database):
    """Upgrade, downgrade and re-upgrade existing failures without changing old values."""
    previous = "d18c6a20b5e9"
    command.upgrade(isolated_database.alembic, previous)
    with isolated_database.engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO jobs (id,kind,resource_id,status,stage,attempts,token,dispatches,error,next_at,created_at)
            VALUES ('job','ingest','retained-version','failed','parsing',1,1,1,'Original error',now(),now())
        """)
        )
        connection.execute(
            text("""
            INSERT INTO job_attempts (id,job_id,attempt,status,stage,error,started_at,finished_at)
            VALUES ('attempt','job',1,'failed','parsing','Original error',now(),now())
        """)
        )
        original = dict(connection.execute(text("SELECT * FROM job_attempts")).mappings().one())
    command.upgrade(isolated_database.alembic, "head")
    with isolated_database.engine.connect() as connection:
        stored = dict(connection.execute(text("SELECT * FROM job_attempts")).mappings().one())
        assert stored.pop("error_code") is None and stored.pop("error_retryable") is None
        assert stored == original
        assert connection.scalar(text("SELECT count(*) FROM job_stage_runs")) == 0
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    command.downgrade(isolated_database.alembic, previous)
    with isolated_database.engine.connect() as connection:
        assert (
            dict(connection.execute(text("SELECT * FROM job_attempts")).mappings().one())
            == original
        )
    command.upgrade(isolated_database.alembic, "head")
    with isolated_database.engine.connect() as connection:
        assert connection.scalar(text("SELECT error FROM jobs WHERE id='job'")) == "Original error"
