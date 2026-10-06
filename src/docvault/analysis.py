"""Shared idempotent admission for persisted document analysis jobs."""

from typing import Literal

from sqlalchemy import select

from docvault.cache import calculate_json_fingerprint, notify_change
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import acquire_upload_advisory_lock, require_versions
from docvault.errors import AppError
from docvault.llm.prompts import build_generation_identity
from docvault.models import Artifact, Job


def get_or_create_artifact_job(
    kind: Literal["summary", "comparison"], version_ids: list[str], options: dict
):
    """Reuse or queue an artifact for ready sources, restarting previously failed results."""
    version_ids = list(dict.fromkeys(version_ids))
    if kind == "comparison" and len(version_ids) < 2:
        raise AppError(
            422, "comparison_sources", "Choose at least two distinct versions to compare."
        )
    generation_identity = build_generation_identity(get_settings().generation_model(kind), kind)
    fingerprint = calculate_json_fingerprint(
        ["artifact-v2", kind, version_ids, options, generation_identity]
    )
    options = {**options, "generation_fingerprint": calculate_json_fingerprint(generation_identity)}
    with session() as db, db.begin():
        require_versions(db, version_ids)
        acquire_upload_advisory_lock(db, f"artifact:{fingerprint}")
        artifact = db.scalar(select(Artifact).where(Artifact.signature == fingerprint))
        if artifact and artifact.status == "failed":
            artifact.status, artifact.error = "pending", None
            db.add(Job(kind=kind, resource_id=artifact.id))
        elif not artifact:
            artifact = Artifact(
                kind=kind, signature=fingerprint, version_ids=version_ids, options=options
            )
            db.add(artifact)
            db.flush()
            db.add(Job(kind=kind, resource_id=artifact.id))
        result = {"id": artifact.id, "status": artifact.status}
    notify_change()
    return result
