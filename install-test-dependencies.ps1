[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$packageRoot = $PSScriptRoot
$venvDirectory = Join-Path $packageRoot ".test-venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"

if ($env:OS -ne "Windows_NT") {
    throw "This test package must be prepared on Windows."
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $launcher) {
    throw "Python Launcher was not found. Install 64-bit Python 3.10 or later, including the py launcher."
}

& $launcher.Source -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 2)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.10 or later is required."
}

if (-not (Test-Path $venvPython)) {
    & $launcher.Source -3 -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the isolated test environment."
    }
}

& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $packageRoot "requirements-test-windows.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install test dependencies. Check Internet access or the corporate Python package mirror."
}

& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency consistency check failed."
}

Write-Host "Test environment is ready: $venvPython" -ForegroundColor Green
Write-Host "Next: run 02-RUN-SELF-CHECK.cmd" -ForegroundColor Cyan
