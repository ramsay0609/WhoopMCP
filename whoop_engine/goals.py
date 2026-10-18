"""Goals on any metric, evaluated per calendar week (Mon-Sun) or month.

Kinds:
  days     value `op` target on at least `min_days` days of the period
  total    sum over the period `op` target    (e.g. workouts >= 4, alcohol <= 2)
  average  mean over the period `op` target   (e.g. HRV >= 60)

Statuses: achieved | missed | on_track | behind | no_data. "on_track" and
"behind" judge the pace so far; achieved/missed are final.
"""

import calendar
import operator
from datetime import date, timedelta

from whoop_engine.metrics import get_metric

KINDS = ("days", "total", "average")
OPS = {">=": operator.ge, "<=": operator.le}
PERIOD_MAX_DAYS = {"week": 7, "month": 31}


def validate_goal(spec: dict) -> dict:
    metric = get_metric(spec.get("metric", "")).name
    kind, op, period = spec.get("kind"), spec.get("op"), spec.get("period")
    if kind not in KINDS:
        raise ValueError(f"Goal kind must be one of {KINDS}")
    if op not in OPS:
        raise ValueError(f"Goal op must be one of {tuple(OPS)}")
    if period not in PERIOD_MAX_DAYS:
        raise ValueError(f"Goal period must be one of {tuple(PERIOD_MAX_DAYS)}")
    try:
        target = float(spec.get("target"))
    except (TypeError, ValueError):
        raise ValueError("Goal target must be a number") from None
    min_days = None
    if kind == "days":
        min_days = spec.get("min_days")
        if not isinstance(min_days, int) or not 1 <= min_days <= PERIOD_MAX_DAYS[period]:
            raise ValueError(f"min_days must be 1-{PERIOD_MAX_DAYS[period]} for a {period}")
    return {"metric": metric, "kind": kind, "op": op, "target": target,
            "period": period, "min_days": min_days}


def period_bounds(period: str, day: date) -> tuple[date, date]:
    if period == "week":
        start = day - timedelta(day.weekday())
        return start, start + timedelta(6)
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


def goal_progress(analytics, goal: dict, as_of: date | None = None) -> dict:
    goal = {**goal, **validate_goal(goal)}
    as_of = as_of or analytics.today
    start, end = period_bounds(goal["period"], as_of)
    as_of = min(as_of, end)
    period_days = (end - start).days + 1
    elapsed = (as_of - start).days + 1
    remaining = period_days - elapsed
    over = remaining == 0
    meets = OPS[goal["op"]]
    target = goal["target"]
    projected = None

    if goal["kind"] == "days":
        evidence = analytics.query_metric(goal["metric"], start, as_of, "series")
        current = sum(meets(p["value"], target) for p in evidence["series"])
        need = goal["min_days"]
        if current >= need:
            status = "achieved"
        elif current + remaining < need:
            status = "missed"
        else:
            status = "on_track" if current >= need * elapsed / period_days else "behind"
    elif goal["kind"] == "total":
        evidence = analytics.query_metric(goal["metric"], start, as_of, "sum")
        current = evidence["value"] or 0
        projected = round(current / elapsed * period_days, 2)
        if goal["op"] == ">=":
            if current >= target:
                status = "achieved"
            elif over:
                status = "missed"
            else:
                status = "on_track" if projected >= target else "behind"
        else:
            if current > target:
                status = "missed"
            elif over:
                status = "achieved"
            else:
                status = "on_track" if projected <= target else "behind"
    else:
        evidence = analytics.query_metric(goal["metric"], start, as_of, "mean")
        current = evidence["value"]
        if current is None:
            status = "no_data"
        elif over:
            status = "achieved" if meets(current, target) else "missed"
        else:
            status = "on_track" if meets(current, target) else "behind"

    return {
        "goal": goal,
        "period": {"start": str(start), "end": str(end)},
        "as_of": str(as_of),
        "days_elapsed": elapsed,
        "days_remaining": remaining,
        "current": current,
        "projected": projected,
        "status": status,
        "method": "goal_progress/v1",
        "evidence": evidence,
    }


def goal_history(analytics, goal: dict, periods: int = 4, as_of: date | None = None) -> dict:
    """Closed periods before the current one, newest first, plus the current streak."""
    as_of = as_of or analytics.today
    current_start, _ = period_bounds(goal["period"], as_of)
    results = []
    day = current_start - timedelta(1)
    for _ in range(periods):
        start, end = period_bounds(goal["period"], day)
        p = goal_progress(analytics, goal, as_of=end)
        results.append({"period": p["period"], "current": p["current"],
                        "met": p["status"] == "achieved", "warnings": p["evidence"]["warnings"]})
        day = start - timedelta(1)
    streak = 0
    for r in results:
        if not r["met"]:
            break
        streak += 1
    return {"goal": goal, "periods": results, "streak": streak, "method": "goal_history/v1"}
