"""Usage charts read real persisted events without inventing cost or activity."""

from datetime import UTC, datetime, timedelta

import pytest
from test_api import api as api
from test_api import test_database_url as test_database_url

from docvault.api import metrics
from docvault.db import session
from docvault.models import LLMCall

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 5, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            return NOW

    monkeypatch.setattr(metrics, "datetime", Clock)


def record_call(
    at: datetime,
    *,
    model: str = "test/model",
    inputs: int | None = 10,
    outputs: int | None = 2,
    cost: float | None = 0.001,
):
    with session() as db, db.begin():
        db.add(
            LLMCall(
                model=model,
                operation="generation",
                input_tokens=inputs,
                output_tokens=outputs,
                cost_usd=cost,
                status="succeeded",
                created_at=at,
            )
        )


def test_history_zero_fills_days_and_preserves_empty_cost_as_zero(api):
    response = api.client.get("/v1/metrics/usage/history?days=7")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["timezone"] == "UTC"
    assert (data["start_date"], data["end_date"]) == ("2026-09-29", "2026-10-05")
    assert len(data["buckets"]) == 7
    assert data["models"] == []
    assert data["totals"] == {
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0,
        "unknown_cost_calls": 0,
    }
    assert all(row["requests"] == 0 and row["cost_usd"] == 0 for row in data["buckets"])


def test_history_uses_utc_boundaries_and_excludes_future_events(api):
    record_call(datetime(2026, 10, 4, 0, tzinfo=UTC))
    record_call(datetime.fromisoformat("2026-10-05T00:30:00+05:30"), inputs=20)
    record_call(datetime(2026, 10, 3, 23, 59, 59, tzinfo=UTC), inputs=999)
    record_call(NOW + timedelta(seconds=1), inputs=999)
    record_call(NOW, inputs=30)
    data = api.client.get("/v1/metrics/usage/history?days=2").json()
    assert [row["requests"] for row in data["buckets"]] == [2, 1]
    assert [row["input_tokens"] for row in data["buckets"]] == [30, 30]
    assert data["totals"]["requests"] == 3
    assert data["totals"]["input_tokens"] == 60


def test_history_distinguishes_unknown_partial_and_measured_zero_cost(api):
    record_call(NOW - timedelta(days=2), model="unknown", cost=None)
    record_call(NOW - timedelta(days=1), model="mixed", cost=None)
    record_call(NOW - timedelta(days=1), model="mixed", cost=0.003)
    record_call(NOW, model="free", cost=0, inputs=None, outputs=None)
    data = api.client.get("/v1/metrics/usage/history?days=3").json()
    assert [row["cost_usd"] for row in data["buckets"]] == [None, 0.003, 0]
    assert data["totals"]["cost_usd"] == pytest.approx(0.003)
    assert data["totals"]["unknown_cost_calls"] == 2
    assert data["models"][0]["model"] == "mixed"
    assert data["models"][0]["unknown_cost_calls"] == 1
    assert next(row for row in data["models"] if row["model"] == "unknown")["cost_usd"] is None
    assert data["buckets"][-1]["input_tokens"] == 0


def test_history_does_not_turn_all_unknown_cost_into_free_usage(api):
    record_call(NOW, cost=None)
    data = api.client.get("/v1/metrics/usage/history?days=7").json()
    assert data["totals"]["cost_usd"] is None
    assert data["totals"]["unknown_cost_calls"] == 1
    # Existing lifetime endpoint remains compatible.
    assert api.client.get("/v1/metrics/usage").json()["cost_usd"] == 0


@pytest.mark.parametrize("days", ["0", "91", "nope"])
def test_history_rejects_unbounded_or_invalid_ranges(api, days):
    assert api.client.get(f"/v1/metrics/usage/history?days={days}").status_code == 422


def test_history_limits_models_without_truncating_totals(api):
    for index in range(22):
        record_call(NOW, model=f"model/{index:02}")
    data = api.client.get("/v1/metrics/usage/history?days=1").json()
    assert len(data["models"]) == 20
    assert data["models"][0]["model"] == "model/00"
    assert data["totals"]["requests"] == 22
