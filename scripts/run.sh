#!/bin/sh
# Start the server the way the LaunchAgent does. Loads .env first, which on
# the mini holds CLAUDE_CODE_OAUTH_TOKEN so the clue writer can reach Claude.
cd "$(dirname "$0")/.." || exit 1
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
exec .venv/bin/python server.py
