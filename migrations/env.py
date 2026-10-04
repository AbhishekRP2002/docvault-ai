from alembic import context
from sqlalchemy import create_engine

from docvault import models  # noqa: F401
from docvault.config import get_settings
from docvault.db import Base
from docvault.migration_generation import prepare_generated_migration

target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            process_revision_directives=prepare_generated_migration,
        )
        with context.begin_transaction():
            context.run_migrations()
