"""MCP server glue, with the WHOOP HTTP call (whoop_get) faked."""

import asyncio
from datetime import date, datetime, timezone

import pytest

import whoop_mcp
from tests import factories as f

PROFILE = {"user_id": 1, "email": "me@example.com", "first_name": "A", "last_name": "B"}


class FakeAPI:
    """Serves WHOOP paths; collections are split into pages of `page_size`."""

    def __init__(self, page_size=25, **collections):
        self.page_size = page_size
        self.collections = collections
        self.calls = []

    async def __call__(self, path, params=None):
        params = params or {}
        self.calls.append((path, params))
        if path == "/v2/user/profile/basic":
            return PROFILE
        records = self.collections.get(path, [])
        offset = int(params.get("nextToken") or 0)
        size = min(self.page_size, params.get("limit") or self.page_size)
        page = records[offset:offset + size]
        nxt = offset + size
        return {"records": page, "next_token": str(nxt) if nxt < len(records) else None}


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(whoop_mcp, "DB_FILE", tmp_path / "whoop.db")
    monkeypatch.setattr(whoop_mcp, "_now", lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc))
    monkeypatch.setattr(whoop_mcp, "_today", lambda: date(2026, 10, 18))
    whoop_mcp._reset_state()

    def install(api):
        monkeypatch.setattr(whoop_mcp, "whoop_get", api)
        return api

    yield install
    whoop_mcp._reset_state()


def run(coro):
    return asyncio.run(coro)


# --- pagination -----------------------------------------------------------


def test_collection_without_limit_follows_every_page(server):
    records = [{"id": n} for n in range(60)]
    api = server(FakeAPI(page_size=25, **{"/v2/cycle": records}))
    got = run(whoop_mcp.whoop_collection("/v2/cycle", None, None, None))
    assert got == records
    assert [c[1]["nextToken"] for c in api.calls] == [None, "25", "50"]


def test_collection_stops_at_the_limit(server):
    server(FakeAPI(page_size=25, **{"/v2/cycle": [{"id": n} for n in range(60)]}))
    assert len(run(whoop_mcp.whoop_collection("/v2/cycle", None, None, 30))) == 30


# --- get_today ------------------------------------------------------------


def test_get_today_skips_a_nap_and_returns_last_nights_sleep(server):
    nap = f.sleep("2026-10-18T17:00:00Z", "2026-10-18T17:40:00Z", nap=True)
    night = f.sleep("2026-10-18T03:00:00Z", "2026-10-18T11:00:00Z")
    server(FakeAPI(**{"/v2/activity/sleep": [nap, night]}))
    today = run(whoop_mcp.get_today())
    assert [s["id"] for s in today["sleep"]] == [night["id"]]


def test_get_today_explains_a_recovery_that_is_not_scored_yet(server):
    c = f.cycle("2026-10-18T03:00:00Z", None)
    s = f.sleep("2026-10-18T03:00:00Z", "2026-10-18T11:00:00Z", cycle_id=c["id"])
    r = f.recovery(c["id"], s["id"], state="PENDING_SCORE")
    server(FakeAPI(**{"/v2/cycle": [c], "/v2/activity/sleep": [s], "/v2/recovery": [r]}))
    today = run(whoop_mcp.get_today())
    assert any("PENDING_SCORE" in n for n in today["notes"])


# --- engine tools ---------------------------------------------------------


def nights_api():
    cycles, sleeps, recs = [], [], []
    for day, hrv in ((15, 60.0), (16, 70.0), (17, 80.0)):
        c = f.cycle(f"2026-10-{day}T03:00:00Z", f"2026-10-{day + 1}T03:00:00Z")
        s = f.sleep(f"2026-10-{day}T03:00:00Z", f"2026-10-{day}T11:00:00Z", cycle_id=c["id"])
        cycles.append(c), sleeps.append(s), recs.append(f.recovery(c["id"], s["id"], hrv=hrv))
    return FakeAPI(**{"/v2/cycle": cycles, "/v2/activity/sleep": sleeps, "/v2/recovery": recs})


def test_query_metric_syncs_on_first_use_and_answers_from_the_store(server):
    api = server(nights_api())
    r = run(whoop_mcp.query_metric("hrv_ms", "2026-10-15", "2026-10-17"))
    assert r["value"] == 70
    assert r["observed_days"] == 3
    calls_after_first = len(api.calls)
    run(whoop_mcp.query_metric("hrv_ms", "2026-10-15", "2026-10-17"))
    assert len(api.calls) == calls_after_first  # fresh enough: no second sync


def test_logged_events_feed_goals(server):
    server(nights_api())
    run(whoop_mcp.log_event("Alcohol", day="2026-10-13"))
    run(whoop_mcp.log_event("alcohol", day="2026-10-14", value=2))
    goal = run(whoop_mcp.set_goal("event:alcohol", "total", "<=", 2, "week"))
    assert goal["progress"]["current"] == 3
    assert goal["progress"]["status"] == "missed"
    listed = run(whoop_mcp.list_goals())
    assert [g["goal"]["id"] for g in listed["goals"]] == [goal["goal"]["id"]]


def test_bad_dates_are_rejected_with_a_clear_error(server):
    server(nights_api())
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        run(whoop_mcp.query_metric("hrv_ms", "last week", "2026-10-17"))


def test_every_tool_tells_the_assistant_what_it_does():
    tools = run(whoop_mcp.mcp.list_tools())
    undocumented = [t.name for t in tools if not (t.description or "").strip()]
    assert undocumented == []


def test_concurrent_tool_calls_share_one_sync(server):
    api = server(nights_api())

    async def both():
        return await asyncio.gather(
            whoop_mcp.query_metric("hrv_ms", "2026-10-15", "2026-10-17"),
            whoop_mcp.get_baseline("hrv_ms"),
        )

    run(both())
    cycle_first_pages = [c for c in api.calls if c[0] == "/v2/cycle" and c[1]["nextToken"] is None]
    assert len(cycle_first_pages) == 1
