[CmdletBinding()]
param(
    [string]$PythonPath = "py"
)

$ErrorActionPreference = "Stop"
$serviceName = "HawkHiveDCP8001Collector"
$dataDirectory = Join-Path $env:ProgramData "HawkHive\DCP8001"
$serviceScript = Join-Path $PSScriptRoot "windows_service.py"
$programFilesRoot = [IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') + '\'
$sourceRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请以管理员身份运行 PowerShell。"
}
if (-not $sourceRoot.StartsWith($programFilesRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "请先将发布包解压到 C:\Program Files\HawkHive\DCP8001Collector，再从该目录运行安装脚本。"
}

New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
& icacls.exe $dataDirectory /inheritance:r /grant:r `
    "*S-1-5-18:(OI)(CI)F" "*S-1-5-19:(OI)(CI)M" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "无法设置采集器数据目录 ACL。"
}

& $PythonPath -m pip install -r (Join-Path $PSScriptRoot "requirements-windows.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Python 依赖安装失败。"
}

# pywin32 upstream requires this for machine-wide service hosting. This
# intentionally assumes a system Python, not a per-user virtual environment.
& $PythonPath -m pywin32_postinstall -install
if ($LASTEXITCODE -ne 0) {
    throw "pywin32 系统级服务组件注册失败。"
}

$existingService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existingService -and $existingService.Status -ne "Stopped") {
    Stop-Service -Name $serviceName
    $existingService.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}
$serviceAction = if ($existingService) { "update" } else { "install" }
& $PythonPath $serviceScript --username "NT AUTHORITY\LocalService" --startup delayed $serviceAction
if ($LASTEXITCODE -ne 0) {
    throw "Windows 服务安装或更新失败。"
}

& sc.exe failure $serviceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
& sc.exe failureflag $serviceName 1 | Out-Null
& sc.exe description $serviceName "DCP8001 车间数据采集与断网上传服务" | Out-Null
& sc.exe start $serviceName | Out-Null
Start-Sleep -Seconds 2
$service = Get-Service -Name $serviceName
if ($service.Status -ne "Running") {
    throw "服务已注册，但未能进入 Running。请检查 Windows Application 事件日志。"
}

Write-Host "服务已安装并启动：$serviceName"
Write-Host "本机配置入口：http://127.0.0.1:8787"
Write-Host "运行数据目录：$dataDirectory"
