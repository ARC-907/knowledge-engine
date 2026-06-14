# Knowledge Engine - Windows install
# Creates a virtualenv in ./engine/.venv, installs the engine package editable.

$ErrorActionPreference = 'Stop'
Set-Location -Path (Split-Path $MyInvocation.MyCommand.Path -Parent)
Set-Location ..

if (-not (Test-Path 'engine\.venv')) {
    Write-Host "Creating virtualenv at engine\.venv..."
    python -m venv engine\.venv
}

& engine\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip --quiet
python -m pip install -e .\engine --quiet
python -m pip install fastapi uvicorn httpx pydantic watchdog pytest --quiet

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. .\engine\.venv\Scripts\Activate.ps1"
Write-Host "  2. knowledge-engine bootstrap"
Write-Host "  3. knowledge-engine reindex"
Write-Host "  4. .\scripts\serve.ps1"
