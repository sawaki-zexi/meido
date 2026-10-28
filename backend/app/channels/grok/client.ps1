param([Parameter(Mandatory = $true)][string]$Connection)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Utility') -ErrorAction Stop
    Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Management') -ErrorAction Stop
    $settings = Get-Content -LiteralPath $Connection -Raw -Encoding UTF8 | ConvertFrom-Json
    $callback = [Uri]$settings.callback_url
    if ($settings.protocol -ne 'meido-grok/1' -or $callback.Scheme -ne 'http' -or $callback.Host -ne '127.0.0.1' -or $callback.UserInfo -or $callback.Query -or $callback.Fragment) {
        throw 'Invalid callback configuration'
    }
    $payload = [Console]::In.ReadToEnd()
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    if ($bytes.Length -gt 65536) { throw 'Message too large' }
    $request = [Net.HttpWebRequest]::Create($callback.AbsoluteUri + 'messages')
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Method = 'POST'
    $request.ContentType = 'application/json; charset=utf-8'
    $request.Headers['Authorization'] = 'Bearer ' + $settings.token
    $request.Timeout = 15000
    $request.ReadWriteTimeout = 15000
    $request.ContentLength = $bytes.Length
    $stream = $request.GetRequestStream()
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    $response = $request.GetResponse()
    try {
        if ([int]$response.StatusCode -notin @(200, 202)) { throw 'Callback not accepted' }
        $reader = [IO.StreamReader]::new($response.GetResponseStream(), [Text.Encoding]::UTF8)
        try { [Console]::Out.WriteLine($reader.ReadToEnd()) } finally { $reader.Dispose() }
    } finally { $response.Dispose() }
} catch {
    [Console]::Error.WriteLine('Meido callback failed or uncertain. Keep the original message_id; check connection status.')
    exit 1
}
