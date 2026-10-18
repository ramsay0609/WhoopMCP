from datetime import date

import pytest

from whoop_engine.analytics import Analytics
from whoop_engine.goals import goal_history, goal_progress, validate_goal
from whoop_engine.normalize import MetricRow, Normalized
from whoop_engine.store import Store

D = lambda m, d: date(2026, m, d)  # noqa: E731


def engine(values: dict, metric="sleep_hours", kind="sleep", events=()):
    store = Store(":memory:")
    store.replace_derived(1, Normalized(metrics=[
        MetricRow(day, metric, float(v), kind, (f"{metric}-{day}",)) for day, v in values.items()
    ]))
    store.set_meta(1, "covered_from", "2026-09-01")
    for day, label in events:
        store.add_event(1, day, label)
    return Analytics(store, 1, D(12, 31))


SLEEP_5_OF_7 = {"metric": "sleep_hours", "kind": "days", "op": ">=", "target": 7.5,
                "min_days": 5, "period": "week"}


# --- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"metric": "vibes"},
        {"kind": "streak"},
        {"op": "=="},
        {"period": "year"},
        {"target": "lots"},
        {"min_days": None},
        {"min_days": 8},
        {"min_days": 0},
    ],
)
def test_invalid_goals_are_rejected(change):
    with pytest.raises(ValueError):
        validate_goal({**SLEEP_5_OF_7, **change})


def test_valid_goal_is_normalized():
    g = validate_goal({"metric": "event: Alcohol ", "kind": "total", "op": "<=",
                       "target": "2", "period": "week"})
    assert g == {"metric": "event:alcohol", "kind": "total", "op": "<=", "target": 2.0,
                 "period": "week", "min_days": None}


# --- progress: "days" goals ----------------------------------------------


def test_days_goal_behind_pace_midweek():
    a = engine({D(10, 5): 8, D(10, 6): 7, D(10, 7): 7.6})
    p = goal_progress(a, SLEEP_5_OF_7, as_of=D(10, 7))  # Wednesday
    assert p["period"] == {"start": "2026-10-05", "end": "2026-10-11"}
    assert (p["current"], p["days_remaining"], p["status"]) == (2, 4, "behind")


def test_days_goal_on_pace():
    a = engine({D(10, 5): 8, D(10, 6): 7, D(10, 7): 7.6, D(10, 8): 8})
    assert goal_progress(a, SLEEP_5_OF_7, as_of=D(10, 8))["status"] == "on_track"


def test_days_goal_missed_once_it_can_no_longer_be_reached():
    a = engine({D(10, 5): 8})
    assert goal_progress(a, SLEEP_5_OF_7, as_of=D(10, 10))["status"] == "missed"


def test_days_goal_achieved_early():
    a = engine({D(10, n): 8 for n in range(5, 10)})
    assert goal_progress(a, SLEEP_5_OF_7, as_of=D(10, 9))["status"] == "achieved"


def test_days_without_data_never_count_toward_a_days_goal():
    a = engine({D(10, 5): 8})
    assert goal_progress(a, SLEEP_5_OF_7, as_of=D(10, 6))["current"] == 1


# --- progress: "total" goals ---------------------------------------------

WORKOUTS_4 = {"metric": "workout_count", "kind": "total", "op": ">=", "target": 4, "period": "week"}


@pytest.mark.parametrize("as_of,status", [(D(10, 7), "on_track"), (D(10, 9), "behind")])
def test_total_goal_is_judged_on_projected_pace(as_of, status):
    a = engine({D(10, 5): 1, D(10, 6): 1}, metric="workout_count", kind="workout")
    p = goal_progress(a, WORKOUTS_4, as_of=as_of)
    assert (p["current"], p["status"]) == (2, status)


ALCOHOL_MAX_2 = {"metric": "event:alcohol", "kind": "total", "op": "<=", "target": 2, "period": "week"}


@pytest.mark.parametrize(
    "drink_days,as_of,status",
    [
        ([], D(10, 7), "on_track"),
        ([5, 6, 7], D(10, 7), "missed"),
        ([5, 9], D(10, 11), "achieved"),
    ],
)
def test_limit_goal_on_logged_events(drink_days, as_of, status):
    a = engine({}, events=[(D(10, d), "alcohol") for d in drink_days])
    assert goal_progress(a, ALCOHOL_MAX_2, as_of=as_of)["status"] == status


# --- progress: "average" goals -------------------------------------------

HRV_60 = {"metric": "hrv_ms", "kind": "average", "op": ">=", "target": 60, "period": "month"}


def test_average_goal_mid_month_and_after_month_end():
    a = engine({D(10, 1): 60, D(10, 2): 64}, metric="hrv_ms", kind="recovery")
    p = goal_progress(a, HRV_60, as_of=D(10, 10))
    assert (p["period"]["end"], p["current"], p["status"]) == ("2026-10-31", 62, "on_track")
    a = engine({D(10, 1): 56, D(10, 2): 60}, metric="hrv_ms", kind="recovery")
    assert goal_progress(a, HRV_60, as_of=D(10, 31))["status"] == "missed"


def test_average_goal_without_data():
    a = engine({}, metric="hrv_ms", kind="recovery")
    assert goal_progress(a, HRV_60, as_of=D(10, 10))["status"] == "no_data"


# --- history --------------------------------------------------------------


def test_history_lists_closed_periods_newest_first_with_current_streak():
    goal = {**SLEEP_5_OF_7, "min_days": 2}
    a = engine({
        D(9, 28): 8, D(9, 29): 8,     # week of Sep 28: met
        D(10, 5): 8,                   # week of Oct 5: missed
        D(10, 12): 8, D(10, 18): 8,    # week of Oct 12: met
    })
    h = goal_history(a, goal, periods=3, as_of=D(10, 20))
    assert [(p["period"]["start"], p["met"]) for p in h["periods"]] == [
        ("2026-10-12", True), ("2026-10-05", False), ("2026-09-28", True),
    ]
    assert h["streak"] == 1
