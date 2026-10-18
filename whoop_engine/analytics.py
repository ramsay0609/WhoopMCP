"""Deterministic analytics over the local store.

Every result carries its own evidence: the window, how many days were
expected vs. observed, which days are missing, the source record ids, the
method version, when data was last synced, and warnings about anything that
weakens the answer. The assistant explains the numbers; it never has to
compute them.
"""

import statistics
from datetime import date, timedelta

from whoop_engine.metrics import EVENT_PREFIX, get_metric, normalize_label
from whoop_engine.normalize import MetricRow

AGGREGATIONS = ("mean", "median", "min", "max", "sum", "count", "stdev", "series")
LOW_COVERAGE = 0.7
PROVISIONAL_BASELINE_DAYS = 14
LATEST_LOOKBACK_DAYS = 7
DEFAULT_EFFECT_DAYS = 90
SMALL_SAMPLE = 5
CLAIM_TOLERANCE = 0.02  # relative; absorbs rounding in the assistant's wording


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


def _round(x):
    return None if x is None else round(x, 2)


def _aggregate(values: list[float], aggregation: str):
    if not values:
        return None
    if aggregation == "stdev":
        return statistics.stdev(values) if len(values) > 1 else None
    return {
        "mean": statistics.fmean,
        "median": statistics.median,
        "min": min,
        "max": max,
        "sum": sum,
        "count": len,
    }[aggregation](values)


class Analytics:
    def __init__(self, store, user_id: int, today: date):
        self.store = store
        self.user_id = user_id
        self.today = today

    # --- data access ------------------------------------------------------

    def _rows(self, metric, start: date, end: date) -> list[MetricRow]:
        if metric.source != "event":
            return self.store.metric_rows(self.user_id, metric.name, start, end)
        label = metric.name[len(EVENT_PREFIX):]
        totals: dict[str, list] = {}
        for e in self.store.events(self.user_id, start, end, label=label):
            day_total = totals.setdefault(e["day"], [0.0, []])
            day_total[0] += e["value"]
            day_total[1].append(f"event-{e['id']}")
        return [
            MetricRow(date.fromisoformat(d), metric.name, v, "event", tuple(ids))
            for d, (v, ids) in sorted(totals.items())
        ]

    def _covered_from(self) -> date | None:
        value = self.store.get_meta(self.user_id, "covered_from")
        return date.fromisoformat(value) if value else None

    # --- core query -------------------------------------------------------

    def query_metric(self, metric: str, start: date, end: date, aggregation: str = "mean",
                     include_incomplete: bool = False) -> dict:
        m = get_metric(metric)
        if aggregation not in AGGREGATIONS:
            raise ValueError(f"Unknown aggregation '{aggregation}'. Use one of {AGGREGATIONS}")
        if start > end:
            raise ValueError(f"start {start} is after end {end}")

        warnings: list[str] = []
        last_day = min(end, self.today)
        covered = self._covered_from()
        first_day = start
        if m.source != "event":  # events are user-logged; they have no sync horizon
            if covered is None:
                first_day = last_day + timedelta(1)  # nothing synced yet
                warnings.append("No WHOOP data has been synced yet")
            elif covered > start:
                first_day = covered
                warnings.append(f"Synced history starts {covered}; earlier days are not counted")
        expected = _days(first_day, last_day) if first_day <= last_day else []

        rows = {r.day: r for r in self._rows(m, start, end)}
        in_progress = sorted(d for d, r in rows.items() if not r.complete)
        if in_progress and not include_incomplete:
            for d in in_progress:
                del rows[d]
            warnings.append(
                "Excluded value(s) still in progress for " + ", ".join(map(str, in_progress))
                + " (the day is not over yet)"
            )

        points: list[tuple[date, float, tuple[str, ...]]] = []
        missing: list[date] = []
        for d in expected:
            if d in rows:
                points.append((d, rows[d].value, rows[d].source_ids))
            elif m.zero_fill:
                points.append((d, 0.0, ()))
            else:
                missing.append(d)

        if not points:
            warnings.append("No data for this metric in the window")
        elif len(points) < LOW_COVERAGE * len(expected):
            warnings.append(f"Only {len(points)} of {len(expected)} expected days have data")
        if m.source == "event":
            warnings.append("Days with nothing logged count as 0")

        unscored = [
            s for s in self.store.record_states(self.user_id, start, end)
            if s.kind == m.source and s.score_state != "SCORED" and s.role != "superseded"
        ]
        if unscored:
            days = sorted({str(s.day) for s in unscored})
            warnings.append(
                f"{len(unscored)} {m.source} record(s) not scored (pending, unscorable, or no score from WHOOP) on "
                + ", ".join(days)
            )

        result = {
            "metric": m.name,
            "unit": m.unit,
            "window": {"start": str(start), "end": str(end)},
            "aggregation": aggregation,
            "value": None,
            "observed_days": len(points),
            "expected_days": len(expected),
            "missing_days": [str(d) for d in missing],
            "method": "query_metric/v1",
            "source_ids": [i for _, _, ids in points for i in ids],
            "last_synced_at": self.store.get_meta(self.user_id, "last_synced_at"),
            "warnings": warnings,
        }
        if aggregation == "series":
            result["series"] = [
                {"day": str(d), "value": _round(v), "source_ids": list(ids)} for d, v, ids in points
            ]
        else:
            result["value"] = _round(_aggregate([v for _, v, _ in points], aggregation))
        return result

    # --- derived questions ------------------------------------------------

    def baseline(self, metric: str, days: int = 30, as_of: date | None = None) -> dict:
        """Where does the latest value sit against the `days` before it?"""
        as_of = as_of or self.today
        recent = self.query_metric(
            metric, as_of - timedelta(LATEST_LOOKBACK_DAYS - 1), as_of, "series"
        )["series"]
        latest = {"day": recent[-1]["day"], "value": recent[-1]["value"]} if recent else None
        anchor = date.fromisoformat(latest["day"]) if latest else as_of + timedelta(1)
        window = self.query_metric(metric, anchor - timedelta(days), anchor - timedelta(1), "series")
        values = [p["value"] for p in window["series"]]

        warnings = list(window["warnings"])
        if latest is None:
            warnings.append(f"No value in the last {LATEST_LOOKBACK_DAYS} days to compare")
        if len(values) < PROVISIONAL_BASELINE_DAYS:
            warnings.append(f"Baseline has only {len(values)} days; treat it as provisional")

        stdev = statistics.stdev(values) if len(values) > 1 else None
        mean = statistics.fmean(values) if values else None
        z = pct = None
        if latest and values:
            x = latest["value"]
            if stdev:
                z = (x - mean) / stdev
            below = sum(v < x for v in values)
            equal = sum(v == x for v in values)
            pct = 100 * (below + 0.5 * equal) / len(values)
        return {
            "metric": window["metric"],
            "unit": window["unit"],
            "latest": latest,
            "baseline": {
                "window": window["window"],
                "days_observed": len(values),
                "mean": _round(mean),
                "median": _round(statistics.median(values)) if values else None,
                "stdev": _round(stdev),
                "min": _round(min(values)) if values else None,
                "max": _round(max(values)) if values else None,
            },
            "z_score": _round(z),
            "percentile_rank": _round(pct),
            "method": "baseline/v1",
            "source_ids": window["source_ids"],
            "last_synced_at": window["last_synced_at"],
            "warnings": warnings,
        }

    def compare_periods(self, metric: str, first: tuple[date, date], second: tuple[date, date],
                        aggregation: str = "mean") -> dict:
        a = self.query_metric(metric, *first, aggregation)
        b = self.query_metric(metric, *second, aggregation)
        diff = pct = None
        if a["value"] is not None and b["value"] is not None:
            diff = b["value"] - a["value"]
            pct = 100 * diff / a["value"] if a["value"] else None
        return {
            "metric": a["metric"],
            "unit": a["unit"],
            "first": a,
            "second": b,
            "difference": _round(diff),
            "pct_change": _round(pct),
            "method": "compare_periods/v1",
        }

    def event_effect(self, label: str, metric: str, lag_days: int = 1,
                     start: date | None = None, end: date | None = None) -> dict:
        """Mean of `metric` on days `lag_days` after a logged event vs. all other days.

        lag_days=1 suits evening behaviours: a drink on Oct 2 shows up in the
        recovery of the Oct 3 wake day.
        """
        label = normalize_label(label)
        end = end or self.today
        start = start or end - timedelta(DEFAULT_EFFECT_DAYS - 1)
        evidence = self.query_metric(metric, start, end, "series")
        event_days = {
            date.fromisoformat(e["day"]) + timedelta(lag_days)
            for e in self.store.events(self.user_id, start - timedelta(lag_days), end, label=label)
        }
        with_event = [p for p in evidence["series"] if date.fromisoformat(p["day"]) in event_days]
        without = [p for p in evidence["series"] if date.fromisoformat(p["day"]) not in event_days]

        def group(points):
            values = [p["value"] for p in points]
            return {"days": [p["day"] for p in points], "n": len(values),
                    "mean": _round(statistics.fmean(values)) if values else None}

        a, b = group(with_event), group(without)
        diff = a["mean"] - b["mean"] if a["n"] and b["n"] else None
        warnings = list(evidence["warnings"])
        if not event_days:
            warnings.append(f"No '{label}' events logged in this window")
        elif min(a["n"], b["n"]) < SMALL_SAMPLE:
            warnings.append(f"Small sample (fewer than {SMALL_SAMPLE} days in a group); "
                            "the difference may be noise")
        warnings.append("This is an association, not causation: other factors may differ "
                        "on these days")
        return {
            "label": label,
            "metric": evidence["metric"],
            "unit": evidence["unit"],
            "lag_days": lag_days,
            "window": evidence["window"],
            "with_event": {"days": a["days"], "n": a["n"], "mean": a["mean"]},
            "without_event": {"n": b["n"], "mean": b["mean"]},
            "difference": _round(diff),
            "method": "event_effect/v1",
            "warnings": warnings,
        }

    def verify_claim(self, metric: str, start: date, end: date, aggregation: str,
                     claimed_value: float, tolerance: float | None = None) -> dict:
        evidence = self.query_metric(metric, start, end, aggregation)
        actual = evidence["value"]
        if actual is None:
            verdict, diff, tol = "cannot_verify", None, tolerance
        else:
            tol = tolerance if tolerance is not None else max(abs(actual) * CLAIM_TOLERANCE, 0.01)
            diff = claimed_value - actual
            verdict = "matches" if abs(diff) <= tol else "does_not_match"
        return {
            "claimed": claimed_value,
            "actual": actual,
            "difference": _round(diff),
            "tolerance": _round(tol),
            "verdict": verdict,
            "method": "verify_claim/v1",
            "evidence": evidence,
        }
