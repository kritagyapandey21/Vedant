@echo off
REM Pocket Option backend on http://localhost:8000 (uses the shared virtualenv in ..\quotex_lib\venv).
cd /d "%~dp0"
"%~dp0..\quotex_lib\venv\Scripts\python.exe" -W ignore app.py
