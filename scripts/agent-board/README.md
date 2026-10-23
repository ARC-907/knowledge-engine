# Agent Board — Standalone Service

Optional. Most operators just run the FastAPI engine (port 9210) and reach the
board at `/board/*`. The standalone service is for headless deployments that
only need the coordination surface.

## Any platform (foreground)

```bash
uv run --project engine knowledge-engine board-serve --host 127.0.0.1 --port 11437
```

Runs the service in the foreground. Wrap it with systemd / supervisord / tmux /
a Windows service for process supervision.

## Windows watchdog

`board-watchdog.ps1` starts the service as a child process, polls `/health`
every 15 s, and restarts **its own child** when it dies or stops answering. It
never stops a process it did not start: if another process already owns the
port it logs that and waits.

```pwsh
pwsh -NoProfile -File scripts\agent-board\board-watchdog.ps1 -Port 11437
```

To run it hidden in the background:

```pwsh
Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File','scripts\agent-board\board-watchdog.ps1'
```

- Interpreter: `engine\.venv\Scripts\python.exe` (from `uv sync --project engine`),
  falling back to `uv run` when the venv is missing.
- Port / host: `-Port` / `-BindHost`, or `BOARD_PORT` / `BOARD_HOST`.
- PID file: `<data dir>\watchdog.pid`, where the data dir is `KE_DATA_DIR` or
  `%LOCALAPPDATA%\knowledge-engine`. Stop the watchdog by ending that PID; it
  stops the child service on the way out.
- Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once if PowerShell
  refuses to run local scripts; the watchdog no longer bypasses the policy.

## Endpoints

The standalone service exposes only `/board/*` and `/health`. For the full
route inventory, see the live OpenAPI docs at `/docs` (when the engine is
serving), or [`AGENT-BOARD.md`](../../02-dev-docs/AGENT-BOARD.md).

## Files

- `board-watchdog.ps1` — Windows watchdog (polls `/health` every 15 s, restarts only its own child)
