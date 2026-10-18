from datetime import date

import pytest

from tests import factories as f
from whoop_engine.normalize import normalize


def values(result):
    return {(r.day.isoformat(), r.metric): r.value for r in result.metrics}


def row(result, day, metric):
    [r] = [r for r in result.metrics if r.day.isoformat() == day and r.metric == metric]
    return r


def state(result, kind, id):
    [s] = [s for s in result.records if s.kind == kind and s.id == str(id)]
    return s


# --- Sleep ----------------------------------------------------------------


def test_sleep_counts_toward_local_wake_day_not_utc_day():
    # Wakes 22:30 local on Oct 2 (a daytime sleeper) = 02:30 UTC on Oct 3.
    s = f.sleep("2026-10-02T19:00:00Z", "2026-10-03T02:30:00Z", tz="-04:00")
    result = normalize(sleeps=[s])
    assert ("2026-10-02", "sleep_hours") in values(result)
    assert ("2026-10-03", "sleep_hours") not in values(result)


def test_sleep_hours_is_light_plus_deep_plus_rem_excluding_awake():
    s = f.sleep(
        "2026-10-02T03:00:00Z", "2026-10-02T11:00:00Z",
        light_ms=14_400_000, sws_ms=3_600_000, rem_ms=7_200_000, awake_ms=1_800_000,
    )
    result = normalize(sleeps=[s])
    assert values(result)[("2026-10-02", "sleep_hours")] == pytest.approx(7.0)


def test_sleep_scores_are_exposed_as_metrics():
    s = f.sleep(
        "2026-10-02T03:00:00Z", "2026-10-02T11:00:00Z",
        performance=75, efficiency=92.5, consistency=45, respiratory_rate=14.9,
    )
    v = values(normalize(sleeps=[s]))
    assert v[("2026-10-02", "sleep_performance_pct")] == 75
    assert v[("2026-10-02", "sleep_efficiency_pct")] == 92.5
    assert v[("2026-10-02", "sleep_consistency_pct")] == 45
    assert v[("2026-10-02", "respiratory_rate")] == 14.9


def test_nap_is_tracked_separately_and_never_counts_as_night_sleep():
    night = f.sleep("2026-10-02T03:00:00Z", "2026-10-02T11:00:00Z", performance=80,
                    light_ms=18_000_000, sws_ms=3_600_000, rem_ms=3_600_000)
    nap = f.sleep("2026-10-02T18:00:00Z", "2026-10-02T19:00:00Z", nap=True, performance=20,
                  light_ms=1_800_000, sws_ms=0, rem_ms=0, awake_ms=0)
    v = values(normalize(sleeps=[night, nap]))
    assert v[("2026-10-02", "sleep_hours")] == pytest.approx(7.0)
    assert v[("2026-10-02", "nap_hours")] == pytest.approx(0.5)
    assert v[("2026-10-02", "sleep_performance_pct")] == 80
    assert state(normalize(sleeps=[night, nap]), "sleep", nap["id"]).role == "nap"


def test_unscored_sleep_produces_no_metrics_but_is_recorded_with_its_day():
    s = f.sleep("2026-10-02T03:00:00Z", "2026-10-02T11:00:00Z", state="PENDING_SCORE")
    result = normalize(sleeps=[s])
    assert result.metrics == []
    st = state(result, "sleep", s["id"])
    assert st.day == date(2026, 10, 2)
    assert st.score_state == "PENDING_SCORE"


def test_two_main_sleeps_on_one_day_sum_hours_and_take_scores_from_longest():
    short = f.sleep("2026-10-02T02:00:00Z", "2026-10-02T05:00:00Z", performance=30,
                    light_ms=3_600_000, sws_ms=3_600_000, rem_ms=3_600_000)
    long = f.sleep("2026-10-02T12:00:00Z", "2026-10-02T20:00:00Z", performance=70,
                   light_ms=10_800_000, sws_ms=7_200_000, rem_ms=7_200_000)
    result = normalize(sleeps=[short, long])
    v = values(result)
    assert v[("2026-10-02", "sleep_hours")] == pytest.approx(10.0)
    assert v[("2026-10-02", "sleep_performance_pct")] == 70
    assert set(row(result, "2026-10-02", "sleep_hours").source_ids) == {short["id"], long["id"]}


# --- Cycles and recovery --------------------------------------------------


def test_cycle_lands_on_the_wake_day_of_its_sleep_even_when_it_starts_the_evening_before():
    # Falls asleep 22:30 local Oct 1, wakes 06:30 local Oct 2.
    c = f.cycle("2026-10-02T02:30:00Z", "2026-10-03T02:00:00Z", strain=12.5)
    s = f.sleep("2026-10-02T02:30:00Z", "2026-10-02T10:30:00Z", cycle_id=c["id"])
    result = normalize(sleeps=[s], cycles=[c])
    v = values(result)
    assert v[("2026-10-02", "day_strain")] == 12.5
    assert ("2026-10-01", "day_strain") not in v
    assert state(result, "cycle", c["id"]).day_source == "linked"


def test_cycle_without_linked_sleep_gets_an_estimated_day():
    # Starts 22:30 local Oct 1; +12h lands on Oct 2.
    c = f.cycle("2026-10-02T02:30:00Z", "2026-10-03T02:00:00Z", strain=9.0)
    result = normalize(cycles=[c])
    assert values(result)[("2026-10-02", "day_strain")] == 9.0
    assert state(result, "cycle", c["id"]).day_source == "estimated"


def test_cycle_metrics_include_energy_heart_rate_and_steps():
    c = f.cycle("2026-10-02T02:30:00Z", "2026-10-03T02:00:00Z",
                kilojoule=9671.7, avg_hr=70, steps=4390)
    v = values(normalize(cycles=[c]))
    assert v[("2026-10-02", "day_kilojoules")] == 9671.7
    assert v[("2026-10-02", "day_avg_hr")] == 70
    assert v[("2026-10-02", "steps")] == 4390


def test_in_progress_cycle_metrics_are_marked_incomplete():
    c = f.cycle("2026-10-02T02:30:00Z", None, strain=1.4)
    s = f.sleep("2026-10-02T02:30:00Z", "2026-10-02T10:30:00Z", cycle_id=c["id"])
    r = f.recovery(c["id"], s["id"], score=62)
    result = normalize(sleeps=[s], cycles=[c], recoveries=[r])
    assert row(result, "2026-10-02", "day_strain").complete is False
    assert row(result, "2026-10-02", "recovery_score").complete is True
    assert row(result, "2026-10-02", "sleep_hours").complete is True


def test_recovery_lands_on_its_sleeps_wake_day_with_all_scores():
    c = f.cycle("2026-10-02T02:30:00Z", "2026-10-03T02:00:00Z")
    s = f.sleep("2026-10-02T02:30:00Z", "2026-10-02T10:30:00Z", cycle_id=c["id"])
    r = f.recovery(c["id"], s["id"], score=62, rhr=56, hrv=84.9, spo2=95.9, skin_temp=33.9)
    result = normalize(sleeps=[s], cycles=[c], recoveries=[r])
    v = values(result)
    assert v[("2026-10-02", "recovery_score")] == 62
    assert v[("2026-10-02", "resting_hr")] == 56
    assert v[("2026-10-02", "hrv_ms")] == 84.9
    assert v[("2026-10-02", "spo2_pct")] == 95.9
    assert v[("2026-10-02", "skin_temp_c")] == 33.9
    assert row(result, "2026-10-02", "recovery_score").source_ids == (str(c["id"]),)


def test_unscorable_recovery_is_recorded_without_metrics():
    c = f.cycle("2026-10-02T02:30:00Z", "2026-10-03T02:00:00Z")
    s = f.sleep("2026-10-02T02:30:00Z", "2026-10-02T10:30:00Z", cycle_id=c["id"])
    r = f.recovery(c["id"], s["id"], state="UNSCORABLE")
    result = normalize(sleeps=[s], cycles=[c], recoveries=[r])
    assert ("2026-10-02", "recovery_score") not in values(result)
    st = state(result, "recovery", c["id"])
    assert (st.day, st.score_state) == (date(2026, 10, 2), "UNSCORABLE")


def test_second_cycle_on_same_day_is_superseded_by_the_main_sleeps_cycle():
    c1 = f.cycle("2026-10-02T02:00:00Z", "2026-10-02T12:00:00Z", strain=3.0)
    c2 = f.cycle("2026-10-02T12:00:00Z", "2026-10-03T03:00:00Z", strain=11.0)
    s1 = f.sleep("2026-10-02T02:00:00Z", "2026-10-02T05:00:00Z", cycle_id=c1["id"],
                 light_ms=3_600_000, sws_ms=3_600_000, rem_ms=3_600_000)
    s2 = f.sleep("2026-10-02T12:00:00Z", "2026-10-02T20:00:00Z", cycle_id=c2["id"],
                 light_ms=10_800_000, sws_ms=7_200_000, rem_ms=7_200_000)
    r1 = f.recovery(c1["id"], s1["id"], score=20)
    r2 = f.recovery(c2["id"], s2["id"], score=70)
    result = normalize(sleeps=[s1, s2], cycles=[c1, c2], recoveries=[r1, r2])
    v = values(result)
    assert v[("2026-10-02", "day_strain")] == 11.0
    assert v[("2026-10-02", "recovery_score")] == 70
    assert state(result, "cycle", c1["id"]).role == "superseded"
    assert state(result, "recovery", c1["id"]).role == "superseded"


# --- Workouts -------------------------------------------------------------


def test_workouts_sum_per_local_start_day():
    # 21:00 local Oct 2 = 01:00 UTC Oct 3 -> still Oct 2.
    w1 = f.workout("2026-10-02T12:00:00Z", "2026-10-02T12:30:00Z", zones_min=(0, 5, 5, 10, 8, 2))
    w2 = f.workout("2026-10-03T01:00:00Z", "2026-10-03T01:45:00Z", zones_min=(0, 0, 0, 0, 3, 0))
    result = normalize(workouts=[w1, w2])
    v = values(result)
    assert v[("2026-10-02", "workout_count")] == 2
    assert v[("2026-10-02", "workout_minutes")] == pytest.approx(75.0)
    assert v[("2026-10-02", "hard_minutes")] == pytest.approx(13.0)
    assert ("2026-10-03", "workout_count") not in v


def test_unscored_workout_still_counts_as_a_workout_but_adds_no_zone_minutes():
    w = f.workout("2026-10-02T12:00:00Z", "2026-10-02T12:30:00Z", state="PENDING_SCORE")
    v = values(normalize(workouts=[w]))
    assert v[("2026-10-02", "workout_count")] == 1
    assert v[("2026-10-02", "workout_minutes")] == pytest.approx(30.0)
    assert ("2026-10-02", "hard_minutes") not in v


# --- Real-world quirk: WHOOP sends score_state SCORED with score null -------
# (seen on breathwork activities such as "increase_relaxation").


def _scoreless(record):
    record["score"] = None
    return record


def test_scored_workout_without_a_score_counts_but_adds_no_zone_minutes():
    w = _scoreless(f.workout("2026-10-02T12:00:00Z", "2026-10-02T12:10:00Z", sport="increase_relaxation"))
    v = values(normalize(workouts=[w]))
    assert v[("2026-10-02", "workout_count")] == 1
    assert ("2026-10-02", "hard_minutes") not in v


def test_scored_sleep_cycle_and_recovery_without_scores_produce_no_metrics():
    c = _scoreless(f.cycle("2026-10-02T02:30:00Z", "2026-10-03T02:00:00Z", steps=None))
    s = _scoreless(f.sleep("2026-10-02T02:30:00Z", "2026-10-02T10:30:00Z", cycle_id=c["id"]))
    r = _scoreless(f.recovery(c["id"], s["id"]))
    result = normalize(sleeps=[s], cycles=[c], recoveries=[r])
    assert result.metrics == []
    assert {st.kind for st in result.records} == {"sleep", "cycle", "recovery"}
