"""Reject destructive rename transformations when ledger structure also changes."""

import pytest
from alembic.migration import MigrationContext
from alembic.operations import ops
from sqlalchemy import Column, Index, MetaData, String

from docvault.migration_generation import prepare_generated_migration
from docvault.models import LLMCall


@pytest.mark.parametrize("change", ["column", "index"])
def test_ledger_rename_requires_unchanged_structure(change):
    """Fail generation rather than silently dropping a column or uniqueness change."""
    context = MigrationContext.configure(dialect_name="postgresql")
    old_table = LLMCall.__table__.to_metadata(MetaData(), name="ai_calls")
    new_table = LLMCall.__table__.to_metadata(MetaData())
    operations = [ops.CreateTableOp.from_table(new_table), ops.DropTableOp.from_table(old_table)]
    if change == "column":
        new_table.append_column(Column("new_requirement", String, nullable=False))
        operations[0] = ops.CreateTableOp.from_table(new_table)
    else:
        index = Index("ix_llm_calls_resource_id", new_table.c.resource_id, unique=True)
        operations.append(ops.ModifyTableOps("llm_calls", [ops.CreateIndexOp.from_index(index)]))
    upgrade = ops.UpgradeOps(operations)
    script = ops.MigrationScript("fixture", upgrade, ops.DowngradeOps([]))
    with pytest.raises(ValueError, match="Separate ledger"):
        prepare_generated_migration(context, (), [script])
    assert any(isinstance(item, ops.CreateTableOp) for item in upgrade.ops)
    assert any(isinstance(item, ops.DropTableOp) for item in upgrade.ops)
