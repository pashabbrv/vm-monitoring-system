@echo off
set "ROOT=%~dp0.."
cd /d "%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0agent-windows.ps1" %*
