<#
.SYNOPSIS
  Watchdog for the Knowledge-Engine standalone Agent Board service (Windows).

.DESCRIPTION
  Starts the board service as a child process and polls its /health endpoint
  every CheckInterval seconds. When the child exits or stops answering, the
  watchdog stops *its own* child and starts a fresh one. It never touches a
  process it did not start: if another process already owns the port, the
  watchdog logs that and waits instead of killing it.

  The interpreter is resolved from the project virtualenv
  (engine\.venv\Scripts\python.exe, created by `uv sync --project engine`);
  when that is missing it falls back to `uv run`.

  The watchdog's own PID is written to <data dir>\watchdog.pid, where the
  data dir is $env:KE_DATA_DIR or %LOCALAPPDATA%\knowledge-engine.

  Run it in the foreground:
    pwsh -NoProfile -File scripts\agent-board\board-watchdog.ps1 -Port 11437
  Or hidden in the background:
    Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File','scripts\agent-board\board-watchdog.ps1'
  Stop it by killing the PID in watchdog.pid; the child service is stopped with it.

.PARAMETER Port
  Listening port (default 11437, or $env:BOARD_PORT when set).

.PARAMETER BindHost
  Interface to bind (default 127.0.0.1, or $env:BOARD_HOST when set).

.PARAMETER CheckInterval
  Seconds between health checks (default 15).
#>

param(
  [int]$Port = $(if ($env:BOARD_PORT) { [int]$env:BOARD_PORT } else { 11437 }),
  [string]$BindHost = $(if ($env:BOARD_HOST) { $env:BOARD_HOST } else { '127.0.0.1' }),
  [int]$CheckInterval = 15
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot  = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$EngineDir = Join-Path $RepoRoot 'engine'

function Write-Log([string]$Message, [string]$Color = 'Gray') {
  Write-Host ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) -ForegroundColor $Color
}

function Resolve-Launcher {
  $venvPython = Join-Path $EngineDir '.venv\Scripts\python.exe'
  if (Test-Path $venvPython) {
    return @{
      FilePath = $venvPython
      Args     = @('-m', 'knowledge_engine.agent_board.service', '--host', $BindHost, '--port', "$Port")
    }
  }
  $uv = Get-Command uv -ErrorAction SilentlyContinue
  if ($uv) {
    return @{
      FilePath = $uv.Source
      Args     = @('run', '--project', $EngineDir, 'knowledge-engine', 'board-serve', '--host', $BindHost, '--port', "$Port")
    }
  }
  throw "No interpreter found. Create the venv with 'uv sync --project engine' or install uv (https://docs.astral.sh/uv/)."
}

function Resolve-DataDir {
  if ($env:KE_DATA_DIR) { return $env:KE_DATA_DIR }
  return (Join-Path $env:LOCALAPPDATA 'knowledge-engine')
}

function Test-BoardAlive {
  try {
    $r = Invoke-RestMethod -Uri ("http://{0}:{1}/health" -f $BindHost, $Port) -TimeoutSec 5
    return ($r.ok -eq $true)
  } catch {
    return $false
  }
}

# PIDs listening on the port that are not our child. We never stop these.
function Get-ForeignListeners {
  $ownPid = if ($script:Child) { $script:Child.Id } else { -1 }
  try {
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique |
      Where-Object { $_ -ne $ownPid })
  } catch {
    return @()
  }
}

$script:Child = $null

function Start-Board {
  $launcher = Resolve-Launcher
  Write-Log ("starting agent-board on {0}:{1} via {2}" -f $BindHost, $Port, $launcher.FilePath)
  $script:Child = Start-Process -FilePath $launcher.FilePath -ArgumentList $launcher.Args `
    -WorkingDirectory $EngineDir -WindowStyle Hidden -PassThru
  Write-Log ("child PID {0} started" -f $script:Child.Id)
}

function Stop-Board {
  if ($script:Child -and -not $script:Child.HasExited) {
    Write-Log ("stopping child PID {0}" -f $script:Child.Id)
    Stop-Process -Id $script:Child.Id -Force -ErrorAction SilentlyContinue
    $script:Child.WaitForExit(5000) | Out-Null
  }
  $script:Child = $null
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Knowledge-Engine Board Watchdog" -ForegroundColor Cyan
Write-Host ("  Bind     : {0}:{1}" -f $BindHost, $Port) -ForegroundColor Cyan
Write-Host ("  Interval : {0}s" -f $CheckInterval) -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$DataDir = Resolve-DataDir
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$PidFile = Join-Path $DataDir 'watchdog.pid'
$PID | Out-File -FilePath $PidFile -Force -Encoding ascii
Write-Log ("watchdog PID {0} -> {1}" -f $PID, $PidFile)

try {
  while ($true) {
    $childDown = (-not $script:Child) -or $script:Child.HasExited
    if ($childDown -or -not (Test-BoardAlive)) {
      $foreign = Get-ForeignListeners
      if ($foreign.Count -gt 0) {
        Write-Log ("port {0} is held by PID(s) {1}, which this watchdog did not start; waiting" -f $Port, ($foreign -join ', ')) 'Yellow'
      } else {
        if (-not $childDown) { Write-Log "board not answering /health; restarting" 'Yellow' }
        elseif ($script:Child) { Write-Log "child exited; restarting" 'Yellow' }
        Stop-Board
        Start-Board
        Start-Sleep -Seconds 3
        if (Test-BoardAlive) {
          Write-Log "board is UP" 'Green'
        } else {
          Write-Log "board did not answer /health yet" 'Red'
        }
      }
    }
    Start-Sleep -Seconds $CheckInterval
  }
} finally {
  Stop-Board
  Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}
