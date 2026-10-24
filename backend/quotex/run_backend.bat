@echo off
REM Quotex backend on http://localhost:8002 (uses the shared virtualenv in ..\quotex_lib\venv).
cd /d "%~dp0"
set PORT=8002
"%~dp0..\quotex_lib\venv\Scripts\python.exe" -W ignore app.py
