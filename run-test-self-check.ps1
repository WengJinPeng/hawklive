[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$packageRoot = $PSScriptRoot
$python = Join-Path $packageRoot ".test-venv\Scripts\python.exe"

if ($env:OS -ne "Windows_NT") {
    throw "This self-check must run on Windows."
}
if (-not (Test-Path $python)) {
    throw "The test environment is missing. Run 01-INSTALL-TEST-DEPS.cmd first."
}

$requiredFiles = @(
    "dashboard_server.py",
    "dcp8001_collector.py",
    "public\collector.html",
    "public\index.html",
    "public\wallboard.html",
    "TEST-PACKAGE-GUIDE.md"
)
foreach ($relativePath in $requiredFiles) {
    if (-not (Test-Path (Join-Path $packageRoot $relativePath))) {
        throw "The test package is incomplete: $relativePath is missing."
    }
}

Push-Location $packageRoot
try {
    & $python -m compileall -q -f `
        alarm_config.py auth_service.py backup_scheduler.py cloud_backup.py cloud_sync.py collector_settings.py dashboard_server.py `
        dcp8001_collector.py device_discovery.py monitoring_service.py report_i18n.py `
        runtime_paths.py single_instance.py storage_maintenance.py
    if ($LASTEXITCODE -ne 0) {
        throw "Python compilation check failed."
    }

    & $python -m unittest -v `
        test_collector_settings.py `
        test_dcp8001_collector.py `
        test_diagnose_rtu_over_tcp.py `
        test_i18n.py `
        test_monitoring.py `
        test_runtime_foundation.py `
        test_storage_maintenance.py `
        test_wallboard_contract.py `
        test_windows_launcher.py
    if ($LASTEXITCODE -ne 0) {
        throw "Collector self-check failed."
    }
}
finally {
    Pop-Location
}

Write-Host "All local collector self-checks passed." -ForegroundColor Green
Write-Host "You can now run 03-START-DEMO.cmd or 04-START-DEVICE-TEST.cmd." -ForegroundColor Cyan
