# Knowledge Engine - serve dashboard + API
$ErrorActionPreference = 'Stop'
Set-Location -Path (Split-Path $MyInvocation.MyCommand.Path -Parent)
Set-Location ..

$root = Get-Location
$env:KE_CORPUS_ROOT = (Join-Path $root 'corpus')
$env:KE_DATA_DIR = (Join-Path $root 'engine\data')
$env:KE_REGISTRY_PATH = (Join-Path $root 'corpus\registry.json')

& engine\.venv\Scripts\Activate.ps1

$port = if ($env:KE_PORT) { $env:KE_PORT } else { 9210 }
Write-Host "Knowledge Engine serving at http://127.0.0.1:$port" -ForegroundColor Green
Write-Host "  - Dashboard: http://127.0.0.1:$port/ui/"
Write-Host "  - API docs:  http://127.0.0.1:$port/docs"
Write-Host ""
uvicorn knowledge_engine.app:create_app --factory --host 127.0.0.1 --port $port
