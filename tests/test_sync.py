from datetime import date, datetime, timedelta, timezone

import pytest

from tests import factories as f
from whoop_engine.normalize import parse_ts
from whoop_engine.store import Store
from whoop_engine.sync import sync, sync_if_stale

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


class FakeWhoop:
    """Stands in for the WHOOP API: returns records whose start is in [start, end)."""

    def __init__(self, **records):
        self.records = {k: list(records.get(k, [])) for k in ("cycle", "sleep", "recovery", "workout")}
        self.calls = []
        self.fail_on = None

    def __call__(self, kind, start, end):
        self.calls.append((kind, start, end))
        if kind == self.fail_on:
            raise RuntimeError("WHOOP 503")
        lo, hi = parse_ts(start), parse_ts(end)
        if kind == "recovery":
            starts = {c["id"]: parse_ts(c["start"]) for c in self.records["cycle"]}
            return [r for r in self.records[kind] if lo <= starts[r["cycle_id"]] < hi]
        return [r for r in self.records[kind] if lo <= parse_ts(r["start"]) < hi]


def night(day: int, *, score=60, strain=10.0, hrv=80.0):
    """A cycle + sleep + recovery waking on Oct `day` (local -04:00)."""
    c = f.cycle(f"2026-10-{day:02d}T03:00:00Z", f"2026-10-{day + 1:02d}T03:00:00Z", strain=strain)
    s = f.sleep(f"2026-10-{day:02d}T03:00:00Z", f"2026-10-{day:02d}T11:00:00Z", cycle_id=c["id"])
    r = f.recovery(c["id"], s["id"], score=score, hrv=hrv)
    return c, s, r


def fake_with(*nights, workouts=()):
    return FakeWhoop(
        cycle=[n[0] for n in nights],
        sleep=[n[1] for n in nights],
        recovery=[n[2] for n in nights],
        workout=list(workouts),
    )


def metric(store, name):
    return {r.day.day: r.value for r in store.metric_rows(1, name, date(2026, 1, 1), date(2026, 12, 31))}


def test_first_sync_backfills_the_initial_window_for_every_kind():
    store, api = Store(":memory:"), fake_with()
    sync(store, 1, api, NOW, initial_days=30)
    start = (NOW - timedelta(days=30)).isoformat()
    assert sorted(api.calls) == sorted(
        (k, start, NOW.isoformat()) for k in ("cycle", "sleep", "recovery", "workout")
    )


def test_later_sync_refetches_a_lookback_window_before_the_last_sync():
    store, api = Store(":memory:"), fake_with()
    sync(store, 1, api, NOW - timedelta(days=2))
    api.calls.clear()
    sync(store, 1, api, NOW, lookback_days=14)
    expected_start = (NOW - timedelta(days=16)).isoformat()
    assert {c[1] for c in api.calls} == {expected_start}


def test_synced_records_become_daily_metrics():
    store = Store(":memory:")
    sync(store, 1, fake_with(night(10, score=55), night(11, score=70)), NOW)
    assert metric(store, "recovery_score") == {10: 55, 11: 70}


def test_a_rescored_record_replaces_the_old_value():
    store = Store(":memory:")
    c, s, r = night(10, score=55)
    api = fake_with((c, s, r))
    sync(store, 1, api, NOW - timedelta(days=1))
    r["score"]["recovery_score"] = 61
    sync(store, 1, api, NOW)
    assert metric(store, "recovery_score") == {10: 61}


def test_records_deleted_upstream_inside_the_window_are_removed_with_their_recovery():
    store = Store(":memory:")
    keep, gone = night(10, score=55), night(11, score=70)
    api = fake_with(keep, gone)
    sync(store, 1, api, NOW - timedelta(days=1))
    for kind, rec in zip(("cycle", "sleep", "recovery"), gone):
        api.records[kind].remove(rec)
    result = sync(store, 1, api, NOW)
    assert metric(store, "recovery_score") == {10: 55}
    assert result["deleted"] == {"cycle": 1, "sleep": 1, "recovery": 1, "workout": 0}


def test_records_older_than_the_window_are_kept_even_though_not_refetched():
    store = Store(":memory:")
    old = night(1, score=40)
    api = fake_with(old)
    sync(store, 1, api, datetime(2026, 10, 2, tzinfo=timezone.utc))
    sync(store, 1, api, datetime(2026, 10, 15, tzinfo=timezone.utc), lookback_days=3)
    for kind, rec in zip(("cycle", "sleep", "recovery"), old):
        api.records[kind].remove(rec)
    sync(store, 1, api, NOW, lookback_days=3)
    assert metric(store, "recovery_score") == {1: 40}


def test_failed_fetch_raises_and_does_not_advance_the_sync_clock():
    store, api = Store(":memory:"), fake_with(night(10))
    sync(store, 1, api, NOW - timedelta(days=1))
    before = store.get_meta(1, "last_synced_at")
    api.fail_on = "recovery"
    with pytest.raises(RuntimeError):
        sync(store, 1, api, NOW)
    assert store.get_meta(1, "last_synced_at") == before


def test_covered_from_is_set_by_the_first_sync_and_kept_afterwards():
    store, api = Store(":memory:"), fake_with()
    sync(store, 1, api, NOW, initial_days=30)
    sync(store, 1, api, NOW + timedelta(days=1))
    assert store.get_meta(1, "covered_from") == "2026-09-18"


def test_sync_if_stale_skips_a_recent_sync_and_runs_an_old_one():
    store, api = Store(":memory:"), fake_with()
    sync(store, 1, api, NOW)
    api.calls.clear()
    assert sync_if_stale(store, 1, api, NOW + timedelta(minutes=30)) is None
    assert api.calls == []
    assert sync_if_stale(store, 1, api, NOW + timedelta(hours=2)) is not None
    assert len(api.calls) == 4
