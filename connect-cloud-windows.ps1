param([switch]$Connect, [int]$Port = 8787)
$ErrorActionPreference = 'Stop'
$base = "http://127.0.0.1:$Port"
if ($Connect) {
    $secret = Read-Host 'Enter edge token encoded as hexadecimal (hidden)' -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try {
        $hex = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
        if ($hex -notmatch '^(?:[0-9a-fA-F]{2}){24,}$') { throw 'Invalid token encoding' }
        $bytes = New-Object byte[] ($hex.Length / 2)
        for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = [Convert]::ToByte($hex.Substring($i * 2, 2), 16) }
        $token = [Text.Encoding]::UTF8.GetString($bytes)
        $headers = @{Origin = $base}
        try {
            $session = Invoke-RestMethod "$base/api/collector/session" -TimeoutSec 10
            $headers['X-DCP-CSRF'] = $session.data.csrf_token
        } catch {
            if ([int]$_.Exception.Response.StatusCode -ne 401) { throw }
            $legacy = Invoke-RestMethod "$base/api/collector/cloud" -TimeoutSec 10
            if (-not $legacy.ok) { throw 'Local collector identity check failed' }
            Write-Host 'Legacy local collector API detected (no session endpoint).'
        }
        $body = @{cloud_url = 'https://43.134.164.108'; site_id = 'site-001'; token = $token} | ConvertTo-Json -Compress
        $result = Invoke-RestMethod "$base/api/collector/cloud" -Method Post -ContentType 'application/json' -Headers $headers -Body $body -TimeoutSec 60
        $result | ConvertTo-Json -Depth 12 -Compress
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
        $hex = $null; $bytes = $null; $token = $null; $body = $null
        $secret.Dispose()
    }
}
foreach ($endpoint in @('health', 'collector/cloud', 'collector/status', 'collector/config')) {
    Write-Host "ENDPOINT $endpoint"
    try { Invoke-RestMethod "$base/api/$endpoint" -TimeoutSec 15 | ConvertTo-Json -Depth 15 -Compress } catch { Write-Host $_.Exception.Message }
}
