[CmdletBinding()]
param(
    [string]$PythonPath = "py"
)

$ErrorActionPreference = "Stop"
$serviceName = "HawkHiveDCP8001Collector"
$serviceScript = Join-Path $PSScriptRoot "windows_service.py"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请以管理员身份运行 PowerShell。"
}

$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne "Stopped") {
    Stop-Service -Name $serviceName
    $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(30))
}
& $PythonPath $serviceScript remove
if ($LASTEXITCODE -ne 0) {
    throw "Windows 服务卸载失败。"
}

Write-Host "服务已卸载。运行数据未删除，可在以下目录恢复："
Write-Host (Join-Path $env:ProgramData "HawkHive\DCP8001")
