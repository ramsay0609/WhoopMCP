"""Turn raw WHOOP records into one value per (day, metric).

Day rules (the "WHOOP day" a value belongs to):
  - Sleep belongs to the local date it ended (the wake day). Naps are kept
    apart from night sleep.
  - A cycle starts at sleep onset, so it takes the wake day of its linked
    non-nap sleep. Without one, its day is estimated as local(start + 12h).
  - Recovery takes the day of its cycle.
  - Workouts belong to the local date they started.
  - When several cycles land on one day, the cycle of that day's longest
    sleep wins; the others are marked "superseded".
Local dates always use the record's own timezone_offset, so travel and DST
are handled per record.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

MS_PER_HOUR = 3_600_000
MS_PER_MINUTE = 60_000


@dataclass(frozen=True)
class MetricRow:
    day: date
    metric: str
    value: float
    source_kind: str
    source_ids: tuple[str, ...]
    complete: bool = True


@dataclass(frozen=True)
class RecordState:
    kind: str
    id: str
    day: date
    score_state: str
    role: str = "main"  # main | nap | extra | superseded
    day_source: str = "linked"  # linked | estimated


@dataclass
class Normalized:
    metrics: list[MetricRow] = field(default_factory=list)
    records: list[RecordState] = field(default_factory=list)


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def local_date(ts: str, offset: str, shift: timedelta = timedelta(0)) -> date:
    sign = -1 if offset.startswith("-") else 1
    hours, minutes = (int(p) for p in offset.lstrip("+-").split(":"))
    tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return (parse_ts(ts) + shift).astimezone(tz).date()


def _scored(r: dict) -> bool:
    # WHOOP can say SCORED and still send score: null (e.g. breathwork activities).
    return r["score_state"] == "SCORED" and r.get("score") is not None


def _state(r: dict) -> str:
    return "NO_SCORE" if r["score_state"] == "SCORED" and r.get("score") is None else r["score_state"]


def _asleep_ms(s: dict) -> int:
    st = s["score"]["stage_summary"]
    return (
        st["total_light_sleep_time_milli"]
        + st["total_slow_wave_sleep_time_milli"]
        + st["total_rem_sleep_time_milli"]
    )


def _duration_ms(r: dict) -> float:
    return (parse_ts(r["end"]) - parse_ts(r["start"])).total_seconds() * 1000


def normalize(sleeps=(), cycles=(), recoveries=(), workouts=()) -> Normalized:
    out = Normalized()
    main_cycle_by_day = _sleeps(sleeps, out)
    cycle_day = _cycles(cycles, sleeps, main_cycle_by_day, out)
    _recoveries(recoveries, cycle_day, out)
    _workouts(workouts, out)
    return out


def _sleeps(sleeps, out: Normalized) -> dict[date, str]:
    """Emit sleep metrics; return {day: cycle_id of that day's longest sleep}."""
    nights: dict[date, list[dict]] = defaultdict(list)
    naps: dict[date, list[dict]] = defaultdict(list)
    for s in sleeps:
        day = local_date(s["end"], s["timezone_offset"])
        scored = _scored(s)
        if s["nap"]:
            out.records.append(RecordState("sleep", s["id"], day, _state(s), "nap"))
            if scored:
                naps[day].append(s)
            continue
        if scored:
            nights[day].append(s)
        else:
            out.records.append(RecordState("sleep", s["id"], day, _state(s)))

    main_cycle_by_day: dict[date, str] = {}
    for day, group in nights.items():
        main = max(group, key=_asleep_ms)
        main_cycle_by_day[day] = str(main["cycle_id"])
        for s in group:
            role = "main" if s is main else "extra"
            out.records.append(RecordState("sleep", s["id"], day, s["score_state"], role))
        ids = tuple(s["id"] for s in group)
        hours = sum(_asleep_ms(s) for s in group) / MS_PER_HOUR
        out.metrics.append(MetricRow(day, "sleep_hours", hours, "sleep", ids))
        sc = main["score"]
        for metric, value in (
            ("sleep_performance_pct", sc["sleep_performance_percentage"]),
            ("sleep_efficiency_pct", sc["sleep_efficiency_percentage"]),
            ("sleep_consistency_pct", sc["sleep_consistency_percentage"]),
            ("respiratory_rate", sc["respiratory_rate"]),
        ):
            if value is not None:
                out.metrics.append(MetricRow(day, metric, value, "sleep", (main["id"],)))

    for day, group in naps.items():
        hours = sum(_asleep_ms(s) for s in group) / MS_PER_HOUR
        ids = tuple(s["id"] for s in group)
        out.metrics.append(MetricRow(day, "nap_hours", hours, "sleep", ids))
    return main_cycle_by_day


def _cycles(cycles, sleeps, main_cycle_by_day, out: Normalized) -> dict[str, tuple[date, str]]:
    """Emit cycle metrics; return {cycle_id: (day, role)} for recovery lookup."""
    wake_day_by_cycle = {
        str(s["cycle_id"]): local_date(s["end"], s["timezone_offset"])
        for s in sleeps
        if not s["nap"]
    }
    placed: dict[str, tuple[date, str, str]] = {}  # id -> (day, day_source, role)
    by_day: dict[date, list[dict]] = defaultdict(list)
    for c in cycles:
        cid = str(c["id"])
        if cid in wake_day_by_cycle:
            day, source = wake_day_by_cycle[cid], "linked"
        else:
            day = local_date(c["start"], c["timezone_offset"], timedelta(hours=12))
            source = "estimated"
        placed[cid] = (day, source, "main")
        by_day[day].append(c)

    for day, group in by_day.items():
        if len(group) == 1:
            continue
        winner = main_cycle_by_day.get(day)
        if winner not in {str(c["id"]) for c in group}:
            winner = str(max(group, key=lambda c: parse_ts(c["start"]))["id"])
        for c in group:
            cid = str(c["id"])
            if cid != winner:
                d, src, _ = placed[cid]
                placed[cid] = (d, src, "superseded")

    for c in cycles:
        cid = str(c["id"])
        day, source, role = placed[cid]
        out.records.append(RecordState("cycle", cid, day, _state(c), role, source))
        if role != "main":
            continue
        complete = c["end"] is not None
        values = []
        if _scored(c):
            sc = c["score"]
            values += [
                ("day_strain", sc["strain"]),
                ("day_kilojoules", sc["kilojoule"]),
                ("day_avg_hr", sc["average_heart_rate"]),
            ]
        values.append(("steps", c.get("step_count")))
        for metric, value in values:
            if value is not None:
                out.metrics.append(MetricRow(day, metric, value, "cycle", (cid,), complete))
    return {cid: (day, role) for cid, (day, _, role) in placed.items()}


def _recoveries(recoveries, cycle_day, out: Normalized) -> None:
    for r in recoveries:
        cid = str(r["cycle_id"])
        if cid not in cycle_day:
            continue  # its cycle is outside the synced range; nothing to anchor it to
        day, role = cycle_day[cid]
        out.records.append(RecordState("recovery", cid, day, _state(r), role))
        if role != "main" or not _scored(r):
            continue
        sc = r["score"]
        for metric, value in (
            ("recovery_score", sc["recovery_score"]),
            ("hrv_ms", sc["hrv_rmssd_milli"]),
            ("resting_hr", sc["resting_heart_rate"]),
            ("spo2_pct", sc.get("spo2_percentage")),
            ("skin_temp_c", sc.get("skin_temp_celsius")),
        ):
            if value is not None:
                out.metrics.append(MetricRow(day, metric, value, "recovery", (cid,)))


def _workouts(workouts, out: Normalized) -> None:
    by_day: dict[date, list[dict]] = defaultdict(list)
    for w in workouts:
        day = local_date(w["start"], w["timezone_offset"])
        out.records.append(RecordState("workout", w["id"], day, _state(w)))
        by_day[day].append(w)
    for day, group in by_day.items():
        ids = tuple(w["id"] for w in group)
        out.metrics.append(MetricRow(day, "workout_count", len(group), "workout", ids))
        minutes = sum(_duration_ms(w) for w in group) / MS_PER_MINUTE
        out.metrics.append(MetricRow(day, "workout_minutes", minutes, "workout", ids))
        scored = [w for w in group if _scored(w) and w["score"].get("zone_durations")]
        if scored:
            hard = sum(
                w["score"]["zone_durations"]["zone_four_milli"]
                + w["score"]["zone_durations"]["zone_five_milli"]
                for w in scored
            ) / MS_PER_MINUTE
            out.metrics.append(
                MetricRow(day, "hard_minutes", hard, "workout", tuple(w["id"] for w in scored))
            )
