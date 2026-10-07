@echo off
REM MR Pilot (Windows + Docker Desktop). Contoh: setup.bat  |  setup.bat -Server  |  setup.bat ci  |  setup.bat logs
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if errorlevel 1 pause
