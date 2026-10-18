#!/usr/bin/env bash
# One-command setup: venv + deps, WHOOP credentials, OAuth login, Claude Code registration.
# Safe to re-run: every step that is already done is skipped.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd -P)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
ENV_FILE="$ROOT/.env"
TOKEN_FILE="${WHOOP_TOKEN_FILE:-$HOME/.whoop-mcp/tokens.json}"
DEFAULT_REDIRECT="http://localhost:8765/callback"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
step() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

is_py310() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }

find_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && is_py310 "$c"; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

env_get() {
  # Read KEY from .env without sourcing it; strips surrounding whitespace and quotes.
  [ -f "$ENV_FILE" ] || return 0
  { grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" || true; } | tail -n 1 | cut -d= -f2- |
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# --- 1. Python environment (silent) ------------------------------------------
step "Preparing Python environment"
if [ ! -x "$PY" ] || ! is_py310 "$PY"; then
  BASE_PY="$(find_python)" || die "Python 3.10+ not found. Install it (e.g. 'brew install python') and re-run."
  rm -rf "$VENV"
  "$BASE_PY" -m venv "$VENV" </dev/null
fi
PIP_LOG="$VENV/setup-pip.log"
if ! "$PY" -m pip install -q --disable-pip-version-check -r "$ROOT/requirements.txt" </dev/null >"$PIP_LOG" 2>&1; then
  tail -n 20 "$PIP_LOG" >&2
  die "Dependency install failed (full log: $PIP_LOG)"
fi
ok "Dependencies ready ($("$PY" -V))"

# --- 2. WHOOP credentials ----------------------------------------------------
step "WHOOP credentials"
CLIENT_ID="$(env_get WHOOP_CLIENT_ID)"
CLIENT_SECRET="$(env_get WHOOP_CLIENT_SECRET)"
REDIRECT_URI="$(env_get WHOOP_REDIRECT_URI)"
REDIRECT_URI="${REDIRECT_URI:-$DEFAULT_REDIRECT}"

if [ -z "$CLIENT_ID" ] || [ -z "$CLIENT_SECRET" ]; then
  [ -t 0 ] || die "No WHOOP credentials saved and no terminal to ask in. Run ./setup.sh in a terminal."
  echo "  Find these in the WHOOP developer dashboard under your app."
  while [ -z "$CLIENT_ID" ]; do
    read -r -p "  WHOOP Client ID: " CLIENT_ID || die "Cancelled."
  done
  while [ -z "$CLIENT_SECRET" ]; do
    read -r -s -p "  WHOOP Client Secret (hidden): " CLIENT_SECRET || die "Cancelled."
    echo
  done
  (
    umask 077
    printf 'WHOOP_CLIENT_ID=%s\nWHOOP_CLIENT_SECRET=%s\nWHOOP_REDIRECT_URI=%s\n' \
      "$CLIENT_ID" "$CLIENT_SECRET" "$REDIRECT_URI" >"$ENV_FILE.tmp"
  )
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  ok "Saved to .env"
else
  ok "Using saved credentials from .env"
fi
chmod 600 "$ENV_FILE"

# --- 3. WHOOP login ----------------------------------------------------------
step "WHOOP login"
whoop_works() {
  (cd "$ROOT" && "$PY" -c 'import asyncio, whoop_mcp; asyncio.run(whoop_mcp.get_profile())') >/dev/null 2>&1
}
if [ -s "$TOKEN_FILE" ] && whoop_works; then
  ok "Already logged in"
else
  echo "  Opening your browser for WHOOP consent..."
  (cd "$ROOT" && "$PY" whoop_mcp.py login) || true
  whoop_works || die "WHOOP login failed. Check your Client ID/Secret (delete .env to re-enter them) and re-run."
  ok "Logged in"
fi

# --- 4. Register with Claude Code --------------------------------------------
step "Connecting to Claude Code"
CLAUDE="$(command -v claude || true)"
if [ -z "$CLAUDE" ] && [ -x "$HOME/.local/bin/claude" ]; then
  CLAUDE="$HOME/.local/bin/claude"
fi
if [ -z "$CLAUDE" ]; then
  echo "  Claude Code CLI not found. Install it with:"
  echo "    curl -fsSL https://claude.ai/install.sh | bash"
  die "then re-run ./setup.sh"
fi
"$CLAUDE" mcp remove whoop -s user >/dev/null 2>&1 || true
"$CLAUDE" mcp add --scope user --transport stdio whoop -- "$PY" "$ROOT/whoop_mcp.py" >/dev/null
if "$CLAUDE" mcp get whoop 2>/dev/null | grep -q "Connected"; then
  ok "Registered 'whoop' with Claude Code (all projects)"
else
  "$CLAUDE" mcp get whoop >&2 || true
  die "Registered, but Claude Code could not connect to the server."
fi

echo
printf '\033[32mDone.\033[0m Open a new Claude Code session and ask: "what'"'"'s my recovery today?"\n'
