[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".build-venv"
$python = Join-Path $venv "Scripts\python.exe"
$outputRoot = Join-Path $root "dist\windows"
$oneDirOutput = Join-Path $outputRoot "onedir"
$oneFileOutput = Join-Path $outputRoot "onefile"
$releaseOutput = Join-Path $root "release"
$wheelhouse = Join-Path $root "windows-build-wheels"

if ($env:OS -ne "Windows_NT") {
    throw "Windows EXE must be built on Windows."
}

$launcher = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $launcher) {
    throw "Python 3.10 or newer is required on the build computer."
}
$version = & py.exe -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or [version]$version -lt [version]"3.10") {
    throw "Python 3.10 or newer is required on the build computer."
}

if (-not (Test-Path $python)) {
    & py.exe -3 -m venv $venv
}
if (Test-Path $wheelhouse) {
    & $python -m pip install --disable-pip-version-check --no-index `
        --find-links $wheelhouse -r (Join-Path $root "requirements-build-windows.txt")
}
else {
    & $python -m pip install --disable-pip-version-check --upgrade pip
    & $python -m pip install --disable-pip-version-check `
        -r (Join-Path $root "requirements-build-windows.txt")
}
if ($LASTEXITCODE -ne 0) {
    throw "Build dependency installation failed."
}
& $python -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "Build dependency verification failed."
}

Push-Location $root
try {
    & $python -m PyInstaller --noconfirm --clean --onedir `
        --name "HawkHive-DPC8001-Collector" `
        --distpath $oneDirOutput --workpath (Join-Path $root "build\pyinstaller-onedir") `
        --add-data "public:public" `
        "windows_launcher.py"
    if ($LASTEXITCODE -ne 0) { throw "One-folder EXE build failed." }

    & $python -m PyInstaller --noconfirm --clean --onefile `
        --name "HawkHive-DPC8001-Collector" `
        --distpath $oneFileOutput --workpath (Join-Path $root "build\pyinstaller-onefile") `
        --add-data "public:public" `
        "windows_launcher.py"
    if ($LASTEXITCODE -ne 0) { throw "Single-file EXE build failed." }

    $exe = Join-Path $oneFileOutput "HawkHive-DPC8001-Collector.exe"
    New-Item -ItemType Directory -Force -Path $releaseOutput | Out-Null
    Copy-Item -Force $exe (Join-Path $releaseOutput "HawkHive-DPC8001-Collector.exe")
    $digest = (Get-FileHash -Algorithm SHA256 $exe).Hash.ToLowerInvariant()
    Set-Content -Encoding ASCII -Path "$exe.sha256" `
        -Value "$digest  HawkHive-DPC8001-Collector.exe"
    Set-Content -Encoding ASCII `
        -Path (Join-Path $releaseOutput "HawkHive-DPC8001-Collector.exe.sha256") `
        -Value "$digest  HawkHive-DPC8001-Collector.exe"
    Write-Host "Windows EXE build complete: $exe" -ForegroundColor Green
    Write-Host "SHA-256: $digest"
}
finally {
    Pop-Location
}
