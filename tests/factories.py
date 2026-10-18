"""Builders for synthetic WHOOP v2 records.

Shapes mirror the real API responses field-for-field; values are made up.
"""

import itertools

_ids = itertools.count(1000)


def sleep(
    start,
    end,
    *,
    id=None,
    cycle_id=None,
    nap=False,
    tz="-04:00",
    state="SCORED",
    light_ms=14_000_000,
    sws_ms=4_000_000,
    rem_ms=6_000_000,
    awake_ms=2_000_000,
    performance=80,
    efficiency=90.0,
    consistency=60,
    respiratory_rate=15.0,
    updated_at="2026-10-01T00:00:00.000Z",
):
    score = None
    if state == "SCORED":
        score = {
            "stage_summary": {
                "total_in_bed_time_milli": light_ms + sws_ms + rem_ms + awake_ms,
                "total_awake_time_milli": awake_ms,
                "total_no_data_time_milli": 0,
                "total_light_sleep_time_milli": light_ms,
                "total_slow_wave_sleep_time_milli": sws_ms,
                "total_rem_sleep_time_milli": rem_ms,
                "sleep_cycle_count": 4,
                "disturbance_count": 10,
            },
            "sleep_needed": {
                "baseline_milli": 27_000_000,
                "need_from_sleep_debt_milli": 0,
                "need_from_recent_strain_milli": 0,
                "need_from_recent_nap_milli": 0,
            },
            "respiratory_rate": respiratory_rate,
            "sleep_performance_percentage": performance,
            "sleep_consistency_percentage": consistency,
            "sleep_efficiency_percentage": efficiency,
        }
    return {
        "id": id or f"sleep-{next(_ids)}",
        "cycle_id": cycle_id if cycle_id is not None else next(_ids),
        "v1_id": None,
        "user_id": 1,
        "created_at": updated_at,
        "updated_at": updated_at,
        "start": start,
        "end": end,
        "timezone_offset": tz,
        "nap": nap,
        "score_state": state,
        "score": score,
    }


def cycle(
    start,
    end,
    *,
    id=None,
    tz="-04:00",
    state="SCORED",
    strain=10.0,
    kilojoule=9000.0,
    avg_hr=65,
    max_hr=150,
    steps=8000,
    updated_at="2026-10-01T00:00:00.000Z",
):
    score = None
    if state == "SCORED":
        score = {
            "strain": strain,
            "kilojoule": kilojoule,
            "average_heart_rate": avg_hr,
            "max_heart_rate": max_hr,
        }
    return {
        "id": id if id is not None else next(_ids),
        "user_id": 1,
        "created_at": updated_at,
        "updated_at": updated_at,
        "start": start,
        "end": end,
        "timezone_offset": tz,
        "score_state": state,
        "score": score,
        "step_count": steps,
    }


def recovery(
    cycle_id,
    sleep_id,
    *,
    state="SCORED",
    score=60,
    rhr=55,
    hrv=80.0,
    spo2=96.0,
    skin_temp=33.8,
    calibrating=False,
    updated_at="2026-10-01T00:00:00.000Z",
):
    body = None
    if state == "SCORED":
        body = {
            "user_calibrating": calibrating,
            "recovery_score": score,
            "resting_heart_rate": rhr,
            "hrv_rmssd_milli": hrv,
            "spo2_percentage": spo2,
            "skin_temp_celsius": skin_temp,
        }
    return {
        "cycle_id": cycle_id,
        "sleep_id": sleep_id,
        "user_id": 1,
        "created_at": updated_at,
        "updated_at": updated_at,
        "score_state": state,
        "score": body,
    }


def workout(
    start,
    end,
    *,
    id=None,
    tz="-04:00",
    state="SCORED",
    sport="running",
    strain=12.0,
    zones_min=(0, 5, 10, 10, 5, 0),
    updated_at="2026-10-01T00:00:00.000Z",
):
    score = None
    if state == "SCORED":
        names = ["zero", "one", "two", "three", "four", "five"]
        score = {
            "strain": strain,
            "average_heart_rate": 140,
            "max_heart_rate": 175,
            "kilojoule": 1500.0,
            "percent_recorded": 1,
            "distance_meter": 5000.0,
            "altitude_gain_meter": 20.0,
            "altitude_change_meter": 0.0,
            "zone_durations": {
                f"zone_{n}_milli": m * 60_000 for n, m in zip(names, zones_min)
            },
        }
    return {
        "id": id or f"workout-{next(_ids)}",
        "v1_id": None,
        "user_id": 1,
        "created_at": updated_at,
        "updated_at": updated_at,
        "start": start,
        "end": end,
        "timezone_offset": tz,
        "sport_name": sport,
        "score_state": state,
        "score": score,
        "sport_id": 0,
    }
