from datetime import date

import pytest

from whoop_engine.analytics import Analytics
from whoop_engine.normalize import MetricRow, Normalized, RecordState
from whoop_engine.store import Store

D = lambda n: date(2026, 10, n)  # noqa: E731


def engine(rows=(), records=(), today=D(31), covered_from="2026-09-01"):
    store = Store(":memory:")
    store.replace_derived(1, Normalized(metrics=list(rows), records=list(records)))
    store.set_meta(1, "covered_from", covered_from)
    store.set_meta(1, "last_synced_at", "2026-10-31T12:00:00+00:00")
    return Analytics(store, 1, today)


def series(metric, values: dict, kind="recovery", incomplete=()):
    return [
        MetricRow(D(d), metric, float(v), kind, (f"{metric}-{d}",), d not in incomplete)
        for d, v in values.items()
    ]


# --- query_metric ---------------------------------------------------------


def test_mean_over_a_fully_covered_window():
    a = engine(series("hrv_ms", {1: 60, 2: 70, 3: 80}))
    r = a.query_metric("hrv_ms", D(1), D(3))
    assert r["value"] == 70
    assert (r["observed_days"], r["expected_days"], r["missing_days"]) == (3, 3, [])
    assert r["unit"] == "ms"
    assert r["source_ids"] == ["hrv_ms-1", "hrv_ms-2", "hrv_ms-3"]
    assert r["warnings"] == []
    assert r["last_synced_at"] == "2026-10-31T12:00:00+00:00"


def test_missing_days_are_listed_and_low_coverage_is_warned():
    a = engine(series("hrv_ms", {1: 60, 2: 70, 4: 80}))
    r = a.query_metric("hrv_ms", D(1), D(5))
    assert r["missing_days"] == ["2026-10-03", "2026-10-05"]
    assert any("3 of 5" in w for w in r["warnings"])


def test_future_days_are_not_expected():
    a = engine(series("hrv_ms", {1: 60, 2: 70, 3: 80}), today=D(3))
    r = a.query_metric("hrv_ms", D(1), D(10))
    assert (r["observed_days"], r["expected_days"]) == (3, 3)


def test_days_before_synced_history_are_not_expected_and_are_flagged():
    a = engine(series("hrv_ms", {5: 60, 6: 70}), covered_from="2026-10-05")
    r = a.query_metric("hrv_ms", D(1), D(6))
    assert r["expected_days"] == 2
    assert any("2026-10-05" in w for w in r["warnings"])


def test_in_progress_values_are_excluded_unless_asked_for():
    rows = series("day_strain", {1: 10, 2: 12, 3: 1.4}, kind="cycle", incomplete={3})
    a = engine(rows, today=D(3))
    r = a.query_metric("day_strain", D(1), D(3))
    assert r["value"] == 11
    assert any("in progress" in w for w in r["warnings"])
    r = a.query_metric("day_strain", D(1), D(3), include_incomplete=True)
    assert r["value"] == pytest.approx(7.8)


def test_activity_metrics_count_days_without_workouts_as_zero():
    a = engine(series("workout_count", {2: 2}, kind="workout"), today=D(4))
    r = a.query_metric("workout_count", D(1), D(4))
    assert r["value"] == 0.5
    assert (r["observed_days"], r["missing_days"]) == (4, [])
    assert a.query_metric("workout_count", D(1), D(4), aggregation="sum")["value"] == 2


def test_unscored_records_for_the_metrics_source_are_warned():
    records = [
        RecordState("recovery", "c3", D(3), "PENDING_SCORE"),
        RecordState("workout", "w3", D(3), "PENDING_SCORE"),
    ]
    a = engine(series("hrv_ms", {1: 60, 2: 70}), records, today=D(3))
    warnings = a.query_metric("hrv_ms", D(1), D(3))["warnings"]
    assert sum("not scored" in w for w in warnings) == 1
    assert any("2026-10-03" in w for w in warnings if "not scored" in w)


@pytest.mark.parametrize(
    "aggregation,want",
    [("median", 4.5), ("min", 2), ("max", 9), ("sum", 40), ("count", 8), ("stdev", 2.14)],
)
def test_aggregations(aggregation, want):
    a = engine(series("hrv_ms", dict(zip(range(1, 9), [2, 4, 4, 4, 5, 5, 7, 9]))), today=D(8))
    assert a.query_metric("hrv_ms", D(1), D(8), aggregation=aggregation)["value"] == want


def test_series_returns_each_day_with_its_source():
    a = engine(series("hrv_ms", {1: 60, 3: 80}), today=D(3))
    r = a.query_metric("hrv_ms", D(1), D(3), aggregation="series")
    assert r["series"] == [
        {"day": "2026-10-01", "value": 60, "source_ids": ["hrv_ms-1"]},
        {"day": "2026-10-03", "value": 80, "source_ids": ["hrv_ms-3"]},
    ]


def test_no_data_gives_no_value_and_a_warning():
    r = engine(today=D(3)).query_metric("hrv_ms", D(1), D(3))
    assert r["value"] is None
    assert any("No data" in w for w in r["warnings"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric": "vibes"},
        {"aggregation": "mode"},
        {"start": D(5), "end": D(1)},
    ],
)
def test_invalid_queries_are_rejected(kwargs):
    args = {"metric": "hrv_ms", "start": D(1), "end": D(3), **kwargs}
    with pytest.raises(ValueError):
        engine().query_metric(**args)


# --- baseline -------------------------------------------------------------


def test_baseline_places_the_latest_value_against_the_preceding_days():
    a = engine(series("hrv_ms", {1: 50, 2: 60, 3: 70, 4: 80, 5: 90, 6: 85}), today=D(6))
    r = a.baseline("hrv_ms", days=5)
    assert r["latest"] == {"day": "2026-10-06", "value": 85}
    assert r["baseline"]["window"] == {"start": "2026-10-01", "end": "2026-10-05"}
    assert r["baseline"]["mean"] == 70
    assert r["baseline"]["stdev"] == pytest.approx(15.81, abs=0.01)
    assert r["z_score"] == pytest.approx(0.95, abs=0.01)
    assert r["percentile_rank"] == 80


def test_baseline_uses_the_most_recent_value_when_today_has_none():
    a = engine(series("hrv_ms", {1: 50, 2: 60, 3: 70}), today=D(6))
    r = a.baseline("hrv_ms", days=2)
    assert r["latest"] == {"day": "2026-10-03", "value": 70}
    assert r["baseline"]["mean"] == 55


def test_short_baseline_is_flagged_as_provisional():
    a = engine(series("hrv_ms", {1: 50, 2: 60, 3: 70}), today=D(3))
    assert any("provisional" in w for w in a.baseline("hrv_ms", days=30)["warnings"])


def test_baseline_without_a_recent_value_has_no_score():
    a = engine(series("hrv_ms", {1: 50, 2: 60}), today=D(20))
    r = a.baseline("hrv_ms", days=5)
    assert (r["latest"], r["z_score"], r["percentile_rank"]) == (None, None, None)


# --- compare_periods ------------------------------------------------------


def test_compare_reports_change_from_first_to_second_period():
    a = engine(series("hrv_ms", {1: 55, 2: 65, 8: 60, 9: 72}), today=D(9))
    r = a.compare_periods("hrv_ms", (D(1), D(2)), (D(8), D(9)))
    assert (r["first"]["value"], r["second"]["value"]) == (60, 66)
    assert r["difference"] == 6
    assert r["pct_change"] == 10.0


def test_compare_with_an_empty_period_has_no_difference():
    a = engine(series("hrv_ms", {8: 60}), today=D(9))
    r = a.compare_periods("hrv_ms", (D(1), D(2)), (D(8), D(9)))
    assert (r["difference"], r["pct_change"]) == (None, None)


# --- verify_claim ---------------------------------------------------------


@pytest.mark.parametrize(
    "claimed,verdict",
    [(70, "matches"), (71.2, "matches"), (75, "does_not_match")],
)
def test_verify_claim_recomputes_and_compares(claimed, verdict):
    a = engine(series("hrv_ms", {1: 60, 2: 70, 3: 80}), today=D(3))
    r = a.verify_claim("hrv_ms", D(1), D(3), "mean", claimed)
    assert (r["actual"], r["verdict"]) == (70, verdict)


def test_verify_claim_without_data_cannot_verify():
    r = engine(today=D(3)).verify_claim("hrv_ms", D(1), D(3), "mean", 70)
    assert r["verdict"] == "cannot_verify"
