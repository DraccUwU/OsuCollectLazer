@echo off
setlocal
cd /d "%~dp0"
set PY=python
where %PY% >nul 2>nul || set PY=py
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" || (
  echo Python 3.10+ is required but was not found on PATH.
  echo Install it from https://python.org and try again.
  pause
  exit /b 1
)
%PY% -m app.server %*
if errorlevel 1 pause
