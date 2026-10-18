"""
WHOOP MCP server (Python) — personal, read-only, official OAuth.

Setup:
  1. Register an app at https://developer-dashboard.whoop.com
     - Redirect URI: http://localhost:8765/callback
     - Scopes: read:profile read:body_measurement read:cycles read:recovery
               read:sleep read:workout   (offline is requested at auth time)
  2. pip install "mcp[cli]" httpx python-dotenv
  3. Create .env with WHOOP_CLIENT_ID=... and WHOOP_CLIENT_SECRET=...
  4. python whoop_mcp.py login      # one-time browser OAuth, saves tokens
  5. python whoop_mcp.py            # runs the MCP server over stdio

API reference: https://developer.whoop.com/api
"""

import asyncio
import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

from whoop_engine.analytics import Analytics
from whoop_engine.goals import goal_history, goal_progress, validate_goal
from whoop_engine.metrics import METRICS, normalize_label
from whoop_engine.store import Store
from whoop_engine.sync import sync, sync_if_stale

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().with_name(".env"))
except ImportError:
    pass

# --- Config ---------------------------------------------------------------

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"  # endpoints below are /v2/...
REDIRECT_URI = os.environ.get("WHOOP_REDIRECT_URI", "http://localhost:8765/callback")
SCOPES = (
    "read:profile read:body_measurement read:cycles read:recovery "
    "read:sleep read:workout offline"
)
PAGE_MAX = 25  # WHOOP caps `limit` at 25 per page

CLIENT_ID = os.environ.get("WHOOP_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("WHOOP_CLIENT_SECRET", "")
TOKEN_FILE = Path(
    os.environ.get("WHOOP_TOKEN_FILE", Path.home() / ".whoop-mcp" / "tokens.json")
)
DB_FILE = Path(os.environ.get("WHOOP_DB_FILE", Path.home() / ".whoop-mcp" / "whoop.db"))

_state: dict[str, Any] = {}  # lazily opened store and the WHOOP user id
_sync_lock = asyncio.Lock()  # one sync at a time; SQLite must not be written from two threads


def _reset_state() -> None:
    global _sync_lock
    _state.clear()
    _sync_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return date.today()

# --- Token storage --------------------------------------------------------

_refresh_lock = threading.Lock()  # WHOOP invalidates old refresh tokens; avoid races


def load_tokens() -> dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise RuntimeError("Not logged in. Run: python whoop_mcp.py login")
    return json.loads(TOKEN_FILE.read_text())


def save_tokens(tokens: dict[str, Any]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600) - 60
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    r = httpx.post(
        TOKEN_URL,
        data={**data, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def exchange_code(code: str) -> dict[str, Any]:
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI}
    )


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    return _token_request(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": "offline"}
    )


def get_access_token(force_refresh: bool = False) -> str:
    with _refresh_lock:
        tokens = load_tokens()
        if force_refresh or time.time() >= tokens.get("expires_at", 0):
            if "refresh_token" not in tokens:
                raise RuntimeError(
                    "No refresh token (offline scope missing). Run: python whoop_mcp.py login"
                )
            tokens = refresh_tokens(tokens["refresh_token"])
            save_tokens(tokens)
        return tokens["access_token"]


# --- API client -----------------------------------------------------------


async def whoop_get(path: str, params: dict[str, Any] | None = None) -> Any:
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(base_url=API_BASE, timeout=30) as client:
        for attempt in range(2):
            token = get_access_token(force_refresh=attempt == 1)
            r = await client.get(
                path, params=clean, headers={"Authorization": f"Bearer {token}"}
            )
            if r.status_code == 401 and attempt == 0:
                continue  # token may have been invalidated; refresh once and retry
            r.raise_for_status()
            return r.json()


async def whoop_collection(
    path: str, start: str | None, end: str | None, limit: int | None
) -> list[dict]:
    """Fetch a paginated collection, following nextToken until `limit` records
    (or every record when limit is None)."""
    records: list[dict] = []
    next_token: str | None = None
    while limit is None or len(records) < limit:
        page_size = PAGE_MAX if limit is None else min(PAGE_MAX, limit - len(records))
        page = await whoop_get(
            path,
            {"start": start, "end": end, "limit": page_size, "nextToken": next_token},
        )
        records.extend(page.get("records", []))
        next_token = page.get("next_token")
        if not next_token:
            break
    return records


# --- MCP server -----------------------------------------------------------

mcp = FastMCP("whoop")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request is noise

DATE_HELP = "ISO 8601 datetime, e.g. 2026-09-01T00:00:00Z. Omit for most recent."


@mcp.tool()
async def get_profile() -> dict:
    """Basic WHOOP user profile (name, email, user id)."""
    return await whoop_get("/v2/user/profile/basic")


@mcp.tool()
async def get_body_measurements() -> dict:
    """Height, weight and max heart rate on file."""
    return await whoop_get("/v2/user/measurement/body")


@mcp.tool()
async def get_recovery(
    start: str | None = None, end: str | None = None, limit: int = 7
) -> list[dict]:
    """Recovery scores: recovery %, HRV (hrv_rmssd_milli), resting HR, SpO2, skin temp.
    Note: a day's recovery only exists once the previous night's sleep is processed.
    start/end: ISO 8601 datetime, e.g. 2026-09-01T00:00:00Z. Omit for most recent."""
    return await whoop_collection("/v2/recovery", start, end, limit)


@mcp.tool()
async def get_sleep(
    start: str | None = None, end: str | None = None, limit: int = 7
) -> list[dict]:
    """Sleep records: stage durations, efficiency, consistency, respiratory rate,
    sleep performance %, sleep needed. start/end: ISO 8601 datetime. Omit for most recent."""
    return await whoop_collection("/v2/activity/sleep", start, end, limit)


@mcp.tool()
async def get_cycles(
    start: str | None = None, end: str | None = None, limit: int = 7
) -> list[dict]:
    """Physiological cycles (days): strain, kilojoules, average and max heart rate.
    start/end: ISO 8601 datetime. Omit for most recent."""
    return await whoop_collection("/v2/cycle", start, end, limit)


@mcp.tool()
async def get_recovery_for_cycle(cycle_id: int) -> dict:
    """Recovery for a specific cycle id (from get_cycles)."""
    return await whoop_get(f"/v2/cycle/{cycle_id}/recovery")


@mcp.tool()
async def get_workouts(
    start: str | None = None, end: str | None = None, limit: int = 10
) -> list[dict]:
    """Workouts: sport, strain, HR zones, calories, distance, altitude.
    start/end: ISO 8601 datetime. Omit for most recent."""
    return await whoop_collection("/v2/activity/workout", start, end, limit)


@mcp.tool()
async def get_today() -> dict:
    """Snapshot: latest cycle, recovery and main (non-nap) sleep in one call.
    `notes` explains anything not scored yet. The current cycle's strain is
    still accumulating until the day ends."""
    cycles = await whoop_collection("/v2/cycle", None, None, 1)
    recovery = await whoop_collection("/v2/recovery", None, None, 1)
    recent_sleep = await whoop_collection("/v2/activity/sleep", None, None, 5)
    sleep = [s for s in recent_sleep if not s.get("nap")][:1]
    notes = []
    for kind, records in (("cycle", cycles), ("recovery", recovery), ("sleep", sleep)):
        if records and records[0].get("score_state") != "SCORED":
            notes.append(f"Latest {kind} is {records[0].get('score_state')}; no score yet.")
    if cycles and cycles[0].get("end") is None:
        notes.append("Today's cycle is in progress; strain will keep rising.")
    return {"cycle": cycles, "recovery": recovery, "sleep": sleep, "notes": notes}


# --- Evidence engine tools ------------------------------------------------
# These answer from a local SQLite store (DB_FILE) that syncs from WHOOP when
# it is more than an hour old. Results carry their evidence: coverage, missing
# days, source record ids, method version and warnings. Quote the warnings.

COLLECTION_PATHS = {
    "cycle": "/v2/cycle",
    "sleep": "/v2/activity/sleep",
    "recovery": "/v2/recovery",
    "workout": "/v2/activity/workout",
}


def _day(value: str | None, name: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date in YYYY-MM-DD form, got {value!r}") from None


def _fetch_blocking(kind: str, start: str, end: str) -> list[dict]:
    # Runs in a worker thread (see _engine), which has no event loop of its own.
    return asyncio.run(whoop_collection(COLLECTION_PATHS[kind], start, end, None))


async def _engine(force_sync: bool = False) -> Analytics:
    async with _sync_lock:
        if "store" not in _state:
            _state["store"] = Store(DB_FILE)
        if "user_id" not in _state:
            _state["user_id"] = int((await whoop_get("/v2/user/profile/basic"))["user_id"])
        store, user_id = _state["store"], _state["user_id"]
        runner = sync if force_sync else sync_if_stale
        result = await asyncio.to_thread(runner, store, user_id, _fetch_blocking, _now())
        if result:
            _state["last_sync"] = result
    return Analytics(store, user_id, _today())


@mcp.tool()
async def sync_now() -> dict:
    """Pull the latest WHOOP data into the local store now (normally automatic
    when data is over an hour old). The first sync backfills 180 days."""
    await _engine(force_sync=True)
    return _state["last_sync"]


@mcp.tool()
async def list_metrics() -> dict:
    """Metrics the engine can analyse, with units. Also `event:<label>` for any
    logged life event (e.g. event:alcohol)."""
    return {
        "metrics": [
            {"name": m.name, "unit": m.unit, "source": m.source, "description": m.description,
             "missing_day_means_zero": m.zero_fill}
            for m in METRICS.values()
        ],
        "events": "event:<label> counts logged events per day (see log_event).",
    }


@mcp.tool()
async def data_status() -> dict:
    """When data was last synced, how far back history goes, and the last sync summary."""
    a = await _engine()
    return {
        "last_synced_at": a.store.get_meta(a.user_id, "last_synced_at"),
        "covered_from": a.store.get_meta(a.user_id, "covered_from"),
        "database": str(DB_FILE),
        "last_sync": _state.get("last_sync"),
    }


@mcp.tool()
async def query_metric(
    metric: str,
    start: str,
    end: str,
    aggregation: str = "mean",
    include_incomplete: bool = False,
) -> dict:
    """Compute one metric over a date range: mean, median, min, max, sum, count,
    stdev, or series (per-day values with source ids). Prefer this over doing
    arithmetic on raw records. Today's in-progress strain is excluded unless
    include_incomplete=True.
    Dates are YYYY-MM-DD (inclusive), in your local WHOOP days."""
    a = await _engine()
    return a.query_metric(metric, _day(start, "start"), _day(end, "end"), aggregation,
                          include_incomplete)


@mcp.tool()
async def get_baseline(metric: str, days: int = 30) -> dict:
    """Is the latest value normal for me? Compares the most recent value with the
    `days` before it: mean, stdev, z-score and percentile rank."""
    return (await _engine()).baseline(metric, days)


@mcp.tool()
async def compare_periods(
    metric: str,
    first_start: str,
    first_end: str,
    second_start: str,
    second_end: str,
    aggregation: str = "mean",
) -> dict:
    """Change in a metric from the first period to the second, e.g. last month
    vs. this month. difference = second - first.
    Dates are YYYY-MM-DD (inclusive), in your local WHOOP days."""
    a = await _engine()
    return a.compare_periods(
        metric,
        (_day(first_start, "first_start"), _day(first_end, "first_end")),
        (_day(second_start, "second_start"), _day(second_end, "second_end")),
        aggregation,
    )


@mcp.tool()
async def verify_claim(
    metric: str, start: str, end: str, aggregation: str, claimed_value: float
) -> dict:
    """Recompute a number before stating it (e.g. "your average HRV was 62 ms").
    verdict: matches | does_not_match | cannot_verify.
    Dates are YYYY-MM-DD (inclusive), in your local WHOOP days."""
    a = await _engine()
    return a.verify_claim(metric, _day(start, "start"), _day(end, "end"), aggregation,
                          claimed_value)


@mcp.tool()
async def log_event(
    label: str, day: str | None = None, value: float = 1, note: str | None = None
) -> dict:
    """Record a life event WHOOP doesn't know about: alcohol, late meal, travel,
    illness, caffeine, stress... `value` is an amount (default 1, e.g. 2 drinks).
    `day` defaults to today. Use a consistent short label for the same thing."""
    a = await _engine()
    when = _day(day, "day") or a.today
    label = normalize_label(label)
    event_id = a.store.add_event(a.user_id, when, label, value, note)
    return {"id": event_id, "day": str(when), "label": label, "value": value, "note": note}


@mcp.tool()
async def list_events(
    start: str | None = None, end: str | None = None, label: str | None = None
) -> dict:
    """Logged life events, optionally filtered by label. Defaults to the last 30 days."""
    a = await _engine()
    end_day = _day(end, "end") or a.today
    start_day = _day(start, "start") or end_day - timedelta(29)
    return {"events": a.store.events(a.user_id, start_day, end_day,
                                     normalize_label(label) if label else None)}


@mcp.tool()
async def delete_event(event_id: int) -> dict:
    """Delete a logged life event by id."""
    a = await _engine()
    return {"deleted": a.store.delete_event(a.user_id, event_id)}


@mcp.tool()
async def event_effect(label: str, metric: str, lag_days: int = 1, days: int = 90) -> dict:
    """How does a metric differ after a logged event? lag_days=1 compares e.g.
    recovery the morning after drinking with all other days. An association,
    not proof of cause; mention the warnings."""
    a = await _engine()
    return a.event_effect(label, metric, lag_days, a.today - timedelta(days - 1), a.today)


@mcp.tool()
async def set_goal(
    metric: str,
    kind: str,
    op: str,
    target: float,
    period: str,
    min_days: int | None = None,
    name: str | None = None,
) -> dict:
    """Create a goal on any metric (or event:<label>), per week (Mon-Sun) or month.
    kind=days: value op target on at least min_days days (sleep_hours >= 7.5 on 5 days);
    kind=total: period sum op target (workout_count >= 4; event:alcohol <= 2);
    kind=average: period mean op target (hrv_ms >= 60). op is >= or <=."""
    a = await _engine()
    spec = validate_goal({"metric": metric, "kind": kind, "op": op, "target": target,
                          "period": period, "min_days": min_days})
    if name:
        spec["name"] = name
    goal_id = a.store.add_goal(a.user_id, spec)
    goal = {**spec, "id": goal_id}
    return {"goal": goal, "progress": goal_progress(a, goal)}


@mcp.tool()
async def list_goals() -> dict:
    """Active goals with progress for the current week/month."""
    a = await _engine()
    return {"goals": [goal_progress(a, g) for g in a.store.goals(a.user_id)]}


@mcp.tool()
async def get_goal_history(goal_id: int, periods: int = 4) -> dict:
    """Past weeks/months of a goal (met or not, newest first) and the current streak."""
    a = await _engine()
    goals = {g["id"]: g for g in a.store.goals(a.user_id, include_archived=True)}
    if goal_id not in goals:
        raise ValueError(f"No goal with id {goal_id}")
    return goal_history(a, goals[goal_id], periods)


@mcp.tool()
async def archive_goal(goal_id: int) -> dict:
    """Stop tracking a goal (kept for history)."""
    a = await _engine()
    return {"archived": a.store.archive_goal(a.user_id, goal_id)}


# --- One-time login flow --------------------------------------------------


def login() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET (in .env or the environment).")

    state = secrets.token_hex(4)  # WHOOP requires an 8-character state
    url = AUTH_URL + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
        }
    )
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("state", [""])[0] != state:
                self.send_error(400, "state mismatch")
                return
            if "error" in qs:
                self.send_error(400, qs.get("error_description", qs["error"])[0])
                result["error"] = qs["error"][0]
                return
            result["code"] = qs["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"WHOOP login complete. You can close this tab.")

        def log_message(self, *_):
            pass

    port = urlparse(REDIRECT_URI).port or 8765
    server = HTTPServer(("localhost", port), Handler)
    print("Opening browser for WHOOP login...\nIf it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if "error" in result:
        sys.exit(f"WHOOP authorization failed: {result['error']}")
    save_tokens(exchange_code(result["code"]))
    print(f"Tokens saved to {TOKEN_FILE}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        mcp.run()
