@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if exist "ai01_SmartVoyage\.venv\Scripts\python.exe" (
  "ai01_SmartVoyage\.venv\Scripts\python.exe" -m ai01_SmartVoyage.scripts.launch
) else (
  python -m ai01_SmartVoyage.scripts.launch
)
if errorlevel 1 pause
