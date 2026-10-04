"""Verify migrations and hybrid retrieval in disposable schemas of docvault_test only."""

import asyncio
import hashlib
import math
import os
import random
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import make_url

from docvault import config, retrieval
from docvault import db as database
from docvault.errors import AppError
from docvault.models import Chunk, Document, LLMCall, Version, now

pytestmark = pytest.mark.integration
PREVIOUS_REVISION = "5a286abcc976"
QUERY_VECTOR = [1.0] + [0.0] * 1535


@pytest.fixture
def isolated_database(monkeypatch):
    """Own a random schema and redirect application and migration connections into it."""
    value = os.getenv("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to the dedicated docvault_test database.")
    url = make_url(value)
    if url.database != "docvault_test":
        pytest.fail("Retrieval tests require the dedicated docvault_test database.")
    schema = "retrieval_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        extension = connection.scalar(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        assert extension and tuple(map(int, extension.split("."))) >= (0, 8, 0)
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        # An empty local version table prevents resolving another schema's migration state.
        connection.execute(
            text(f'CREATE TABLE "{schema}".alembic_version (version_num varchar(32) PRIMARY KEY)')
        )
    options = dict(url.query, options=f"-csearch_path={schema},public")
    scoped_url = url.set(query=options)
    engine = create_engine(scoped_url)
    monkeypatch.setenv("DATABASE_URL", scoped_url.render_as_string(hide_password=False))
    config.get_settings.cache_clear()
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(retrieval, "cache_get", lambda key: None)
    monkeypatch.setattr(retrieval, "cache_set", lambda *args: None)
    monkeypatch.setattr(retrieval, "count_metric", lambda *args: None)
    try:
        yield SimpleNamespace(engine=engine, schema=schema, alembic=Config("alembic.ini"))
    finally:
        engine.dispose()
        config.get_settings.cache_clear()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def migrated_database(isolated_database):
    """Apply the real migration chain before exercising current retrieval behavior."""
    command.upgrade(isolated_database.alembic, "head")
    return isolated_database


def seed_version(filename="selected.txt"):
    """Persist a ready source using the configured embedding model and return its ID."""
    with database.session() as db, db.begin():
        document = Document(title=filename)
        db.add(document)
        db.flush()
        version = Version(
            document_id=document.id,
            version_number=1,
            filename=filename,
            mime_type="text/plain",
            size_bytes=20,
            sha256=hashlib.sha256(filename.encode()).hexdigest(),
            storage_key=f"sources/{uuid4()}.txt",
            status="ready",
            embedding_model=config.get_settings().openrouter_embedding_model,
        )
        db.add(version)
        db.flush()
        document.current_version_id = document.latest_version_id = version.id
        return version.id


def chunk_row(version_id, ordinal, embedding, content="Ordinary contract details."):
    """Create a complete chunk insert mapping with stable source location metadata."""
    return dict(
        id=str(uuid4()),
        version_id=version_id,
        ordinal=ordinal,
        text=content,
        embedding_text=content,
        input_hash=hashlib.sha256(f"{version_id}:{ordinal}".encode()).hexdigest(),
        location={"paragraph": ordinal + 1},
        token_count=5,
        embedding=embedding,
    )


class FixedEmbeddingLLM:
    def __init__(self):
        """Track deterministic embedding calls without accessing any provider."""
        self.calls = []

    async def embed_texts(self, texts):
        """Return the fixture query vector and record inputs for boundary assertions."""
        self.calls.append(texts)
        return [QUERY_VECTOR for _ in texts]


def plan_indexes(plan):
    """Collect index names recursively from a PostgreSQL JSON execution plan."""
    names = {plan["Index Name"]} if "Index Name" in plan else set()
    for child in plan.get("Plans", []):
        names.update(plan_indexes(child))
    return names


def test_migration_preserves_ledger_and_existing_chunks(isolated_database):
    """Upgrade and downgrade real persisted records and match current ORM schema metadata."""
    command.upgrade(isolated_database.alembic, PREVIOUS_REVISION)
    identifier = str(uuid4())
    with isolated_database.engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO ai_calls
                    (id, resource_id, model, operation, input_tokens, output_tokens, cached_tokens,
                     cost_usd, duration_ms, status, request_id, created_at)
                VALUES (:id, :id, 'original-model', 'embedding', 123, 0, 2,
                        0.000123, 20.5, 'complete', 'original-request', now())
            """),
            {"id": identifier},
        )
        original_call = dict(connection.execute(text("SELECT * FROM ai_calls")).mappings().one())
    version_id = seed_version()
    chunk = chunk_row(version_id, 0, QUERY_VECTOR)
    with isolated_database.engine.begin() as connection:
        connection.execute(insert(Chunk), [chunk])
    command.upgrade(isolated_database.alembic, "head")
    with isolated_database.engine.connect() as connection:
        assert "ai_calls" not in inspect(connection).get_table_names()
        assert dict(connection.execute(text("SELECT * FROM llm_calls")).mappings().one()) == (
            original_call
        )
        indexes = {
            row.indexname: row.indexdef
            for row in connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :schema"),
                {"schema": isolated_database.schema},
            )
        }
        assert "ix_llm_calls_resource_id" in indexes and "llm_calls_pkey" in indexes
        assert "ix_ai_calls_resource_id" not in indexes
        definition = indexes["ix_chunks_embedding_hnsw"]
        assert "USING hnsw (embedding vector_cosine_ops)" in definition
        assert "m='32'" in definition and "ef_construction='200'" in definition
        assert "USING gin (search)" in indexes["ix_chunks_search"]
        assert connection.scalar(select(func.count()).select_from(Chunk)) == 1
        assert connection.scalar(select(Chunk.text)) == chunk["text"]
        assert list(connection.scalar(select(Chunk.embedding))) == QUERY_VECTOR
        assert connection.scalar(select(LLMCall.input_tokens)) == 123
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, database.Base.metadata) == []
    command.downgrade(isolated_database.alembic, PREVIOUS_REVISION)
    with isolated_database.engine.connect() as connection:
        assert dict(connection.execute(text("SELECT * FROM ai_calls")).mappings().one()) == (
            original_call
        )
        assert "llm_calls" not in inspect(connection).get_table_names()
        assert connection.scalar(select(func.count()).select_from(Chunk)) == 1
        assert list(connection.scalar(select(Chunk.embedding))) == QUERY_VECTOR
        assert "ix_chunks_embedding_hnsw" not in {
            item["name"] for item in inspect(connection).get_indexes("chunks")
        }
    command.upgrade(isolated_database.alembic, "head")
    with isolated_database.engine.connect() as connection:
        assert connection.scalar(select(LLMCall.id)) == identifier


def seed_retrieval_corpus(engine):
    """Persist 12,000 full-size vectors with closer out-of-scope neighbors and keyword hits."""
    selected_id, other_id = seed_version(), seed_version("unselected.txt")
    rng = random.Random(1729)
    with engine.begin() as connection:
        for start in range(0, 12000, 250):
            rows = []
            for ordinal in range(start, start + 250):
                selected = ordinal < 4000 or (ordinal >= 6000 and ordinal % 3 != 2)
                # Three hundred unselected vectors are closer than every selected vector.
                if ordinal >= 6000:
                    cosine = 0.8 - (ordinal - 6000) / 6000 * 1.6
                elif selected:
                    cosine = 0.94 - ordinal / 4000 * 1.7
                elif ordinal < 4300:
                    cosine = 0.999 - (ordinal - 4000) / 300 * 0.03
                else:
                    cosine = 0.85 - (ordinal - 4300) / 1700 * 1.6
                tail = [rng.uniform(-1, 1) for _ in range(1535)]
                scale = math.sqrt((1 - cosine * cosine) / sum(value * value for value in tail))
                vector = [cosine] + [value * scale for value in tail]
                rows.append(chunk_row(selected_id if selected else other_id, ordinal, vector))
            connection.execute(insert(Chunk), rows)
        keyword = chunk_row(selected_id, 12000, None, "The zephyrquartz clause requires notice.")
        connection.execute(insert(Chunk), [keyword])
        connection.execute(
            insert(Chunk),
            [chunk_row(other_id, 12001, None, "Unselected zephyrquartz text must stay excluded.")],
        )
        connection.execute(text("ANALYZE chunks"))
    return selected_id, other_id, keyword


def test_hnsw_default_plan_filtered_recall_and_keyword_rescue(migrated_database):
    """Exercise the actual scoped query over 12,000 full-size vectors with competing sources."""
    selected_id, other_id, keyword = seed_retrieval_corpus(migrated_database.engine)
    statement = retrieval.build_semantic_candidates_query(selected_id, QUERY_VECTOR)
    with database.session() as db:
        retrieval.configure_hnsw_search(db)
        compiled = statement.compile(db.bind, compile_kwargs={"literal_binds": True})
        plan = db.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled)))[0]
        assert "ix_chunks_embedding_hnsw" in plan_indexes(plan["Plan"])
        approximate = list(db.scalars(statement))
        assert len(approximate) == 30
        assert {chunk.version_id for chunk in approximate} == {selected_id}
        # Adding zero to distance makes this a separate exact oracle, never an app fallback.
        exact = list(
            db.scalars(
                select(Chunk.id)
                .where(Chunk.version_id == selected_id, Chunk.embedding.is_not(None))
                .order_by(Chunk.embedding.cosine_distance(QUERY_VECTOR) + 0)
                .limit(30)
            )
        )
        recall = len({chunk.id for chunk in approximate} & set(exact)) / len(exact)
        print(
            f"HNSW default plan: {plan_indexes(plan['Plan'])}; recall@30={recall:.3f}; "
            f"execution_ms={plan['Execution Time']:.3f}"
        )
        assert recall >= 0.9
        distances = [1 - chunk.embedding[0] for chunk in approximate]
        assert distances == sorted(distances)
        assert db.scalar(text("SHOW hnsw.ef_search")) == "200"
        assert db.scalar(text("SHOW hnsw.iterative_scan")) == "strict_order"
    with database.session() as db:
        assert db.scalar(text("SHOW hnsw.ef_search")) == "40"
        assert db.scalar(text("SHOW hnsw.iterative_scan")) == "off"
    llm = FixedEmbeddingLLM()
    evidence = asyncio.run(retrieval.retrieve_relevant_chunks("zephyrquartz", [selected_id], llm))
    assert keyword["id"] in {item.id for item in evidence}
    assert {item.version_id for item in evidence} == {selected_id}
    assert len(evidence) == 8
    assert llm.calls == [["zephyrquartz"]]


@pytest.mark.parametrize("invalid", ["empty", "missing", "deleted", "processing", "model"])
def test_source_validation_precedes_embedding(migrated_database, invalid):
    """Reject invalid source selections without paying for an embedding request."""
    version_id = seed_version()
    version_ids = [version_id]
    with database.session() as db, db.begin():
        version = db.get(Version, version_id)
        if invalid == "empty":
            version_ids = []
        elif invalid == "missing":
            version_ids = [str(uuid4())]
        elif invalid == "deleted":
            db.get(Document, version.document_id).deleted_at = now()
        elif invalid == "processing":
            version.status = "embedding"
        else:
            version.embedding_model = "different-model"
    llm = FixedEmbeddingLLM()
    with pytest.raises(AppError) as error:
        asyncio.run(retrieval.retrieve_relevant_chunks("payment", version_ids, llm))
    assert (
        error.value.code
        == {
            "empty": "documents_required",
            "missing": "version_not_found",
            "deleted": "version_not_found",
            "processing": "document_not_ready",
            "model": "embedding_model_changed",
        }[invalid]
    )
    assert llm.calls == []


def test_scope_revalidated_after_embedding_and_cache_reused(migrated_database, monkeypatch):
    """Recheck source-model compatibility after provider I/O and reuse valid cached vectors."""
    version_id = seed_version()
    stored = {}
    hits = []
    monkeypatch.setattr(retrieval, "cache_get", stored.get)
    monkeypatch.setattr(retrieval, "cache_set", lambda key, value, ttl: stored.update({key: value}))
    monkeypatch.setattr(retrieval, "count_metric", hits.append)

    class MutatingLLM(FixedEmbeddingLLM):
        async def embed_texts(self, texts):
            """Change the indexed model while the query embedding request is in flight."""
            with database.session() as db, db.begin():
                db.get(Version, version_id).embedding_model = "changed-during-request"
            return await super().embed_texts(texts)

    with pytest.raises(AppError, match="different embedding model"):
        asyncio.run(retrieval.retrieve_relevant_chunks("payment", [version_id], MutatingLLM()))
    with database.session() as db, db.begin():
        db.get(
            Version, version_id
        ).embedding_model = config.get_settings().openrouter_embedding_model
        db.add(Chunk(**chunk_row(version_id, 0, QUERY_VECTOR, "Payment is due in 30 days.")))
    llm = FixedEmbeddingLLM()
    evidence = asyncio.run(retrieval.retrieve_relevant_chunks("payment", [version_id], llm))
    assert len(evidence) == 1 and evidence[0].version_id == version_id
    assert hits == ["embedding_cache_hits"]
    assert llm.calls == []
