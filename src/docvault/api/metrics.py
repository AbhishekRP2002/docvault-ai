from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from redis.exceptions import RedisError
from sqlalchemy import func, select

from docvault.db import session
from docvault.models import Document, Job, JobAttempt, JobStageRun, LLMCall, MetricBucket, Version

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
def get_processing_metrics(days: Annotated[int, Query(ge=1, le=90)] = 30):
    """Report lifetime job totals, current backlog, and measured stage/retry activity.

    Completed-job durations retain their lifetime scope for compatibility. Stage
    samples and automatic retry attempts cover the trailing ``days`` UTC interval;
    future, incomplete, and negative-duration records never become samples.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    with session() as db:
        counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        seconds = func.extract("epoch", Job.finished_at - Job.started_at) * 1000
        average, p50, p95, samples = db.execute(
            select(
                func.avg(seconds),
                func.percentile_cont(0.5).within_group(seconds),
                func.percentile_cont(0.95).within_group(seconds),
                func.count(),
            ).where(
                Job.status == "complete",
                Job.finished_at.is_not(None),
                Job.started_at.is_not(None),
                Job.finished_at >= Job.started_at,
                Job.finished_at <= end,
            )
        ).one()
        due_queued, retry_waiting, expired_leases, oldest_due = db.execute(
            select(
                func.count().filter(Job.status == "queued", Job.next_at <= end),
                func.count().filter(Job.status == "queued", Job.next_at > end, Job.attempts > 0),
                func.count().filter(
                    Job.status.in_(["enqueued", "running"]), Job.lease_until <= end
                ),
                func.min(Job.next_at).filter(
                    Job.status.in_(["queued", "enqueued"]), Job.next_at <= end
                ),
            )
        ).one()
        retry_attempts = db.scalar(
            select(func.count())
            .select_from(JobAttempt)
            .where(
                JobAttempt.attempt > 1,
                JobAttempt.started_at >= start,
                JobAttempt.started_at <= end,
            )
        )
        stage_ms = func.extract("epoch", JobStageRun.finished_at - JobStageRun.started_at) * 1000
        stage_durations = [
            dict(stage=stage, sample_count=count, p50_ms=float(median), p95_ms=float(tail))
            for stage, count, median, tail in db.execute(
                select(
                    JobStageRun.stage,
                    func.count(),
                    func.percentile_cont(0.5).within_group(stage_ms),
                    func.percentile_cont(0.95).within_group(stage_ms),
                )
                .where(
                    JobStageRun.status == "complete",
                    JobStageRun.finished_at >= start,
                    JobStageRun.finished_at <= end,
                    JobStageRun.finished_at >= JobStageRun.started_at,
                )
                .group_by(JobStageRun.stage)
                .order_by(JobStageRun.stage)
            )
        ]
        result = dict(
            completed=counts.get("complete", 0),
            failed=counts.get("failed", 0),
            dead_letters=counts.get("failed", 0),
            active=counts.get("running", 0),
            queued=counts.get("queued", 0) + counts.get("enqueued", 0),
            due_queued=due_queued,
            retry_waiting=retry_waiting,
            enqueued=counts.get("enqueued", 0),
            expired_leases=expired_leases,
            oldest_due_job_age_seconds=(end - oldest_due).total_seconds()
            if oldest_due is not None
            else None,
            automatic_retry_attempts=retry_attempts or 0,
            average_duration_ms=float(average) if average is not None else None,
            p50_duration_ms=float(p50) if p50 is not None else None,
            p95_duration_ms=float(p95) if p95 is not None else None,
            duration_sample_count=samples,
            window_start=start.isoformat(),
            window_end=end.isoformat(),
            stage_durations=stage_durations,
        )
    return {**result, **_read_consumer_metrics()}


def _read_consumer_metrics() -> dict:
    """Read queue consumers without converting an unavailable Redis into zero workers."""
    from docvault.health import read_dispatcher_status, read_worker_status

    try:
        workers = read_worker_status()
        dispatchers = read_dispatcher_status()
    except RedisError:
        return dict(
            healthy_workers=None,
            busy_workers=None,
            healthy_dispatchers=None,
            consumer_monitoring_error_code="redis_unavailable",
        )
    return dict(
        healthy_workers=sum(worker.healthy for worker in workers),
        busy_workers=sum(worker.healthy and worker.state == "busy" for worker in workers),
        healthy_dispatchers=sum(dispatcher.healthy for dispatcher in dispatchers),
        consumer_monitoring_error_code=None,
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
