"""Stable snapshots of committed workspace changes, independent of Redis delivery."""

from sqlalchemy import func, select

from docvault.db import session
from docvault.models import WorkspaceRevision


def read_workspace_revision() -> str:
    """Count committed transaction records so out-of-order commits also change the revision.

    Transaction IDs alone can commit out of order. The count changes for every
    committed insertion; the seed epoch distinguishes a freshly initialized DB.
    Rollback removes its change record along with the application mutation.
    """
    with session() as db:
        epoch, count = db.execute(
            select(func.max(WorkspaceRevision.epoch), func.count()).select_from(WorkspaceRevision)
        ).one()
    if epoch is None:
        raise RuntimeError("Workspace revisions require the current migration.")
    return f"{epoch}:{count}"
