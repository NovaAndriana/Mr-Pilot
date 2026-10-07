@echo off
REM Sekali jalan: buat virtualenv, install dependency, siapkan config
cd /d "%~dp0"
python -m venv .venv || (echo Python belum terinstall. Install dari python.org dulu. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist config.yaml copy config.example.yaml config.yaml
if not exist .env copy .env.example .env
echo.
echo Selesai. Isi file .env dan config.yaml, lalu jalankan run.bat
pause
