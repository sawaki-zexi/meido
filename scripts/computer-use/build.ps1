param(
    [Parameter(Mandatory=$true)][string]$CacheRoot,
    [Parameter(Mandatory=$true)][string]$ResourceRoot,
    [string]$MsvcRoot = 'C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.42.34433',
    [string]$SdkRoot = 'C:\Program Files (x86)\Windows Kits\10'
)
$ErrorActionPreference = 'Stop'
$cachePath = (Resolve-Path -LiteralPath $CacheRoot).Path
$sourcePath = Join-Path $cachePath 'cua-source'
$sourceCommit = (& git -C $sourcePath rev-parse HEAD).Trim()
if ($sourceCommit -ne 'fc188250b4ca8549b8e61f937fdb1fb560770e86') { throw 'Fixed CUA source commit mismatch' }
$patchPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\native\computer-use\patches\0001-meido-windows-profile.patch'))
& git -C $sourcePath apply --reverse --check $patchPath 2>$null
if ($LASTEXITCODE -ne 0) {
    & git -C $sourcePath apply --check $patchPath
    if ($LASTEXITCODE -ne 0) { throw 'CUA patch does not apply to fixed source' }
    & git -C $sourcePath apply $patchPath
    if ($LASTEXITCODE -ne 0) { throw 'CUA patch application failed' }
}
$runtimeLock = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\..\backend\app\computer_use\runtime.json') | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $patchPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeLock.patchSha256) { throw 'Native patch is not the verified release patch' }
$diffPath = Join-Path $cachePath 'actual-driver.patch'
& git -C $sourcePath diff --no-ext-diff --binary "--output=$diffPath" HEAD -- libs/cua-driver/rust
if ($LASTEXITCODE -ne 0 -or (Get-FileHash -LiteralPath $diffPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeLock.patchSha256) { throw 'Source includes changes beyond the fixed patch' }
if (@(& git -C $sourcePath ls-files --others --exclude-standard -- libs/cua-driver/rust).Count) { throw 'Untracked native source is not allowed' }
$msvcPath = $MsvcRoot
$sdkPath = $SdkRoot
# These changes affect this build process and its children only.
$env:RUSTUP_HOME = Join-Path $cachePath 'rustup'
$env:CARGO_HOME = Join-Path $cachePath 'cargo'
$env:VCINSTALLDIR = [IO.Path]::GetFullPath((Join-Path $MsvcRoot '..\..\..')) + '\'
$env:VSCMD_ARG_TGT_ARCH = 'x64'
$env:LIB = (Join-Path $cachePath 'msvc\lib\spectre\x64') + ';' + $msvcPath + '\lib\x64;' + $sdkPath + '\Lib\10.0.22621.0\ucrt\x64;' + $sdkPath + '\Lib\10.0.22621.0\um\x64'
$env:INCLUDE = $msvcPath + '\include;' + $sdkPath + '\Include\10.0.22621.0\ucrt;' + $sdkPath + '\Include\10.0.22621.0\shared;' + $sdkPath + '\Include\10.0.22621.0\um;' + $sdkPath + '\Include\10.0.22621.0\winrt'
$env:PATH = (Join-Path $cachePath 'msvc\bin\Hostx64\x64') + ';' + $sdkPath + '\bin\10.0.22621.0\x64;' + (Join-Path $cachePath 'cargo\bin') + ';' + $env:PATH
Push-Location (Join-Path $sourcePath 'libs\cua-driver\rust')
try {
    & (Join-Path $cachePath 'cargo\bin\cargo.exe') build --release --locked -p cua-driver
    if ($LASTEXITCODE -ne 0) { throw 'Fixed driver build failed' }
    $builtPath = Join-Path (Get-Location) 'target\release\cua-driver.exe'
    if ((Get-FileHash -LiteralPath $builtPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $runtimeLock.sha256) {
        throw 'New build differs from the accepted release. Run native acceptance and deliberately update the release lock before packaging; it is not automatically trusted.'
    }
    $manifestPath = Join-Path $cachePath 'driver-manifest.xml'
    & (Join-Path $sdkPath 'bin\10.0.22621.0\x64\mt.exe') "-inputresource:$builtPath;#1" "-out:$manifestPath"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect built PE manifest' }
    [xml]$embeddedManifest = Get-Content -Raw -LiteralPath $manifestPath
    $execution = $embeddedManifest.SelectSingleNode('//*[local-name()="requestedExecutionLevel"]')
    if ($execution.level -ne 'asInvoker' -or $execution.uiAccess -ne 'false') { throw 'Driver requires unexpected privileges' }
    $outputPath = [IO.Path]::GetFullPath((Join-Path $ResourceRoot 'native\computer-use'))
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
    Copy-Item -LiteralPath $builtPath -Destination (Join-Path $outputPath 'cua-driver.exe')
    $manifest = [ordered]@{
        profile = 'meido-windows-169-v1'; upstreamCommit = $sourceCommit
        patchSha256 = (Get-FileHash -LiteralPath $patchPath -Algorithm SHA256).Hash.ToLowerInvariant()
        rust = '1.97.1'; msvc = '14.42.34433'; sdk = '10.0.22621.0'; target = 'x86_64-pc-windows-msvc'
        executionLevel = 'asInvoker'; uiAccess = $false
        sha256 = (Get-FileHash -LiteralPath $builtPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    [IO.File]::WriteAllText((Join-Path $outputPath 'manifest.json'), ($manifest | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '..\..\native\computer-use\LICENSE-CUA.txt') -Destination $outputPath
    $manifest | ConvertTo-Json
} finally { Pop-Location }
