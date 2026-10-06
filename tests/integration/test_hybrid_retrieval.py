"""Verify migrations and hybrid retrieval in disposable schemas of docvault_test only."""

import ast
import asyncio
import hashlib
import math
import os
import random
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, insert, inspect, select, text
from sqlalchemy.engine import make_url
from support import require_persisted_row

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


def test_alembic_autogeneration_preserves_ledger_rename(isolated_database, tmp_path):
    """Generate and execute a fresh revision without hand-editing its ledger/index operations."""
    command.upgrade(isolated_database.alembic, PREVIOUS_REVISION)
    with isolated_database.engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO ai_calls (id, model, operation, input_tokens, duration_ms, status, created_at)
            VALUES ('generated-call', 'fixture-model', 'embedding', 12, 1, 'complete', now())
        """)
        )
    generated_directory = tmp_path / "migrations"
    shutil.copytree("migrations", generated_directory, ignore=shutil.ignore_patterns("__pycache__"))
    for revision in ScriptDirectory.from_config(isolated_database.alembic).iterate_revisions(
        "heads", PREVIOUS_REVISION
    ):
        (generated_directory / "versions" / os.path.basename(revision.path)).unlink()
    generated_config = Config("alembic.ini")
    generated_config.set_main_option("script_location", str(generated_directory))
    command.revision(
        generated_config,
        message="generation regression",
        autogenerate=True,
        rev_id="generatedcheck",
    )
    generated_source = next(
        (generated_directory / "versions").glob("generatedcheck_*.py")
    ).read_text()
    assert 'op.rename_table("ai_calls", "llm_calls")' in generated_source
    assert 'op.rename_table("llm_calls", "ai_calls")' in generated_source
    table_operations = [
        node.args[0].value
        for node in ast.walk(ast.parse(generated_source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"create_table", "drop_table"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    # Additive features may create their tables; neither direction may recreate the ledger.
    assert sorted(table_operations) == [
        "document_overviews",
        "document_overviews",
        "job_stage_runs",
        "job_stage_runs",
        "workspace_revisions",
        "workspace_revisions",
    ]
    assert 'postgresql_with={"m": 32, "ef_construction": 200}' in generated_source
    command.upgrade(generated_config, "head")
    with isolated_database.engine.connect() as connection:
        assert connection.scalar(select(LLMCall.input_tokens)) == 12
        assert (
            compare_metadata(MigrationContext.configure(connection), database.Base.metadata) == []
        )
    command.downgrade(generated_config, PREVIOUS_REVISION)
    with isolated_database.engine.connect() as connection:
        assert connection.scalar(text("SELECT input_tokens FROM ai_calls")) == 12


def test_agent_migration_preserves_old_messages_and_builds_discovery_indexes(isolated_database):
    """Backfill an empty trace for old turns and retain original data through downgrade."""
    command.upgrade(isolated_database.alembic, "b91f270f1115")
    with isolated_database.engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO chats (id, title, version_ids, created_at, updated_at)
            VALUES ('old-chat', 'Existing session', '[]'::jsonb, now(), now())
        """)
        )
        connection.execute(
            text("""
            INSERT INTO messages
                (id, chat_id, role, status, content, suggestions, citations, version_ids,
                 created_at, updated_at)
            VALUES ('old-message', 'old-chat', 'assistant', 'complete', 'Existing answer',
                    '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, now(), now())
        """)
        )
        before = dict(connection.execute(text("SELECT * FROM messages")).mappings().one())
    command.upgrade(isolated_database.alembic, "head")
    with isolated_database.engine.connect() as connection:
        after = dict(connection.execute(text("SELECT * FROM messages")).mappings().one())
        assert after.pop("agent_trace") == []
        assert after == before
        indexes = {
            row.indexname: row.indexdef
            for row in connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=:schema"),
                {"schema": isolated_database.schema},
            )
        }
        assert (
            "USING hnsw (embedding vector_cosine_ops)"
            in indexes["ix_document_overviews_embedding_hnsw"]
        )
        assert "USING gin (search)" in indexes["ix_document_overviews_search"]
    command.downgrade(isolated_database.alembic, "b91f270f1115")
    with isolated_database.engine.connect() as connection:
        assert dict(connection.execute(text("SELECT * FROM messages")).mappings().one()) == before
        assert "document_overviews" not in inspect(connection).get_table_names()


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
        keyword_vector = [0.8, 0.6] + [0.0] * 1534
        keyword = chunk_row(
            selected_id, 12000, keyword_vector, "The zephyrquartz clause requires notice."
        )
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
    statement = retrieval.build_semantic_candidates_query([selected_id], QUERY_VECTOR)
    with database.session() as db:
        retrieval.configure_hnsw_search(db)
        compiled = statement.compile(db.bind, compile_kwargs={"literal_binds": True})
        plan_result = db.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + str(compiled)))
        assert plan_result is not None
        plan = plan_result[0]
        assert "ix_chunks_embedding_hnsw" in plan_indexes(plan["Plan"])
        approximate = list(db.scalars(statement))
        assert len(approximate) == 15
        assert {chunk.version_id for chunk in approximate} == {selected_id}
        # Adding zero to distance makes this a separate exact oracle, never an app fallback.
        exact = list(
            db.scalars(
                select(Chunk.id)
                .where(
                    Chunk.version_id == selected_id,
                    Chunk.embedding.is_not(None),
                    Chunk.embedding.cosine_distance(QUERY_VECTOR) < retrieval.MAX_COSINE_DISTANCE,
                )
                .order_by(Chunk.embedding.cosine_distance(QUERY_VECTOR) + 0)
                .limit(15)
            )
        )
        recall = len({chunk.id for chunk in approximate} & set(exact)) / len(exact)
        print(
            f"HNSW default plan: {plan_indexes(plan['Plan'])}; recall@15={recall:.3f}; "
            f"execution_ms={plan['Execution Time']:.3f}"
        )
        assert recall >= 0.9
        distances = []
        for chunk in approximate:
            assert chunk.embedding is not None
            distances.append(1 - chunk.embedding[0])
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
    assert len(evidence) == 5
    assert llm.calls == [["zephyrquartz"]]


@pytest.mark.parametrize("invalid", ["empty", "missing", "deleted", "processing", "model"])
def test_source_validation_precedes_embedding(migrated_database, invalid):
    """Reject invalid source selections without paying for an embedding request."""
    version_id = seed_version()
    version_ids = [version_id]
    with database.session() as db, db.begin():
        version = require_persisted_row(db, Version, version_id)
        if invalid == "empty":
            version_ids = []
        elif invalid == "missing":
            version_ids = [str(uuid4())]
        elif invalid == "deleted":
            require_persisted_row(db, Document, version.document_id).deleted_at = now()
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
                require_persisted_row(
                    db, Version, version_id
                ).embedding_model = "changed-during-request"
            return await super().embed_texts(texts)

    with pytest.raises(AppError, match="different embedding model"):
        asyncio.run(retrieval.retrieve_relevant_chunks("payment", [version_id], MutatingLLM()))
    with database.session() as db, db.begin():
        require_persisted_row(
            db, Version, version_id
        ).embedding_model = config.get_settings().openrouter_embedding_model
        db.add(Chunk(**chunk_row(version_id, 0, QUERY_VECTOR, "Payment is due in 30 days.")))
    llm = FixedEmbeddingLLM()
    evidence = asyncio.run(retrieval.retrieve_relevant_chunks("payment", [version_id], llm))
    assert len(evidence) == 1 and evidence[0].version_id == version_id
    assert hits == ["embedding_cache_hits"]
    assert llm.calls == []


def cosine_vector(similarity: float) -> list[float]:
    """Build a unit vector with the stated cosine against the deterministic query vector."""
    return [similarity, math.sqrt(1 - similarity**2)] + [0.0] * 1534


def test_global_candidate_and_final_limits_with_independent_lexical_matches(migrated_database):
    """Apply 15/15/5 globally while lexical hits contribute independently of embeddings."""
    selected = [seed_version("first.txt"), seed_version("second.txt")]
    excluded = seed_version("outside.txt")
    rows = [
        chunk_row(selected[index % 2], index, cosine_vector(0.98 - index * 0.003), "Payment terms")
        for index in range(24)
    ]
    lexical_only = [
        chunk_row(selected[0], 25, cosine_vector(0.1), "Payment payment payment payment"),
        chunk_row(selected[1], 26, None, "Payment payment payment payment"),
    ]
    outside = chunk_row(excluded, 27, QUERY_VECTOR, "Payment payment payment payment")
    with migrated_database.engine.begin() as connection:
        connection.execute(insert(Chunk), rows + lexical_only + [outside])
    with database.session() as db:
        retrieval.configure_hnsw_search(db)
        semantic = list(
            db.scalars(retrieval.build_semantic_candidates_query(selected, QUERY_VECTOR))
        )
        lexical = list(db.scalars(retrieval.build_lexical_candidates_query("payment", selected)))
        assert len(semantic) == len(lexical) == 15
        assert {chunk.version_id for chunk in semantic} == set(selected)
        assert {chunk.version_id for chunk in lexical} == set(selected)
        assert not {chunk.id for chunk in semantic} & {row["id"] for row in lexical_only}
        assert {row["id"] for row in lexical_only} <= {chunk.id for chunk in lexical}
        assert outside["id"] not in {chunk.id for chunk in semantic + lexical}
    llm = FixedEmbeddingLLM()
    evidence = asyncio.run(retrieval.retrieve_relevant_chunks("payment", selected, llm))
    expected = retrieval.calculate_rrf(
        [[chunk.id for chunk in semantic], [chunk.id for chunk in lexical]]
    )[:5]
    assert [item.id for item in evidence] == expected
    assert len(evidence) == 5 and len({item.id for item in evidence}) == 5
    assert llm.calls == [["payment"]]


@pytest.mark.parametrize("qualifying_count", [0, 2])
def test_semantic_floor_never_pads_when_neither_branch_matches(migrated_database, qualifying_count):
    """Admit semantic scores above 0.6; do not pad with low scores lacking keyword matches."""
    selected = seed_version()
    accepted = [
        chunk_row(selected, index, cosine_vector(0.61 + index * 0.01), "Cancellation terms")
        for index in range(qualifying_count)
    ]
    rejected = [
        chunk_row(selected, 10, cosine_vector(0.59), "Unrelated details"),
        chunk_row(selected, 11, cosine_vector(0), "Unrelated details"),
        chunk_row(selected, 12, None, "Unrelated details"),
    ]
    with migrated_database.engine.begin() as connection:
        connection.execute(insert(Chunk), accepted + rejected)
    evidence = asyncio.run(
        retrieval.retrieve_relevant_chunks("notice", [selected], FixedEmbeddingLLM())
    )
    assert {item.id for item in evidence} == {str(row["id"]) for row in accepted}


def test_semantic_floor_boundary_does_not_filter_lexical_matches(migrated_database, monkeypatch):
    """Verify a strict native-distance boundary on semantic candidates only."""
    selected = seed_version()
    nearer = chunk_row(selected, 0, cosine_vector(0.61), "Payment terms")
    boundary = chunk_row(selected, 1, cosine_vector(0.6), "Payment terms")
    farther = chunk_row(selected, 2, cosine_vector(0.59), "Payment terms")
    with migrated_database.engine.begin() as connection:
        connection.execute(insert(Chunk), [nearer, boundary, farther])
    with database.session() as db:
        distance = db.scalar(
            select(Chunk.embedding.cosine_distance(QUERY_VECTOR)).where(Chunk.id == boundary["id"])
        )
    assert distance is not None
    monkeypatch.setattr(retrieval, "MAX_COSINE_DISTANCE", distance)
    with database.session() as db:
        retrieval.configure_hnsw_search(db)
        semantic = list(
            db.scalars(retrieval.build_semantic_candidates_query([selected], QUERY_VECTOR))
        )
        lexical = list(db.scalars(retrieval.build_lexical_candidates_query("payment", [selected])))
        assert [chunk.id for chunk in semantic] == [nearer["id"]]
        assert {chunk.id for chunk in lexical} == {
            str(row["id"]) for row in [nearer, boundary, farther]
        }


@pytest.mark.parametrize("embedding", [cosine_vector(0.1), None])
def test_lexical_only_evidence_survives_fusion_without_cosine_filter(migrated_database, embedding):
    """An exact term can reach the agent with weak or missing vectors, within selected scope."""
    selected = seed_version()
    outside = seed_version("unselected.txt")
    match = chunk_row(selected, 0, embedding, "Zephyrquartz requires 30 days notice.")
    with migrated_database.engine.begin() as connection:
        connection.execute(
            insert(Chunk),
            [match, chunk_row(outside, 0, QUERY_VECTOR, "Zephyrquartz requires 90 days notice.")],
        )
    evidence = asyncio.run(
        retrieval.retrieve_relevant_chunks("zephyrquartz", [selected], FixedEmbeddingLLM())
    )
    assert [item.id for item in evidence] == [match["id"]]
    assert evidence[0].version_id == selected
    assert evidence[0].text == match["text"]
