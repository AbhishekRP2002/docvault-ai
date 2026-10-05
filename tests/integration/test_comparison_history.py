"""Saved comparisons remain discoverable independently of an open browser modal."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, func, select
from test_api import api as api
from test_api import ready_source
from test_api import test_database_url as test_database_url

from docvault.db import get_engine, session
from docvault.models import Artifact, Document, Job, Version

pytestmark = pytest.mark.integration


def test_accepted_comparison_can_be_reopened_from_history_without_new_work(api):
    sources = [ready_source("terms.txt"), ready_source("pricing.txt")]
    accepted = api.client.post(
        "/v1/comparisons",
        json={"version_ids": [source.version for source in sources], "dimensions": ["Price"]},
    )
    assert accepted.status_code == 202, accepted.text
    identifier = accepted.json()["id"]
    history = api.client.get("/v1/comparisons")
    assert history.status_code == 200, history.text
    item = history.json()["items"][0]
    assert item["id"] == identifier
    assert item["status"] == "pending"
    assert item["dimensions"] == ["Price"]
    assert [source["filename"] for source in item["sources"]] == ["terms.txt", "pricing.txt"]
    result = {"version_ids": [source.version for source in sources], "rows": []}
    with session() as db, db.begin():
        artifact = db.get(Artifact, identifier)
        assert artifact is not None
        artifact.status, artifact.data = "ready", result
    assert api.client.get("/v1/comparisons").json()["items"][0]["status"] == "ready"
    assert api.client.get(f"/v1/artifacts/{identifier}").json()["data"] == result
    with session() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "comparison")) == 1


def test_history_is_paginated_newest_first_and_does_not_include_summaries(api):
    sources = [ready_source(), ready_source("other.txt")]
    with session() as db, db.begin():
        for index, status in enumerate(["ready", "failed", "running"]):
            db.add(
                Artifact(
                    id=f"comparison-{index}",
                    kind="comparison",
                    signature=str(index) * 64,
                    version_ids=[source.version for source in sources],
                    options={"dimensions": ["Price"], "generation_fingerprint": "internal"},
                    status=status,
                    error="Provider timeout" if status == "failed" else None,
                    created_at=datetime(2026, 10, index + 1, tzinfo=UTC),
                )
            )
        db.add(Artifact(kind="summary", signature="s" * 64, version_ids=[sources[0].version]))
    first = api.client.get("/v1/comparisons?limit=2").json()
    assert first["total"] == 3
    assert [item["id"] for item in first["items"]] == ["comparison-2", "comparison-1"]
    assert first["items"][1]["error"] == "Provider timeout"
    assert "generation_fingerprint" not in first["items"][0]
    assert "data" not in first["items"][0]
    second = api.client.get("/v1/comparisons?limit=2&offset=2").json()
    assert [item["id"] for item in second["items"]] == ["comparison-0"]
    assert api.client.get("/v1/comparisons?offset=99").json() == {"items": [], "total": 3}


def test_history_preserves_original_version_metadata_after_document_replacement(api):
    sources = [ready_source("original.txt"), ready_source("other.txt")]
    api.client.post(
        "/v1/comparisons",
        json={"version_ids": [source.version for source in sources], "dimensions": ["Price"]},
    )
    with session() as db, db.begin():
        document = db.get(Document, sources[0].document)
        assert document is not None
        replacement = Version(
            document_id=document.id,
            version_number=2,
            filename="replacement.txt",
            mime_type="text/plain",
            size_bytes=1,
            sha256="f" * 64,
            storage_key="sources/replacement.txt",
            status="ready",
        )
        db.add(replacement)
        db.flush()
        document.current_version_id = document.latest_version_id = replacement.id
    original = api.client.get("/v1/comparisons").json()["items"][0]["sources"][0]
    assert original["version_id"] == sources[0].version
    assert original["filename"] == "original.txt"
    assert original["version_number"] == 1
    assert original["available"] is True


def test_history_reports_deleted_sources_without_revealing_result_or_source_name(api):
    sources = [ready_source("private.txt"), ready_source("other.txt")]
    identifier = api.client.post(
        "/v1/comparisons",
        json={"version_ids": [source.version for source in sources], "dimensions": ["Price"]},
    ).json()["id"]
    assert api.client.delete(f"/v1/documents/{sources[0].document}").status_code == 204
    item = api.client.get("/v1/comparisons").json()["items"][0]
    assert item["id"] == identifier
    assert item["sources"][0]["available"] is False
    assert item["sources"][0]["filename"] is None
    assert item["sources"][0]["title"] == "Deleted document"
    assert api.client.get(f"/v1/artifacts/{identifier}").status_code == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
def test_history_rejects_invalid_pagination(api, query):
    assert api.client.get(f"/v1/comparisons?{query}").status_code == 422


@pytest.mark.parametrize("count", [1, 10])
def test_history_queries_are_bounded_and_do_not_load_result_bodies(api, count):
    sources = [ready_source(), ready_source("other.txt")]
    with session() as db, db.begin():
        for index in range(count):
            db.add(
                Artifact(
                    kind="comparison",
                    signature=f"{index:064}",
                    version_ids=[source.version for source in sources],
                    options={"dimensions": ["Price"]},
                    data={"large_result": "not_needed_in_history"},
                )
            )
    statements: list[str] = []

    def capture_select(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture_select)
    try:
        response = api.client.get("/v1/comparisons")
    finally:
        event.remove(engine, "before_cursor_execute", capture_select)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == count
    assert len(statements) == 3
    assert all("artifacts.data" not in statement for statement in statements)
    assert all("versions.insights" not in statement for statement in statements)
