# update-vendor.ps1 — re-download and SHA384-verify locally vendored
# front-end dependencies for the Knowledge Engine dashboard.
#
# Usage:
#   pwsh ./scripts/update-vendor.ps1
#
# This script is *idempotent*. Run it after bumping a pinned version
# below, or to verify the on-disk bundles match the upstream artifact.

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

$repoRoot   = Split-Path -Parent $PSScriptRoot
$vendorRoot = Join-Path $repoRoot 'ui/vendor'

# Pinned versions + expected SHA384 (base64) of the upstream bundle.
$assets = @(
    @{
        Name       = 'tailwindcss'
        Version    = '3.4.16'
        Url        = 'https://cdn.tailwindcss.com/3.4.16'
        OutFile    = 'tailwindcss/3.4.16/tailwind.min.js'
        Sha384B64  = 'mS5Uq7sE90lgbBDN8xgf34ibEgbZo4gB3tfLY40ZRle+M188BQw8onzNHg6GUZaA'
    }
    @{
        Name       = 'alpinejs'
        Version    = '3.14.1'
        Url        = 'https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js'
        OutFile    = 'alpinejs/3.14.1/alpine.min.js'
        Sha384B64  = 'l8f0VcPi/M1iHPv8egOnY/15TDwqgbOR1anMIJWvU6nLRgZVLTLSaNqi/TOoT5Fh'
    }
)

$sha = [System.Security.Cryptography.SHA384]::Create()
$fail = $false

foreach ($a in $assets) {
    $dest = Join-Path $vendorRoot $a.OutFile
    $parent = Split-Path -Parent $dest
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }

    Write-Host ("[fetch] {0}@{1}" -f $a.Name, $a.Version)
    $resp = Invoke-WebRequest -Uri $a.Url -UseBasicParsing
    $bytes = $resp.RawContentStream.ToArray()

    $actual = [Convert]::ToBase64String($sha.ComputeHash($bytes))
    if ($actual -ne $a.Sha384B64) {
        Write-Host ("  [FAIL] SHA384 mismatch") -ForegroundColor Red
        Write-Host ("    expected: sha384-{0}" -f $a.Sha384B64)
        Write-Host ("    actual:   sha384-{0}" -f $actual)
        $fail = $true
        continue
    }

    [System.IO.File]::WriteAllBytes($dest, $bytes)
    Write-Host ("  [ok] {0} bytes -> {1}" -f $bytes.Length, $dest) -ForegroundColor Green
}

if ($fail) {
    Write-Host "`nOne or more vendored assets failed SHA384 verification." -ForegroundColor Red
    exit 1
}

Write-Host "`nVendor refresh complete."
