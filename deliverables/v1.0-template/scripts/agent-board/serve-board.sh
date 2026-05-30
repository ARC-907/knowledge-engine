#!/usr/bin/env bash
# Knowledge-Engine — Agent Board standalone launcher (POSIX)
# Starts the board service in the foreground. Use a process supervisor
# (systemd, supervisord, tmux, etc.) for restart-on-failure on Linux/macOS.

set -euo pipefail

PORT="${BOARD_PORT:-11437}"
HOST="${BOARD_HOST:-127.0.0.1}"

echo "Starting Knowledge-Engine Agent Board on ${HOST}:${PORT}"
exec python -m knowledge_engine.agent_board.service --host "$HOST" --port "$PORT"
