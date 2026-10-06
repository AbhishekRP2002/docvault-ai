from collections import defaultdict

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from docvault.cache import cache_get, cache_set, calculate_json_fingerprint, count_metric
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.errors import AppError
from docvault.llm.models import Evidence
from docvault.models import Chunk, Version

SEMANTIC_CANDIDATE_LIMIT = 15
LEXICAL_CANDIDATE_LIMIT = 15
RETRIEVED_CHUNK_LIMIT = 5
MIN_COSINE_SIMILARITY = 0.6
# Only semantic candidates use this floor; pgvector returns 1 - cosine similarity.
MAX_COSINE_DISTANCE = 1 - MIN_COSINE_SIMILARITY
RETRIEVAL_CONFIGURATION = {
    "pipeline": "hybrid-independent-rrf-v4",
    "semantic_candidates": SEMANTIC_CANDIDATE_LIMIT,
    "lexical_candidates": LEXICAL_CANDIDATE_LIMIT,
    "final_chunks": RETRIEVED_CHUNK_LIMIT,
    "semantic_min_cosine_similarity": MIN_COSINE_SIMILARITY,
}


def calculate_rrf(rankings: list[list[str]], constant: int = 60) -> list[str]:
    """Return IDs ordered by reciprocal rank fusion, with deterministic ties."""
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, 1):
            scores[identifier] += 1 / (constant + rank)
    return sorted(scores, key=lambda identifier: (-scores[identifier], identifier))


def create_cited_evidence_record(chunk: Chunk, version: Version) -> Evidence:
    """Combine stored chunk content and source metadata into a citation-ready evidence record."""
    return Evidence(
        id=chunk.id,
        document_id=version.document_id,
        version_id=version.id,
        filename=version.filename,
        version_number=version.version_number,
        text=chunk.text,
        location=chunk.location,
    )


def require_compatible_versions(
    db: Session, version_ids: list[str], embedding_model: str
) -> list[Version]:
    """Reject unavailable sources or sources indexed with a different embedding model."""
    versions = require_versions(db, version_ids)
    if any(version.embedding_model != embedding_model for version in versions):
        raise AppError(
            409,
            "embedding_model_changed",
            "These documents use a different embedding model. Reindex them before chatting.",
        )
    return versions


def configure_hnsw_search(db: Session) -> None:
    """Enable ordered iterative HNSW search within the current database transaction only."""
    # pgvector >=0.8 continues scanning when version filtering discards initial neighbors.
    # https://github.com/pgvector/pgvector#iterative-index-scans
    db.execute(text("SET LOCAL hnsw.ef_search = 200"))
    db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))


def build_semantic_candidates_query(version_ids: list[str], vector: list[float]):
    """Fetch scoped HNSW neighbors first, then apply the strict cosine-distance floor.

    A materialized CTE keeps distance filtering outside the nearest-neighbor scan, as
    recommended by pgvector; source scope and the 15-candidate cap stay inside it.
    """
    nearest = (
        select(Chunk.id, Chunk.embedding.cosine_distance(vector).label("distance"))
        .where(
            Chunk.version_id.in_(version_ids),
            Chunk.embedding.is_not(None),
        )
        .order_by(Chunk.embedding.cosine_distance(vector))
        .limit(SEMANTIC_CANDIDATE_LIMIT)
        .cte("nearest_chunks")
        .prefix_with("MATERIALIZED")
    )
    # https://github.com/pgvector/pgvector#iterative-index-scans
    return (
        select(Chunk)
        .join(nearest, Chunk.id == nearest.c.id)
        .where(nearest.c.distance < MAX_COSINE_DISTANCE)
        .order_by(nearest.c.distance)
    )


def build_lexical_candidates_query(query: str, version_ids: list[str]):
    """Rank scoped full-text matches independently of semantic scores or embeddings."""
    terms = func.websearch_to_tsquery("english", query)
    return (
        select(Chunk)
        .where(
            Chunk.version_id.in_(version_ids),
            Chunk.search.op("@@")(terms),
        )
        .order_by(func.ts_rank_cd(Chunk.search, terms).desc(), Chunk.id)
        .limit(LEXICAL_CANDIDATE_LIMIT)
    )


async def retrieve_relevant_chunks(query: str, version_ids: list[str], llm) -> list[Evidence]:
    """Fuse 15 global semantic/lexical candidates into at most five qualifying chunks.

    Reuse query embeddings and reject versions indexed with a different embedding model.
    HNSW is approximate; iterative filtering has bounded work and no exhaustive fallback.
    Semantic candidates require cosine similarity strictly above 0.6; lexical candidates
    qualify by full-text matching alone. RRF combines both lists without a final cosine
    filter. Return fewer or none when neither branch matches, without padding results or
    guaranteeing every source.
    """
    settings = get_settings()
    with session() as db:
        require_compatible_versions(db, version_ids, settings.openrouter_embedding_model)
    key = "query:" + calculate_json_fingerprint(
        [query, settings.openrouter_embedding_model, settings.embedding_dimensions]
    )
    vector = cache_get(key)
    if vector is None:
        vector = (await llm.embed_texts([query]))[0]
        cache_set(key, vector, 86400)
    else:
        count_metric("embedding_cache_hits")
    with session() as db:
        versions = require_compatible_versions(db, version_ids, settings.openrouter_embedding_model)
        configure_hnsw_search(db)
        versions_by_id = {version.id: version for version in versions}
        dense = list(db.scalars(build_semantic_candidates_query(list(versions_by_id), vector)))
        lexical = list(db.scalars(build_lexical_candidates_query(query, list(versions_by_id))))
        evidence = {
            chunk.id: create_cited_evidence_record(chunk, versions_by_id[chunk.version_id])
            for chunk in dense + lexical
        }
        selected = calculate_rrf([[c.id for c in dense], [c.id for c in lexical]])[
            :RETRIEVED_CHUNK_LIMIT
        ]
        return [evidence[identifier] for identifier in selected]


def build_public_citation(evidence: Evidence) -> dict:
    """Build a public citation payload from an evidence record and its original text and location."""
    return dict(
        citation_id=evidence.id,
        document_id=evidence.document_id,
        version_id=evidence.version_id,
        chunk_id=evidence.id,
        filename=evidence.filename,
        version_number=evidence.version_number,
        location=evidence.location,
        quote=evidence.text,
    )
