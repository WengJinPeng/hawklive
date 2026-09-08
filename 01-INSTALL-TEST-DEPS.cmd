@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-test-dependencies.ps1"
if errorlevel 1 echo Test dependency setup failed. Review the message above.
pause
