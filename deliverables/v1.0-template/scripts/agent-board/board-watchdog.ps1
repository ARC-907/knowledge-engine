<#
.SYNOPSIS
  Watchdog for the Knowledge-Engine standalone Agent Board service.

.DESCRIPTION
  Polls the board's /health endpoint every CheckInterval seconds. If the
  service is dead, kills any stale listener on the port and starts a new
  one. Mirrors the caprock board-watchdog.ps1 pattern so muscle memory
  carries over between projects.

.PARAMETER Port
  Listening port (default 11437; matches BOARD_PORT env var if set).

.PARAMETER CheckInterval
  Seconds between health checks (default 15).
#>

param(
  [int]$Port = $(if ($env:BOARD_PORT) { [int]$env:BOARD_PORT } else { 11437 }),
  [int]$CheckInterval = 15
)

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Test-BoardAlive {
  try {
    $r = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/health" -f $Port) -TimeoutSec 5
    return ($r.ok -eq $true)
  } catch {
    return $false
  }
}

function Start-Board {
  Write-Host ("[watchdog] starting agent-board on port {0}" -f $Port)
  $proc = Start-Process -FilePath "python" `
    -ArgumentList @("-m", "knowledge_engine.agent_board.service", "--port", "$Port") `
    -WindowStyle Hidden -PassThru -WorkingDirectory $ScriptDir
  Write-Host ("[watchdog] PID {0} started" -f $proc.Id)
  return $proc
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Knowledge-Engine Board Watchdog" -ForegroundColor Cyan
Write-Host ("  Port     : {0}" -f $Port) -ForegroundColor Cyan
Write-Host ("  Interval : {0}s" -f $CheckInterval) -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

$PidFile = Join-Path $ScriptDir "watchdog.pid"
$PID | Out-File -FilePath $PidFile -Force

while ($true) {
  if (-not (Test-BoardAlive)) {
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host ("[{0}] board down — restarting..." -f $ts)
    $existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($existing) {
      foreach ($conn in $existing) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
      }
      Start-Sleep -Seconds 2
    }
    Start-Board | Out-Null
    Start-Sleep -Seconds 3
    if (Test-BoardAlive) {
      Write-Host ("[{0}] board is UP" -f (Get-Date -Format 'HH:mm:ss'))
    } else {
      Write-Host ("[{0}] board failed to start" -f (Get-Date -Format 'HH:mm:ss')) `
        -ForegroundColor Red
    }
  }
  Start-Sleep -Seconds $CheckInterval
}
