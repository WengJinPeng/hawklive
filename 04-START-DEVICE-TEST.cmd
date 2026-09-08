@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-test-package.ps1" -Mode Device
if errorlevel 1 echo Device test startup failed. Review the message above.
pause
