# WhoopMCP

A personal MCP (Model Context Protocol) server that gives Claude access to your own WHOOP health data (recovery, HRV, sleep, strain, and workouts) through WHOOP's official Developer API v2, read-only. It also includes an analysis engine that computes trends, baselines and goal progress, and tracks life events you log, with every number traceable to its source records.

It is not a hosted service: you run your own copy on your own machine, with your own WHOOP developer app, against your own WHOOP account. Your data stays on your machine except for what the tools return to your AI assistant (see [Privacy](#privacy)).

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

## Analysis tools (evidence engine)

These answer from a local SQLite copy of your data (`~/.whoop-mcp/whoop.db`, mode `600`, override with `WHOOP_DB_FILE`). It syncs from WHOOP automatically when it's more than an hour old; the first sync backfills 180 days. The server computes every number. Each result says which days it covers, how many days had data and which are missing, the source record IDs, the method version and any warnings (low coverage, today still in progress, records not yet scored), so Claude explains numbers instead of doing arithmetic.

| Tool | Description |
| --- | --- |
| `query_metric` | One metric over a date range: mean, median, min, max, sum, count, stdev, or a per-day series |
| `get_baseline` | Is the latest value normal for me? Mean, stdev, z-score and percentile vs. the preceding days |
| `compare_periods` | Change in a metric between two date ranges |
| `verify_claim` | Recompute a number before stating it: `matches`, `does_not_match`, or `cannot_verify` |
| `log_event` / `list_events` / `delete_event` | Record life events WHOOP doesn't know about (alcohol, travel, illness, caffeine…) |
| `event_effect` | How a metric differs the day after (or the day of) an event vs. other days |
| `set_goal` / `list_goals` / `get_goal_history` / `archive_goal` | Weekly or monthly goals on any metric or event, with pace, status and streaks |
| `list_metrics` | Available metrics and units, plus `event:<label>` for logged events |
| `sync_now` / `data_status` | Force a sync; see when data was last synced and how far back it goes |

**Day rules:** sleep counts toward the local day you woke up. Each cycle and its recovery go on the same day as their sleep. Naps are kept separate (`nap_hours`). Workouts go on the local day they started. Days with no workout count as 0. Today's strain is left out of averages until the day ends.

Run the tests with `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest`.

## Raw data tools

| Tool | Description |
| --- | --- |
| `get_profile` | Basic WHOOP user profile (name, email, user id) |
| `get_body_measurements` | Height, weight, and max heart rate on file |
| `get_recovery` | Recovery scores: recovery %, HRV, resting HR, SpO2, skin temp |
| `get_sleep` | Sleep records: stage durations, efficiency, consistency, sleep performance |
| `get_cycles` | Physiological cycles (days): strain, kilojoules, average/max heart rate |
| `get_recovery_for_cycle` | Recovery for a specific cycle id |
| `get_workouts` | Workouts: sport, strain, HR zones, calories, distance, altitude |
| `get_today` | Snapshot combining the latest cycle, recovery, and main (non-nap) sleep, with notes on anything not yet scored |

## Notes

- Read-only toward WHOOP: this server only calls WHOOP's GET endpoints. It never writes or modifies WHOOP data. The only things it writes are its own local store: synced records, goals and logged events.
- Official OAuth only: authentication goes through WHOOP's documented OAuth 2.0 flow. No private/undocumented API, no WHOOP account credentials handled directly by this app.
- One account per copy: WHOOP's sandbox developer tier only covers your own account, so each person registers their own developer app. Don't connect other people's accounts to yours.

## Privacy

- WHOOP data, goals and logged events are stored only on your machine, in `~/.whoop-mcp/` (tokens in `tokens.json`, data in `whoop.db`, both mode `600`).
- What a tool returns is sent to your AI assistant's provider (for Claude, Anthropic) as part of the conversation and handled under their terms. The analysis tools return computed summaries, which reveal less than the raw records.
- To delete everything, remove the `~/.whoop-mcp/` folder. To revoke access, also remove the app from your WHOOP account.

See [PRIVACY_POLICY.md](PRIVACY_POLICY.md) for the policy covering the developer's own WHOOP app.

## License

[MIT](LICENSE)
