#!/usr/bin/env bash
#
# One-shot installer for the PPK2 MCP server.
#   - installs uv if missing
#   - syncs Python dependencies into a local .venv
#   - registers the `ppk2` MCP server with the Claude Code CLI (port autodetected)
#   - prints a ready-to-paste Claude prompt to try it
#
# Usage:
#   ./install.sh           # install + register
#   ./install.sh --run     # also launch Claude with the starter prompt
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PROMPT='Use the ppk2 MCP tools to check the PPK2: call ppk2_status, then configure source mode at 3.3V, power the DUT on, measure current for 2 seconds, capture the logic channels for 1 second, and finally power off and disconnect. Summarise the results.'

# 1. Ensure uv is available.
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "==> uv $(uv --version | awk '{print $2}')"

# 2. Sync dependencies.
echo "==> Syncing dependencies (uv sync)..."
uv sync

# 3. Register with Claude Code (user scope, port autodetected).
if command -v claude >/dev/null 2>&1; then
  echo "==> Registering 'ppk2' MCP server with Claude Code..."
  claude mcp remove -s user ppk2 >/dev/null 2>&1 || true
  claude mcp add -s user ppk2 -- uv run --directory "$HERE" ppk2_mcp_server.py
  echo "==> Registered. Check with:  claude mcp list"
else
  echo "!! Claude Code CLI not found — skipping registration."
  echo "   This repo's .mcp.json still registers 'ppk2' for any Claude Code"
  echo "   session opened in $HERE."
fi

echo
echo "==> Done. Try it with this prompt:"
echo
echo "    $PROMPT"
echo

# 4. Optionally launch Claude with the starter prompt.
if [[ "${1:-}" == "--run" ]] && command -v claude >/dev/null 2>&1; then
  echo "==> Launching Claude..."
  exec claude "$PROMPT"
fi
