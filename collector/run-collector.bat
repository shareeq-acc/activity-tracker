@echo off
REM Run the collector in a visible console. Ctrl+C stops it cleanly, closing
REM and flushing the open segment. Configuration comes from ..\.env
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not on PATH. Install Python 3.10+ from python.org and retry.
  pause
  exit /b 1
)

python collector.py %*
pause
