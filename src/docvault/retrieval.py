from collections import defaultdict

from sqlalchemy import func, select

from docvault.ai.types import Evidence
from docvault.cache import cache_get, cache_set, count_metric, signature
from docvault.config import get_settings
from docvault.db import session
from docvault.documents import require_versions
from docvault.errors import AppError
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


def chunk_evidence(chunk: Chunk, version) -> Evidence:
    return Evidence(
        id=chunk.id,
        document_id=version.document_id,
        version_id=version.id,
        filename=version.filename,
        version_number=version.version_number,
        text=chunk.text,
        location=chunk.location,
    )


async def retrieve(query: str, version_ids: list[str], ai) -> list[Evidence]:
    settings = get_settings()
    with session() as db:
        versions = require_versions(db, version_ids)
        if any(v.embedding_model != settings.openrouter_embedding_model for v in versions):
            raise AppError(
                409,
                "embedding_model_changed",
                "These documents use a different embedding model. Reindex them before chatting.",
            )
    key = "query:" + signature(
        [query, settings.openrouter_embedding_model, settings.embedding_dimensions]
    )
    vector = cache_get(key)
    if vector is None:
        vector = (await ai.embed([query]))[0]
        cache_set(key, vector, 86400)
    else:
        count_metric("embedding_cache_hits")
    with session() as db:
        versions = require_versions(db, version_ids)
        evidence, chosen, remaining = {}, [], []
        for version in versions:
            base = select(Chunk).where(Chunk.version_id == version.id, Chunk.embedding.is_not(None))
            dense = list(
                db.scalars(base.order_by(Chunk.embedding.cosine_distance(vector)).limit(30))
            )
            terms = func.websearch_to_tsquery("english", query)
            lexical = list(
                db.scalars(
                    base.where(Chunk.search.op("@@")(terms))
                    .order_by(func.ts_rank_cd(Chunk.search, terms).desc())
                    .limit(30)
                )
            )
            for chunk in dense + lexical:
                evidence[chunk.id] = chunk_evidence(chunk, version)
            ranked = calculate_rrf([[c.id for c in dense], [c.id for c in lexical]])
            chosen.extend(ranked[:2])
            remaining.extend(ranked[2:])
        # Ensure selected sources are represented; no selected-document or evidence-token cap.
        selected = list(dict.fromkeys(chosen + remaining))[: max(8, len(versions) * 2)]
        return [evidence[identifier] for identifier in selected]


def citation(evidence: Evidence) -> dict:
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
