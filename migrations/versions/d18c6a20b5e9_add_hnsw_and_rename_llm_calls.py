"""Add native cosine HNSW retrieval and preserve the renamed LLM usage ledger."""

from alembic import op

revision = "d18c6a20b5e9"
down_revision = "5a286abcc976"
branch_labels = None
depends_on = None


def upgrade():
    """Require iterative-scan support, rename the existing ledger, and index chunk vectors."""
    op.execute("""
        DO $$
        DECLARE installed_version text;
        BEGIN
            SELECT extversion INTO installed_version FROM pg_extension WHERE extname = 'vector';
            IF installed_version IS NULL OR
               string_to_array(installed_version, '.')::int[] < ARRAY[0, 8, 0] THEN
                RAISE EXCEPTION 'Document retrieval requires pgvector >=0.8.0 (installed: %)',
                    coalesce(installed_version, 'not installed');
            END IF;
        END $$;
    """)
    op.rename_table("ai_calls", "llm_calls")
    op.execute("ALTER INDEX ix_ai_calls_resource_id RENAME TO ix_llm_calls_resource_id")
    op.execute("ALTER TABLE llm_calls RENAME CONSTRAINT ai_calls_pkey TO llm_calls_pkey")
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 32, "ef_construction": 200},
    )


def downgrade():
    """Remove the vector index and restore legacy ledger names without dropping records."""
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.execute("ALTER TABLE llm_calls RENAME CONSTRAINT llm_calls_pkey TO ai_calls_pkey")
    op.execute("ALTER INDEX ix_llm_calls_resource_id RENAME TO ix_ai_calls_resource_id")
    op.rename_table("llm_calls", "ai_calls")
