# Agent Board — Standalone Service

Optional. Most operators just run the FastAPI engine (port 9210) and reach the
board at `/board/*`. These scripts are for headless deployments that only need
the coordination surface.

## Windows

```bat
scripts\agent-board\start-board.bat
```

Spawns a hidden PowerShell watchdog (`board-watchdog.ps1`) that restarts the
board service if it dies. Override port with `set BOARD_PORT=11500` before
launching.

## Linux / macOS

```bash
BOARD_PORT=11437 scripts/agent-board/serve-board.sh
```

Runs the service in the foreground. Wrap with systemd / supervisord / tmux for
process supervision.

## Endpoints

The standalone service exposes only `/board/*` and `/health`. See
`docs/AGENT-BOARD.md` for the full route inventory.

## Files

- `start-board.bat` — Windows one-click launcher
- `board-watchdog.ps1` — Windows watchdog (polls `/health` every 15s)
- `serve-board.sh` — POSIX foreground launcher
- `watchdog.pid` — watchdog PID (auto-created, gitignored)
