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

import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from mcp.server.fastmcp import FastMCP

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
    path: str, start: str | None, end: str | None, limit: int
) -> list[dict]:
    """Fetch a paginated collection, following nextToken until `limit` records."""
    records: list[dict] = []
    next_token: str | None = None
    while len(records) < limit:
        page = await whoop_get(
            path,
            {
                "start": start,
                "end": end,
                "limit": min(PAGE_MAX, limit - len(records)),
                "nextToken": next_token,
            },
        )
        records.extend(page.get("records", []))
        next_token = page.get("next_token")
        if not next_token:
            break
    return records


# --- MCP server -----------------------------------------------------------

mcp = FastMCP("whoop")

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
    """Snapshot: latest cycle, recovery and sleep in one call.
    Recovery may be empty early in the morning before sleep is processed."""
    cycles = await whoop_collection("/v2/cycle", None, None, 1)
    recovery = await whoop_collection("/v2/recovery", None, None, 1)
    sleep = await whoop_collection("/v2/activity/sleep", None, None, 1)
    return {"cycle": cycles[:1], "recovery": recovery[:1], "sleep": sleep[:1]}


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
