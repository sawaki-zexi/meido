param(
    [Parameter(Mandatory=$true)][string]$CacheRoot,
    [string]$MsvcRoot = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.42.34433'
)
$ErrorActionPreference = 'Stop'
$cachePath = [IO.Path]::GetFullPath($CacheRoot)
New-Item -ItemType Directory -Path $cachePath -Force | Out-Null
$sourceLock = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\..\native\computer-use\source.json') | ConvertFrom-Json
function Get-VerifiedFile($Uri, $Destination, $Sha256) {
    if (-not (Test-Path -LiteralPath $Destination)) {
        Invoke-WebRequest -Uri $Uri -OutFile $Destination
    }
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Sha256) {
        throw "Fixed build dependency hash mismatch: $Destination"
    }
}
$sourcePath = Join-Path $cachePath 'cua-source'
if (-not (Test-Path -LiteralPath $sourcePath)) {
    & git clone --filter=blob:none --no-checkout $sourceLock.repository $sourcePath
    if ($LASTEXITCODE -ne 0) { throw 'CUA clone failed' }
    & git -C $sourcePath sparse-checkout set libs/cua-driver
    if ($LASTEXITCODE -ne 0) { throw 'CUA sparse checkout failed' }
    & git -C $sourcePath checkout --detach $sourceLock.commit
    if ($LASTEXITCODE -ne 0) { throw 'CUA fixed checkout failed' }
}
if ((& git -C $sourcePath rev-parse HEAD).Trim() -ne $sourceLock.commit) { throw 'Existing CUA cache has another commit; choose another cache' }
if ((& git -C $sourcePath rev-parse $sourceLock.tag).Trim() -ne $sourceLock.commit) { throw 'CUA tag identity mismatch' }
$env:RUSTUP_HOME = Join-Path $cachePath 'rustup'
$env:CARGO_HOME = Join-Path $cachePath 'cargo'
$rustupInit = Join-Path $cachePath 'rustup-init.exe'
Get-VerifiedFile $sourceLock.rustup.url $rustupInit $sourceLock.rustup.sha256
if (-not (Test-Path -LiteralPath (Join-Path $env:CARGO_HOME 'bin\rustup.exe'))) {
    & $rustupInit -y --no-modify-path --default-toolchain 1.97.1 --profile minimal --component rustfmt
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Rust installation failed' }
}
& (Join-Path $env:CARGO_HOME 'bin\rustup.exe') toolchain install 1.97.1 --profile minimal --component rustfmt --no-self-update
if ($LASTEXITCODE -ne 0) { throw 'Fixed Rust toolchain unavailable' }
if (-not (Test-Path -LiteralPath (Join-Path $MsvcRoot 'bin\Hostx64\x64\cl.exe'))) { throw 'MSVC 14.42.34433 must already be installed; prepare does not install system tools' }
$spectrePackage = Join-Path $cachePath 'spectre.vsix'
Get-VerifiedFile $sourceLock.spectreLibraries.url $spectrePackage $sourceLock.spectreLibraries.sha256
$spectreDirectory = Join-Path $cachePath 'msvc\lib\spectre\x64'
New-Item -ItemType Directory -Path $spectreDirectory -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($spectrePackage)
try {
    foreach ($entry in $archive.Entries) {
        if ($entry.Name.EndsWith('.lib')) {
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, (Join-Path $spectreDirectory $entry.Name), $true)
        }
    }
} finally { $archive.Dispose() }
# regorus locates Spectre CRT relative to cl.exe. Keep this private copy beside
# the extracted CRT; never modify the installed Visual Studio directories.
$compilerCache = Join-Path $cachePath 'msvc\bin\Hostx64\x64'
New-Item -ItemType Directory -Path $compilerCache -Force | Out-Null
Copy-Item -Path (Join-Path $MsvcRoot 'bin\Hostx64\x64\*') -Destination $compilerCache -Recurse -Force
Write-Output 'Fixed source/toolchain cache ready; build.ps1 performs patch and resource verification.'
