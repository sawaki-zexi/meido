[CmdletBinding()]
param(
    [string]$CodeRoot = (Get-Location).Path,
    [string]$SettingsPath = (Join-Path $PSScriptRoot 'local-settings.json'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not (Test-Path -LiteralPath (Join-Path $CodeRoot 'backend/app/server.py') -PathType Leaf)) {
    throw "Not a Meido development checkout with the service runner: $CodeRoot"
}
$launcher = Join-Path $PSScriptRoot 'start-dev.mjs'
$arguments = @($launcher, '--code-root', $CodeRoot)
if ($CheckOnly) { $arguments += '--check' }
if (-not $env:MEIDO_DEV_PYTHON) { $env:MEIDO_DEV_PYTHON = $settings.python }
& $settings.node @arguments
if ($LASTEXITCODE -ne 0) { throw 'The development server did not complete successfully.' }
