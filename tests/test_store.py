import stat
from datetime import date

from whoop_engine.normalize import MetricRow, Normalized, RecordState
from whoop_engine.store import Store


def test_upserting_a_record_with_the_same_id_replaces_it():
    store = Store(":memory:")
    store.upsert_raw(1, "sleep", [{"id": "a", "v": 1}, {"id": "b", "v": 1}])
    store.upsert_raw(1, "sleep", [{"id": "a", "v": 2}])
    assert sorted(store.raw(1, "sleep"), key=lambda r: r["id"]) == [
        {"id": "a", "v": 2},
        {"id": "b", "v": 1},
    ]


def test_recovery_records_are_keyed_by_cycle_id():
    store = Store(":memory:")
    store.upsert_raw(1, "recovery", [{"cycle_id": 7, "score": 1}])
    store.upsert_raw(1, "recovery", [{"cycle_id": 7, "score": 2}])
    assert store.raw(1, "recovery") == [{"cycle_id": 7, "score": 2}]


def test_deleted_raw_records_are_gone():
    store = Store(":memory:")
    store.upsert_raw(1, "cycle", [{"id": 1}, {"id": 2}])
    store.delete_raw(1, "cycle", ["1"])
    assert store.raw(1, "cycle") == [{"id": 2}]


def test_users_never_see_each_others_data():
    store = Store(":memory:")
    store.upsert_raw(1, "sleep", [{"id": "a"}])
    store.replace_derived(1, Normalized(
        metrics=[MetricRow(date(2026, 10, 2), "hrv_ms", 80.0, "recovery", ("9",))],
        records=[RecordState("recovery", "9", date(2026, 10, 2), "SCORED")],
    ))
    store.set_meta(1, "last_synced_at", "x")
    store.add_event(1, date(2026, 10, 2), "alcohol")
    store.add_goal(1, {"metric": "sleep_hours"})
    assert store.raw(2, "sleep") == []
    assert store.metric_rows(2, "hrv_ms", date(2026, 1, 1), date(2026, 12, 31)) == []
    assert store.record_states(2, date(2026, 1, 1), date(2026, 12, 31)) == []
    assert store.get_meta(2, "last_synced_at") is None
    assert store.events(2, date(2026, 1, 1), date(2026, 12, 31)) == []
    assert store.goals(2) == []


def test_replace_derived_drops_rows_from_the_previous_build():
    store = Store(":memory:")
    d = date(2026, 10, 2)
    store.replace_derived(1, Normalized(metrics=[MetricRow(d, "hrv_ms", 80.0, "recovery", ("9",))]))
    store.replace_derived(1, Normalized(metrics=[MetricRow(d, "hrv_ms", 70.0, "recovery", ("10",), False)]))
    assert store.metric_rows(1, "hrv_ms", d, d) == [
        MetricRow(d, "hrv_ms", 70.0, "recovery", ("10",), False)
    ]


def test_metric_rows_respect_an_inclusive_window_and_come_back_in_day_order():
    store = Store(":memory:")
    rows = [MetricRow(date(2026, 10, n), "hrv_ms", float(n), "recovery", (str(n),)) for n in (5, 1, 3, 9)]
    store.replace_derived(1, Normalized(metrics=rows))
    got = store.metric_rows(1, "hrv_ms", date(2026, 10, 1), date(2026, 10, 5))
    assert [r.day.day for r in got] == [1, 3, 5]


def test_record_states_round_trip_within_window():
    store = Store(":memory:")
    s = RecordState("cycle", "5", date(2026, 10, 2), "PENDING_SCORE", "superseded", "estimated")
    store.replace_derived(1, Normalized(records=[s]))
    assert store.record_states(1, date(2026, 10, 2), date(2026, 10, 2)) == [s]
    assert store.record_states(1, date(2026, 10, 3), date(2026, 10, 9)) == []


def test_events_filter_by_label_and_can_be_deleted():
    store = Store(":memory:")
    a = store.add_event(1, date(2026, 10, 2), "alcohol", value=2, note="wine")
    store.add_event(1, date(2026, 10, 3), "travel")
    got = store.events(1, date(2026, 10, 1), date(2026, 10, 31), label="alcohol")
    assert [(e["id"], e["day"], e["label"], e["value"], e["note"]) for e in got] == [
        (a, "2026-10-02", "alcohol", 2, "wine")
    ]
    assert store.delete_event(1, a) is True
    assert store.delete_event(1, a) is False
    assert store.events(1, date(2026, 10, 1), date(2026, 10, 31), label="alcohol") == []


def test_another_user_cannot_delete_my_event():
    store = Store(":memory:")
    a = store.add_event(1, date(2026, 10, 2), "alcohol")
    assert store.delete_event(2, a) is False
    assert len(store.events(1, date(2026, 10, 1), date(2026, 10, 31))) == 1


def test_archived_goals_are_hidden_by_default():
    store = Store(":memory:")
    g1 = store.add_goal(1, {"metric": "sleep_hours", "target": 7.5})
    g2 = store.add_goal(1, {"metric": "hrv_ms", "target": 60})
    assert store.archive_goal(1, g1) is True
    assert [g["id"] for g in store.goals(1)] == [g2]
    assert store.goals(1)[0]["target"] == 60
    assert {g["id"] for g in store.goals(1, include_archived=True)} == {g1, g2}


def test_database_file_is_private_to_the_owner(tmp_path):
    path = tmp_path / "whoop.db"
    Store(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
