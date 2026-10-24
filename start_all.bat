@echo off
REM Starts the whole site locally. Open http://localhost:8080
REM   start_all.bat            -> everything
REM   start_all.bat noquotex   -> everything except the Quotex backend (e.g. while Quotex is rate-limiting)
set PY=%~dp0backend\quotex_lib\venv\Scripts\python.exe
start "AUROX backend :8000" /D "%~dp0backend\pocketoption" cmd /k "set ALLOWED_ORIGINS=http://localhost:8080,http://127.0.0.1:8080&& "%PY%" -W ignore app.py"
if /I not "%1"=="noquotex" start "AUROX AI Quotex backend :8002" /D "%~dp0backend\quotex" cmd /k "set PORT=8002&& "%PY%" -W ignore app.py"
start "Site gateway :8080" /D "%~dp0backend\gateway" cmd /k ""%PY%" dev_gateway.py 8080"
echo Started. Open http://localhost:8080
