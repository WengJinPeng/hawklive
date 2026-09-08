[CmdletBinding()]
param(
    [ValidateSet("Demo", "Device")]
    [string]$Mode = "Demo"
)

$ErrorActionPreference = "Stop"
$packageRoot = $PSScriptRoot
$python = Join-Path $packageRoot ".test-venv\Scripts\python.exe"
$modeName = $Mode.ToLowerInvariant()
$dataDirectory = Join-Path $env:LOCALAPPDATA "HawkHive\DPC8001-$Mode-Test"
$database = Join-Path $dataDirectory "dashboard_data.sqlite3"
$serverProcess = $null

if ($env:OS -ne "Windows_NT") {
    throw "This test launcher must run on Windows."
}
if (-not (Test-Path $python)) {
    throw "The test environment is missing. Run 01-INSTALL-TEST-DEPS.cmd first."
}

New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
$env:DCP_DATA_DIR = $dataDirectory
$env:DCP_DEMO_MODE = if ($Mode -eq "Demo") { "1" } else { "0" }
$env:DCP_POLL_SECONDS = if ($Mode -eq "Demo") { "2" } else { "10" }
$env:DCP_RECORD_SECONDS = if ($Mode -eq "Demo") { "10" } else { "120" }

if (-not (Test-Path $database)) {
    $env:DASHBOARD_INITIAL_USERNAME = "tester"
    $securePassword = Read-Host "Set the initial tester password (at least 12 characters)" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try {
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ($plainPassword.Length -lt 12) {
            throw "The tester password must contain at least 12 characters."
        }
        $env:DASHBOARD_INITIAL_PASSWORD = $plainPassword
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

Push-Location $packageRoot
try {
    $serverProcess = Start-Process -FilePath $python `
        -ArgumentList @("dashboard_server.py", "--bind", "127.0.0.1", "--port", "8787") `
        -WorkingDirectory $packageRoot -NoNewWindow -PassThru

    Start-Sleep -Milliseconds 700
    if ($serverProcess.HasExited) {
        throw "The collector did not start. Port 8787 may already be in use; review the message above."
    }

    $healthy = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:8787/api/health" -TimeoutSec 2
            if ($response.ok) {
                $healthy = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $healthy) {
        throw "The collector health check did not pass within 10 seconds."
    }

    $url = "http://127.0.0.1:8787/collector.html"
    Start-Process $url
    Write-Host "DPC8001-G $modeName test is running." -ForegroundColor Green
    Write-Host "Console: $url"
    Write-Host "Test data: $dataDirectory"
    Write-Host "Close this window or press Ctrl+C to stop the test."
    Wait-Process -Id $serverProcess.Id
    $serverProcess.Refresh()
    if ($serverProcess.ExitCode -ne 0) {
        throw "The collector exited unexpectedly with code $($serverProcess.ExitCode)."
    }
}
finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
    }
    $env:DASHBOARD_INITIAL_PASSWORD = $null
    $plainPassword = $null
    $securePassword = $null
    Pop-Location
}
