"""Check document and batch nullable boundaries against isolated database schemas."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support import require_persisted_row
from test_api import api as api
from test_api import ready_source
from test_api import test_database_url as test_database_url

from docvault.db import session
from docvault.documents import serialize_document_response
from docvault.models import Batch, Document, Idempotency, Job, Version

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("latest_version_id", [None, "missing-version"])
@pytest.mark.parametrize("resource", ["detail", "library"])
def test_document_without_available_latest_version_returns_a_clear_conflict(
    api, latest_version_id, resource
):
    """Reject missing version metadata instead of dereferencing or inventing a version."""
    with session() as db, db.begin():
        document = Document(title="Waiting for upload", latest_version_id=latest_version_id)
        db.add(document)
        db.flush()
        document_id = document.id
    path = f"/v1/documents/{document_id}" if resource == "detail" else "/v1/documents"
    response = api.client.get(path)
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "document_not_ready"
    assert "no available uploaded version" in error["message"]
    with session() as db:
        assert (
            require_persisted_row(db, Document, document_id).latest_version_id == latest_version_id
        )
        assert db.scalar(select(func.count()).select_from(Version)) == 0


def test_supplied_version_can_be_serialized_without_a_latest_version_pointer(api):
    """Use explicitly supplied version metadata when a document has no latest pointer."""
    source = ready_source()
    with session() as db, db.begin():
        document = require_persisted_row(db, Document, source.document)
        version = require_persisted_row(db, Version, source.version)
        document.latest_version_id = None
        response = serialize_document_response(db, document, version)
        assert response["id"] == document.id
        assert response["latest_version_id"] == version.id
        assert response["filename"] == version.filename
        assert response["status"] == "ready"


def test_missing_upload_batch_returns_not_found(api):
    """Return the existing batch error contract for an unknown identifier."""
    response = api.client.get(f"/v1/document-batches/{uuid4()}")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "batch_not_found"


def test_upload_batch_replay_rejects_a_missing_batch_and_cleans_staging_files(api):
    """Keep an orphaned idempotency record from crashing replay or leaking source files."""
    headers = {"Idempotency-Key": "missing-batch-replay"}
    files = [("files", ("terms.txt", b"Payment is due in thirty days.", "text/plain"))]
    first = api.client.post("/v1/document-batches", files=files, headers=headers)
    assert first.status_code == 202, first.text
    batch_id = first.json()["id"]
    original_files = {path.name for path in (api.storage / "sources").iterdir()}
    with session() as db, db.begin():
        db.delete(require_persisted_row(db, Batch, batch_id))
        assert (
            require_persisted_row(db, Idempotency, "batch:missing-batch-replay").resource_id
            == batch_id
        )
    replay = api.client.post("/v1/document-batches", files=files, headers=headers)
    assert replay.status_code == 404, replay.text
    assert replay.json()["error"]["code"] == "batch_not_found"
    assert {path.name for path in (api.storage / "sources").iterdir()} == original_files
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Batch)) == 0
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert db.scalar(select(func.count()).select_from(Version)) == 1
        assert db.scalar(select(func.count()).select_from(Job)) == 1
