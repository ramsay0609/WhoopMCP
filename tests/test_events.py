from datetime import date

from whoop_engine.analytics import Analytics
from whoop_engine.normalize import MetricRow, Normalized
from whoop_engine.store import Store

D = lambda n: date(2026, 10, n)  # noqa: E731

# Recovery Oct 1-7; drinks logged on the evenings of Oct 2 and Oct 5.
RECOVERY = {1: 70, 2: 72, 3: 40, 4: 71, 5: 69, 6: 45, 7: 73}


def engine(events=((2, "alcohol"), (5, "alcohol"))):
    store = Store(":memory:")
    store.replace_derived(1, Normalized(metrics=[
        MetricRow(D(d), "recovery_score", float(v), "recovery", (f"c{d}",)) for d, v in RECOVERY.items()
    ]))
    store.set_meta(1, "covered_from", "2026-09-01")
    for d, label in events:
        store.add_event(1, D(d), label)
    return Analytics(store, 1, D(7))


def test_effect_compares_the_day_after_an_event_with_all_other_days():
    r = engine().event_effect("alcohol", "recovery_score", lag_days=1, start=D(1), end=D(7))
    assert r["with_event"] == {"days": ["2026-10-03", "2026-10-06"], "n": 2, "mean": 42.5}
    assert (r["without_event"]["n"], r["without_event"]["mean"]) == (5, 71)
    assert r["difference"] == -28.5


def test_effect_with_zero_lag_uses_the_event_day_itself():
    r = engine().event_effect("alcohol", "recovery_score", lag_days=0, start=D(1), end=D(7))
    assert r["with_event"]["mean"] == 70.5


def test_small_samples_and_causation_are_called_out():
    warnings = engine().event_effect("alcohol", "recovery_score", start=D(1), end=D(7))["warnings"]
    assert any("small sample" in w.lower() for w in warnings)
    assert any("not causation" in w for w in warnings)


def test_effect_label_matching_ignores_case_and_spacing():
    r = engine().event_effect("  Alcohol ", "recovery_score", start=D(1), end=D(7))
    assert r["with_event"]["n"] == 2


def test_unlogged_label_has_no_effect_estimate():
    r = engine().event_effect("sauna", "recovery_score", start=D(1), end=D(7))
    assert (r["with_event"]["n"], r["difference"]) == (0, None)
    assert any("No 'sauna' events" in w for w in r["warnings"])
