"""Catalog of daily metrics the engine can answer questions about.

`zero_fill` metrics count activity: a synced day with no record means 0, not
missing. `event:<label>` metrics are built from user-logged life events.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    source: str  # sleep | cycle | recovery | workout | event
    description: str
    zero_fill: bool = False


_CATALOG = [
    Metric("recovery_score", "%", "recovery", "WHOOP recovery score"),
    Metric("hrv_ms", "ms", "recovery", "Heart rate variability (RMSSD) during sleep"),
    Metric("resting_hr", "bpm", "recovery", "Resting heart rate"),
    Metric("spo2_pct", "%", "recovery", "Blood oxygen saturation"),
    Metric("skin_temp_c", "°C", "recovery", "Skin temperature"),
    Metric("sleep_hours", "h", "sleep", "Time asleep (light + deep + REM) in main sleep(s), by wake day"),
    Metric("nap_hours", "h", "sleep", "Time asleep in naps", zero_fill=True),
    Metric("sleep_performance_pct", "%", "sleep", "Sleep obtained vs. sleep needed"),
    Metric("sleep_efficiency_pct", "%", "sleep", "Time asleep vs. time in bed"),
    Metric("sleep_consistency_pct", "%", "sleep", "Consistency of sleep/wake timing"),
    Metric("respiratory_rate", "breaths/min", "sleep", "Respiratory rate during sleep"),
    Metric("day_strain", "strain (0-21)", "cycle", "WHOOP day strain"),
    Metric("day_kilojoules", "kJ", "cycle", "Energy expenditure for the day"),
    Metric("day_avg_hr", "bpm", "cycle", "Average heart rate for the day"),
    Metric("steps", "steps", "cycle", "Step count for the day"),
    Metric("workout_count", "workouts", "workout", "Number of workouts", zero_fill=True),
    Metric("workout_minutes", "min", "workout", "Total workout duration", zero_fill=True),
    Metric("hard_minutes", "min", "workout", "Minutes in HR zones 4-5 across workouts", zero_fill=True),
]

METRICS = {m.name: m for m in _CATALOG}
EVENT_PREFIX = "event:"


def normalize_label(label: str) -> str:
    label = " ".join(label.strip().lower().split())
    if not label:
        raise ValueError("Event label must not be empty")
    return label


def get_metric(name: str) -> Metric:
    if name.startswith(EVENT_PREFIX):
        label = normalize_label(name[len(EVENT_PREFIX):])
        return Metric(EVENT_PREFIX + label, "count", "event",
                      f"Logged '{label}' events per day", zero_fill=True)
    if name not in METRICS:
        raise ValueError(
            f"Unknown metric '{name}'. Known: {', '.join(METRICS)}, or event:<label>"
        )
    return METRICS[name]
