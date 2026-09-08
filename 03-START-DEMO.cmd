@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-test-package.ps1" -Mode Demo
if errorlevel 1 echo Demo startup failed. Review the message above.
pause
