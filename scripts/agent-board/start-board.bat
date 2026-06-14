@echo off
REM Knowledge-Engine — Agent Board standalone launcher
REM Starts the watchdog in a hidden PowerShell window. The watchdog restarts
REM the board service if it dies. Override the port with BOARD_PORT before
REM invoking.

setlocal
set "SCRIPT_DIR=%~dp0"
if "%BOARD_PORT%"=="" set BOARD_PORT=11437

powershell -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass ^
  -File "%SCRIPT_DIR%board-watchdog.ps1" -Port %BOARD_PORT%

echo Knowledge-Engine board watchdog started on port %BOARD_PORT%.
echo To stop: kill the python.exe process bound to that port, or run:
echo   Get-NetTCPConnection -LocalPort %BOARD_PORT% -State Listen ^| ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
endlocal
