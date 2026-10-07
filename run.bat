@echo off
REM Jalankan MR Pilot (biarkan jendela ini terbuka, atau pakai Task Scheduler)
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m mr_pilot --config config.yaml %*
