from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from docvault.db import session
from docvault.models import Document, Job, LLMCall, MetricBucket, Version

router = APIRouter(prefix="/v1/metrics", tags=["metrics"])


def _usage_values(
    inputs: int | None = 0,
    outputs: int | None = 0,
    cost: float | None = None,
    requests: int = 0,
    unknown: int = 0,
):
    """Keep missing cost distinct from a measured zero or a partial known subtotal."""
    return dict(
        input_tokens=int(inputs or 0),
        output_tokens=int(outputs or 0),
        cost_usd=None if requests and requests == unknown else float(cost or 0),
        requests=int(requests),
        unknown_cost_calls=int(unknown),
    )


@router.get("/usage/history")
def get_usage_history(days: int = Query(default=30, ge=1, le=90)):
    """Daily recorded LLM usage, including today, grouped by UTC event date.

    Empty days are zero-filled. Null cost means every call lacks cost data;
    otherwise cost is the known subtotal, with unknown calls counted separately.
    Model breakdowns include the twenty most active models in the same period.
    """
    end = datetime.now(UTC)
    start = end.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    day = func.date(func.timezone("UTC", LLMCall.created_at))
    fields = (
        func.sum(LLMCall.input_tokens),
        func.sum(LLMCall.output_tokens),
        func.sum(LLMCall.cost_usd),
        func.count(),
        func.count().filter(LLMCall.cost_usd.is_(None)),
    )
    period = (LLMCall.created_at >= start, LLMCall.created_at <= end)
    with session() as db:
        daily = {
            row[0].isoformat(): _usage_values(*row[1:])
            for row in db.execute(select(day, *fields).where(*period).group_by(day))
        }
        models = [
            dict(model=row[0], **_usage_values(*row[1:]))
            for row in db.execute(
                select(LLMCall.model, *fields)
                .where(*period)
                .group_by(LLMCall.model)
                .order_by(func.count().desc(), LLMCall.model)
                .limit(20)
            )
        ]
    buckets = []
    for offset in range(days):
        date = (start + timedelta(days=offset)).date().isoformat()
        buckets.append(dict(date=date, **daily.get(date, _usage_values())))
    totals = _usage_values(
        inputs=sum(row["input_tokens"] for row in buckets),
        outputs=sum(row["output_tokens"] for row in buckets),
        cost=sum(row["cost_usd"] or 0 for row in buckets),
        requests=sum(row["requests"] for row in buckets),
        unknown=sum(row["unknown_cost_calls"] for row in buckets),
    )
    return dict(
        timezone="UTC",
        start_date=start.date().isoformat(),
        end_date=end.date().isoformat(),
        totals=totals,
        buckets=buckets,
        models=models,
    )


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
