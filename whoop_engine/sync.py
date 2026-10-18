"""Pull WHOOP records into the local store and rebuild daily metrics.

Each sync refetches from `lookback_days` before the previous sync, because
WHOOP rescores and deletes recent records. Inside that window the API is the
source of truth: anything stored but no longer returned is deleted. Records
older than the window are left alone.

All kinds are fetched before anything is written, so a failed request leaves
the store and the sync clock unchanged.
"""

from datetime import datetime, timedelta

from whoop_engine.normalize import normalize, parse_ts

KINDS = ("cycle", "sleep", "recovery", "workout")


def _id(kind: str, record: dict) -> str:
    return str(record["cycle_id"] if kind == "recovery" else record["id"])


def sync(store, user_id: int, fetch, now: datetime, initial_days: int = 180,
         lookback_days: int = 14) -> dict:
    last = store.get_meta(user_id, "last_synced_at")
    if last is None:
        start = now - timedelta(days=initial_days)
    else:
        start = datetime.fromisoformat(last) - timedelta(days=lookback_days)

    fetched = {kind: fetch(kind, start.isoformat(), now.isoformat()) for kind in KINDS}

    def in_window(ts: str) -> bool:
        return start <= parse_ts(ts) < now

    window_cycles = {str(c["id"]) for c in store.raw(user_id, "cycle") if in_window(c["start"])}
    deleted = {}
    for kind in KINDS:
        returned = {_id(kind, r) for r in fetched[kind]}
        stale = [
            _id(kind, r)
            for r in store.raw(user_id, kind)
            if _id(kind, r) not in returned
            and (str(r["cycle_id"]) in window_cycles if kind == "recovery" else in_window(r["start"]))
        ]
        store.delete_raw(user_id, kind, stale)
        store.upsert_raw(user_id, kind, fetched[kind])
        deleted[kind] = len(stale)

    store.replace_derived(
        user_id,
        normalize(
            sleeps=store.raw(user_id, "sleep"),
            cycles=store.raw(user_id, "cycle"),
            recoveries=store.raw(user_id, "recovery"),
            workouts=store.raw(user_id, "workout"),
        ),
    )
    if store.get_meta(user_id, "covered_from") is None:
        store.set_meta(user_id, "covered_from", start.date().isoformat())
    store.set_meta(user_id, "last_synced_at", now.isoformat())
    return {
        "window": {"start": start.isoformat(), "end": now.isoformat()},
        "fetched": {k: len(v) for k, v in fetched.items()},
        "deleted": deleted,
        "last_synced_at": now.isoformat(),
    }


def sync_if_stale(store, user_id: int, fetch, now: datetime,
                  max_age: timedelta = timedelta(hours=1)) -> dict | None:
    last = store.get_meta(user_id, "last_synced_at")
    if last is not None and now - datetime.fromisoformat(last) < max_age:
        return None
    return sync(store, user_id, fetch, now)
