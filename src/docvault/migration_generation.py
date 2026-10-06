"""Keep Alembic-generated ledger renames data-preserving.

Alembic cannot infer table renames. Transform its known add/drop pair before
rendering the revision, rather than editing a generated migration by hand:
https://alembic.sqlalchemy.org/en/latest/api/autogenerate.html
"""

from alembic.autogenerate import renderers
from alembic.operations import ops
from sqlalchemy import PrimaryKeyConstraint

PGVECTOR_REQUIREMENT_CHECK = """DO $$
DECLARE installed_version text;
BEGIN
    SELECT extversion INTO installed_version FROM pg_extension WHERE extname = 'vector';
    IF installed_version IS NULL OR
       string_to_array(installed_version, '.')::int[] < ARRAY[0, 8, 0] THEN
        RAISE EXCEPTION 'Document retrieval requires pgvector >=0.8.0 (installed: %)',
            coalesce(installed_version, 'not installed');
    END IF;
END $$;
"""

WORKSPACE_CHANGE_TABLES = (
    "documents",
    "document_versions",
    "chats",
    "messages",
    "artifacts",
    "llm_calls",
    "jobs",
)


def _workspace_revision_operations() -> tuple[list[ops.ExecuteSQLOp], list[ops.ExecuteSQLOp]]:
    """Generate transaction-scoped revision triggers together with their additive table.

    Appending one record per transaction avoids a global counter lock across
    document/job locks. SQL commit/rollback owns revisions; Redis is only a hint.
    Request metrics, job heartbeat leases and raw chunk writes are excluded.
    """
    upgrade = [
        ops.ExecuteSQLOp(
            "INSERT INTO workspace_revisions (transaction_id, epoch) VALUES (0, gen_random_uuid()::text)"
        ),
        ops.ExecuteSQLOp("""CREATE FUNCTION record_workspace_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('INSERT INTO %I.workspace_revisions (transaction_id) VALUES ($1) ON CONFLICT (transaction_id) DO NOTHING', TG_TABLE_SCHEMA)
        USING txid_current();
    RETURN NULL;
END $$;"""),
    ]
    downgrade = []
    for table in WORKSPACE_CHANGE_TABLES:
        upgrade.append(
            ops.ExecuteSQLOp(
                f"CREATE TRIGGER workspace_change_insert_delete AFTER INSERT OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION record_workspace_change()"
            )
        )
        columns = (
            " OF status, stage, attempts, error, error_code, next_at, started_at, finished_at"
            if table == "jobs"
            else ""
        )
        upgrade.append(
            ops.ExecuteSQLOp(
                f"CREATE TRIGGER workspace_change_update AFTER UPDATE{columns} ON {table} FOR EACH ROW WHEN (OLD IS DISTINCT FROM NEW) EXECUTE FUNCTION record_workspace_change()"
            )
        )
        for name in ("workspace_change_insert_delete", "workspace_change_update"):
            downgrade.append(ops.ExecuteSQLOp(f"DROP TRIGGER {name} ON {table}"))
    downgrade.append(ops.ExecuteSQLOp("DROP FUNCTION record_workspace_change()"))
    return upgrade, downgrade


@renderers.dispatch_for(ops.RenameTableOp)
def render_table_rename(autogen_context, operation: ops.RenameTableOp) -> str:
    """Render Alembic's native rename operation, including an explicit schema if present."""
    schema = f", schema={operation.schema!r}" if operation.schema is not None else ""
    return f"op.rename_table({operation.table_name!r}, {operation.new_table_name!r}{schema})"


def _ledger_table_structure(table, context) -> tuple:
    """Compare ledger columns and primary keys before replacing destructive table operations."""
    if any(not isinstance(constraint, PrimaryKeyConstraint) for constraint in table.constraints):
        raise ValueError("Ledger renames with additional constraints need a separate migration.")
    columns = {
        column.name: (
            context.dialect.type_compiler.process(column.type),
            column.nullable,
            str(column.server_default.arg) if column.server_default is not None else None,
        )
        for column in table.columns
    }
    return columns, tuple(column.name for column in table.primary_key.columns)


def _replace_ledger_table_rename(context, container, old_name: str, new_name: str) -> None:
    """Replace only the known, structurally identical ledger add/drop pair with a rename."""
    added = next(
        (
            item
            for item in container.ops
            if isinstance(item, ops.CreateTableOp) and item.table_name == new_name
        ),
        None,
    )
    removed = next(
        (
            item
            for item in container.ops
            if isinstance(item, ops.DropTableOp) and item.table_name == old_name
        ),
        None,
    )
    if added is None or removed is None:
        return
    if added.schema != removed.schema or _ledger_table_structure(
        added.to_table(context), context
    ) != _ledger_table_structure(removed.to_table(context), context):
        raise ValueError("Separate ledger structural changes from its data-preserving rename.")
    if added.schema is not None:
        raise ValueError("The ledger rename expects the configured default schema.")
    for item in container.ops:
        if isinstance(item, ops.ModifyTableOps) and item.table_name in {old_name, new_name}:
            for change in item.ops:
                if not isinstance(change, (ops.CreateIndexOp, ops.DropIndexOp)) or (
                    change.index_name != f"ix_{item.table_name}_resource_id"
                ):
                    raise ValueError("Separate ledger index changes from its rename.")
                index = change.to_index(context)
                if list(index.columns.keys()) != ["resource_id"] or index.unique:
                    raise ValueError("Separate ledger index changes from its rename.")
    remaining = [
        item
        for item in container.ops
        if item is not added
        and item is not removed
        and not (isinstance(item, ops.ModifyTableOps) and item.table_name in {old_name, new_name})
    ]
    container.ops[:] = [
        ops.RenameTableOp(old_name, new_name),
        ops.ExecuteSQLOp(
            f'ALTER INDEX "ix_{old_name}_resource_id" RENAME TO "ix_{new_name}_resource_id"'
        ),
        ops.ExecuteSQLOp(
            f'ALTER TABLE "{new_name}" RENAME CONSTRAINT "{old_name}_pkey" TO "{new_name}_pkey"'
        ),
        *remaining,
    ]


def _creates_hnsw_index(container) -> bool:
    """Find HNSW creation among the table operation groups emitted by autogeneration."""
    return any(
        (isinstance(item, ops.CreateIndexOp) and item.kw.get("postgresql_using") == "hnsw")
        or (isinstance(item, ops.OpContainer) and _creates_hnsw_index(item))
        for item in container.ops
    )


def prepare_generated_migration(context, revision, directives) -> None:
    """Preserve known ledger renames and include the HNSW compatibility preflight."""
    for script in directives:
        _replace_ledger_table_rename(context, script.upgrade_ops, "ai_calls", "llm_calls")
        _replace_ledger_table_rename(context, script.downgrade_ops, "llm_calls", "ai_calls")
        if _creates_hnsw_index(script.upgrade_ops):
            script.upgrade_ops.ops.insert(0, ops.ExecuteSQLOp(PGVECTOR_REQUIREMENT_CHECK))
        if any(
            isinstance(item, ops.CreateTableOp) and item.table_name == "workspace_revisions"
            for item in script.upgrade_ops.ops
        ):
            upgrade, downgrade = _workspace_revision_operations()
            script.upgrade_ops.ops.extend(upgrade)
            script.downgrade_ops.ops[:0] = downgrade
        if any(
            isinstance(item, ops.CreateTableOp) and item.table_name == "document_overviews"
            for item in script.upgrade_ops.ops
        ):
            for name, event in (
                ("workspace_change_insert_delete", "INSERT OR DELETE"),
                ("workspace_change_update", "UPDATE"),
            ):
                condition = " WHEN (OLD IS DISTINCT FROM NEW)" if event == "UPDATE" else ""
                script.upgrade_ops.ops.append(
                    ops.ExecuteSQLOp(
                        f"CREATE TRIGGER {name} AFTER {event} ON document_overviews FOR EACH ROW{condition} EXECUTE FUNCTION record_workspace_change()"
                    )
                )
                script.downgrade_ops.ops.insert(
                    0, ops.ExecuteSQLOp(f"DROP TRIGGER {name} ON document_overviews")
                )
