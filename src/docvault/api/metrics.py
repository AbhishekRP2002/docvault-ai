from fastapi import APIRouter
from sqlalchemy import func, select

from docvault.db import session
from docvault.models import Document, Job, LLMCall, MetricBucket, Version

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])


@router.get("/documents")
def get_document_metrics():
    """Count live documents, their version statuses, and total source-file storage bytes."""
    with session() as db:
        docs = db.scalar(
            select(func.count()).select_from(Document).where(Document.deleted_at.is_(None))
        )
        rows = list(db.scalars(select(Version).join(Document).where(Document.deleted_at.is_(None))))
        return dict(
            documents=docs if docs is not None else 0,
            versions=len(rows),
            ready=sum(v.status == "ready" for v in rows),
            processing=sum(v.status not in {"ready", "failed"} for v in rows),
            failed=sum(v.status == "failed" for v in rows),
            storage_bytes=sum(v.size_bytes for v in rows),
        )


@router.get("/processing")
def get_processing_metrics():
    """Return job counts and mean/p95 completed-job durations in milliseconds."""
    with session() as db:
        counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        seconds = func.extract("epoch", Job.finished_at - Job.started_at) * 1000
        average, p95 = db.execute(
            select(func.avg(seconds), func.percentile_cont(0.95).within_group(seconds)).where(
                Job.status == "complete", Job.finished_at.is_not(None), Job.started_at.is_not(None)
            )
        ).one()
        return dict(
            completed=counts.get("complete", 0),
            failed=counts.get("failed", 0),
            active=counts.get("running", 0),
            queued=counts.get("queued", 0) + counts.get("enqueued", 0),
            average_duration_ms=float(average) if average is not None else None,
            p95_duration_ms=float(p95) if p95 is not None else None,
        )


@router.get("/usage")
def get_llm_usage_metrics():
    """Sum recorded LLM tokens and known costs, count unknown-cost calls, and report cache hits."""
    with session() as db:
        inputs, outputs, cost, requests, unknown = db.execute(
            select(
                func.coalesce(func.sum(LLMCall.input_tokens), 0),
                func.coalesce(func.sum(LLMCall.output_tokens), 0),
                func.coalesce(func.sum(LLMCall.cost_usd), 0),
                func.count(),
                func.count().filter(LLMCall.cost_usd.is_(None)),
            )
        ).one()
        hits = db.scalar(
            select(func.coalesce(func.sum(MetricBucket.count), 0)).where(
                MetricBucket.key.in_(
                    [
                        "embedding_cache_hits",
                        "embedding_cache_hit",
                        "parser_cache_hit",
                        "answer_cache_hits",
                    ]
                )
            )
        )
        return dict(
            input_tokens=int(inputs if inputs is not None else 0),
            output_tokens=int(outputs if outputs is not None else 0),
            cost_usd=float(cost if cost is not None else 0),
            requests=requests,
            unknown_cost_calls=unknown,
            cache_hits=int(hits if hits is not None else 0),
        )
