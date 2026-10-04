"""Assertions shared by tests that verify persisted database state."""

from typing import TypeVar

from sqlalchemy.orm import Session

PersistedModel = TypeVar("PersistedModel")


def require_persisted_row(
    db: Session, model: type[PersistedModel], identifier: str
) -> PersistedModel:
    """Load an expected row and fail with its identity when persistence is missing."""
    row = db.get(model, identifier)
    assert row is not None, f"Expected persisted {model.__name__} row {identifier!r}."
    return row
