@echo off
rem GWS Exporter command line: run.bat --admin admin@domain.com --user person@domain.com --export-only --local-only
rem Nothing needs to be installed first (Windows 10/11). Open launcher.py in Notepad to read everything it does.
cd /d "%~dp0"
call "%~dp0_uv.bat"
if errorlevel 1 (
  pause
  exit /b 1
)
"%~dp0tools\uv.exe" run --no-project --python 3.11 "%~dp0launcher.py" cli %*
echo.
pause
