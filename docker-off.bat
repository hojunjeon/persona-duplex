@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell was not found.
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\persona-duplex.ps1" -Action docker-off
set "DOCKER_EXIT_CODE=%ERRORLEVEL%"

if not "%DOCKER_EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Docker Desktop stop failed with code %DOCKER_EXIT_CODE%.
  pause
)

endlocal & exit /b %DOCKER_EXIT_CODE%
