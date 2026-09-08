@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-test-self-check.ps1"
if errorlevel 1 echo Self-check failed. Review the message above.
pause
