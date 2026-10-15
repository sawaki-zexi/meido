[CmdletBinding()]
param(
    [ValidateSet('Backend', 'Frontend', 'Backup')]
    [string]$Service = 'Backend',
    [string]$SettingsPath = (Join-Path $PSScriptRoot 'local-settings.json'),
    [string]$CodeRoot,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
function Test-AbsolutePath([string]$Value) {
    if (-not $Value) { return $false }
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        # IsPathRooted also accepts C:folder and \folder on Windows.
        # Match fully qualified drive or UNC paths, including PowerShell 5.1.
        return $Value -match '^(?:[A-Za-z]:[\\/]|[\\/]{2}[^\\/]+[\\/][^\\/]+(?:[\\/]|$))'
    }
    return [IO.Path]::IsPathRooted($Value)
}

if (-not (Test-AbsolutePath $SettingsPath)) {
    throw 'SettingsPath must be an absolute path to the saved local settings.'
}
if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
    throw "Local settings were not found: $SettingsPath. Copy and edit settings.example.json first."
}
$settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $CodeRoot) { $CodeRoot = $settings.codeRoot }

foreach ($field in @('projectRoot', 'dataDir', 'rolesDir', 'ownerKnowledgeDir', 'browserUseDataDir', 'backupRoot', 'python')) {
    $value = [string]$settings.$field
    if (-not (Test-AbsolutePath $value)) {
        throw "The local setting '$field' must be an absolute path."
    }
}
foreach ($directory in @($settings.dataDir, $settings.rolesDir, $settings.ownerKnowledgeDir)) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Saved data directory is missing: $directory. Select the existing data before starting."
    }
}
if (-not (Test-Path -LiteralPath $settings.python -PathType Leaf)) {
    throw "Python was not found: $($settings.python)"
}
foreach ($field in @('apiPort', 'webPort')) {
    $portValue = [int]$settings.$field
    if ($portValue -lt 1 -or $portValue -gt 65535) { throw "Invalid port in '$field'." }
}
if (-not $settings.apiHost) { throw 'apiHost must specify the backend listening address.' }

if ($Service -ne 'Backup') {
    if (-not (Test-AbsolutePath $CodeRoot) -or
        -not (Test-Path -LiteralPath (Join-Path $CodeRoot 'backend/app/main.py') -PathType Leaf)) {
        throw "The selected source checkout is unavailable or not absolute: $CodeRoot"
    }
    if ($Service -eq 'Frontend' -and (
        -not (Test-AbsolutePath $settings.node) -or
        -not (Test-Path -LiteralPath $settings.node -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $CodeRoot 'node_modules/vite/bin/vite.js') -PathType Leaf)
    )) {
        throw 'The Node executable or frontend dependencies are unavailable.'
    }
}

$backendUrl = "http://$($settings.apiHost):$($settings.apiPort)"
$frontendUrl = "http://127.0.0.1:$($settings.webPort)/app"
if ($CheckOnly) {
    [pscustomobject]@{
        Service = $Service
        CodeRoot = $CodeRoot
        DataDir = $settings.dataDir
        RolesDir = $settings.rolesDir
        OwnerKnowledgeDir = $settings.ownerKnowledgeDir
        BackendUrl = $backendUrl
        FrontendUrl = $frontendUrl
    } | ConvertTo-Json -Compress
    return
}

if ($Service -eq 'Backup') {
    & $settings.python (Join-Path $PSScriptRoot 'backup-meido-data.py') --settings $SettingsPath
    if ($LASTEXITCODE -ne 0) { throw 'The data backup did not complete.' }
    return
}

# All source checkouts use the same saved data paths from this local file.
$env:MEIDO_DATA_DIR = $settings.dataDir
$env:MEIDO_ROLES_DIR = $settings.rolesDir
$env:MEIDO_PROJECT_ROOT = $settings.projectRoot
$env:MEIDO_OWNER_KNOWLEDGE_DATA_DIR = $settings.ownerKnowledgeDir
$env:MEIDO_BROWSER_USE_DATA_DIR = $settings.browserUseDataDir
$env:MEIDO_WEB_PORT = [string]$settings.webPort
$env:MEIDO_API_ORIGIN = $backendUrl

$port = if ($Service -eq 'Backend') { $settings.apiPort } else { $settings.webPort }
$url = if ($Service -eq 'Backend') { "$backendUrl/api/health" } else { $frontendUrl }
$listener = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
if ($listener.Count -gt 0) {
    $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
    if ($response.StatusCode -ne 200) { throw "The existing server is not ready: $url" }
    Write-Host "An existing server is responding at $url (PID $($listener[0].OwningProcess))."
    Write-Host 'Keep using it, or stop it in its original terminal before starting another checkout.'
    return
}

Push-Location -LiteralPath $CodeRoot
try {
    if ($Service -eq 'Backend') {
        & $settings.python -m uvicorn backend.app.main:app --host $settings.apiHost --port $settings.apiPort
    } else {
        & $settings.node (Join-Path $CodeRoot 'node_modules/vite/bin/vite.js') --host 127.0.0.1 --port $settings.webPort
    }
    if ($LASTEXITCODE -ne 0) { throw "$Service exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
