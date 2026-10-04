from collections import defaultdict

from sqlalchemy import func, select, text

from docvault.cache import cache_get, cache_set, calculate_json_fingerprint, count_metric
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.errors import AppError
from docvault.llm.types import Evidence
from docvault.models import Chunk


def calculate_rrf(rankings: list[list[str]], constant: int = 60) -> list[str]:
    """Return IDs ordered by reciprocal rank fusion, with deterministic ties.

    pgvector-python provides an RRF SQL example, not an importable helper:
    https://github.com/pgvector/pgvector-python/blob/master/examples/hybrid_search/rrf.py
    """
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, identifier in enumerate(ranking, 1):
            scores[identifier] += 1 / (constant + rank)
    return sorted(scores, key=lambda identifier: (-scores[identifier], identifier))


def create_cited_evidence_record(chunk: Chunk, version) -> Evidence:
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


def require_compatible_versions(db, version_ids: list[str], embedding_model: str):
    """Reject unavailable sources or sources indexed with a different embedding model."""
    versions = require_versions(db, version_ids)
    if any(version.embedding_model != embedding_model for version in versions):
        raise AppError(
            409,
            "embedding_model_changed",
            "These documents use a different embedding model. Reindex them before chatting.",
        )
    return versions


def configure_hnsw_search(db) -> None:
    """Enable ordered iterative HNSW search within the current database transaction only."""
    # pgvector >=0.8 continues scanning when version filtering discards initial neighbors.
    # https://github.com/pgvector/pgvector#iterative-index-scans
    db.execute(text("SET LOCAL hnsw.ef_search = 200"))
    db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))


def build_semantic_candidates_query(version_id: str, vector: list[float]):
    """Build a scoped nearest-neighbor query eligible for the native cosine HNSW index."""
    return (
        select(Chunk)
        .where(Chunk.version_id == version_id, Chunk.embedding.is_not(None))
        .order_by(Chunk.embedding.cosine_distance(vector))
        .limit(30)
    )


async def retrieve_relevant_chunks(query: str, version_ids: list[str], llm) -> list[Evidence]:
    """Fuse scoped cosine and full-text rankings into evidence with selected-source coverage.

    Reuse query embeddings and reject versions indexed with a different embedding model.
    HNSW is approximate; iterative filtering has bounded work and no exhaustive fallback.
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
        evidence, chosen, remaining = {}, [], []
        for version in versions:
            dense = list(db.scalars(build_semantic_candidates_query(version.id, vector)))
            terms = func.websearch_to_tsquery("english", query)
            lexical = list(
                db.scalars(
                    select(Chunk)
                    .where(Chunk.version_id == version.id, Chunk.search.op("@@")(terms))
                    .order_by(func.ts_rank_cd(Chunk.search, terms).desc())
                    .limit(30)
                )
            )
            for chunk in dense + lexical:
                evidence[chunk.id] = create_cited_evidence_record(chunk, version)
            ranked = calculate_rrf([[c.id for c in dense], [c.id for c in lexical]])
            chosen.extend(ranked[:2])
            remaining.extend(ranked[2:])
        # Ensure selected sources are represented; no selected-document or evidence-token cap.
        selected = list(dict.fromkeys(chosen + remaining))[: max(8, len(versions) * 2)]
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
