@echo off
rem Build the import helper. The app needs it: maps and collection entries both go
rem straight into lazer's files + database through this binary (there is no other path).
rem Requires the .NET 10 SDK.
setlocal
cd /d "%~dp0"

where dotnet >nul 2>nul
if errorlevel 1 (
  echo dotnet was not found on PATH - install the .NET 10 SDK first:
  echo   https://dotnet.microsoft.com/download
  exit /b 1
)

dotnet build -c Release LazerDb
if errorlevel 1 (
  echo.
  echo Build failed.
  exit /b 1
)

echo.
echo Built. The app finds it automatically; restart the app if it was already running.
