@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem Double-click launcher. Pass a mode as the first argument when needed.
rem Examples: persona-duplex.bat balanced | persona-duplex.bat accuracy
set "PERSONA_MODE=%~1"
if not defined PERSONA_MODE set "PERSONA_MODE=balanced"

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell was not found.
  exit /b 1
)

echo.
echo Persona Duplex mode: %PERSONA_MODE%
echo Press Ctrl+C to stop all Docker services started by this launcher.
echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0persona-duplex.ps1" -Action run -Mode "%PERSONA_MODE%"
set "PERSONA_EXIT_CODE=%ERRORLEVEL%"

if not "%PERSONA_EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Persona Duplex exited with code %PERSONA_EXIT_CODE%.
  pause
)

endlocal & exit /b %PERSONA_EXIT_CODE%
