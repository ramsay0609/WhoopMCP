# WhoopMCP

A personal, read-only MCP (Model Context Protocol) server that gives Claude access to your own WHOOP health data — recovery, HRV, sleep, strain, and workouts — through WHOOP's official Developer API v2.

This is a single-user tool built and run by its developer against their own WHOOP account under WHOOP's sandbox developer tier. It is not a public service, does not store data on any server, and is not intended for use by anyone other than its developer. See [PRIVACY_POLICY.md](PRIVACY_POLICY.md).

## Setup

### 1. Register a WHOOP developer app

Create an app at the [WHOOP developer dashboard](https://developer-dashboard.whoop.com) with:

- **Redirect URI:** `http://localhost:8765/callback`
- **Scopes:** `read:profile`, `read:body_measurement`, `read:cycles`, `read:recovery`, `read:sleep`, `read:workout` (the `offline` scope is requested automatically at login time, not configured in the dashboard)

### 2. Run setup

```bash
./setup.sh
```

That's the only command. It:

- creates a Python virtual environment and installs dependencies in the background (needs Python 3.10+ installed; it finds Homebrew's Python even if it isn't on your `PATH`)
- asks for your WHOOP **Client ID** and **Client Secret** in the terminal (the secret is hidden as you type) and saves them to `.env` with mode `600`
- opens your browser for WHOOP's consent screen, then saves tokens to `~/.whoop-mcp/tokens.json` (mode `600`)
- registers the server with Claude Code for all your projects

Then open a new Claude Code session and ask "what's my recovery today?".

It's safe to re-run: finished steps are skipped. Re-run it after moving or renaming this folder to update the registration. To change credentials, delete `.env` and re-run. If the Claude Code CLI isn't installed, the script tells you how to install it.

## Tools

| Tool | Description |
| --- | --- |
| `get_profile` | Basic WHOOP user profile (name, email, user id) |
| `get_body_measurements` | Height, weight, and max heart rate on file |
| `get_recovery` | Recovery scores: recovery %, HRV, resting HR, SpO2, skin temp |
| `get_sleep` | Sleep records: stage durations, efficiency, consistency, sleep performance |
| `get_cycles` | Physiological cycles (days): strain, kilojoules, average/max heart rate |
| `get_recovery_for_cycle` | Recovery for a specific cycle id |
| `get_workouts` | Workouts: sport, strain, HR zones, calories, distance, altitude |
| `get_today` | Snapshot combining the latest cycle, recovery, and sleep in one call |

## Notes

- Read-only: this server only calls WHOOP's GET endpoints. It never writes or modifies WHOOP data.
- Official OAuth only: authentication goes through WHOOP's documented OAuth 2.0 flow. No private/undocumented API, no WHOOP account credentials handled directly by this app.
- Personal use only, under WHOOP's sandbox developer tier — not for use with other users' accounts.
