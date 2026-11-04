@echo off
rem GWS Exporter page: double-click this file. Nothing needs to be installed first (Windows 10/11).
rem First start downloads a small setup tool (uv), then Python 3.11, then the program (a few minutes); later starts are quick.
rem Open launcher.py in Notepad to read everything it does.
cd /d "%~dp0"
call "%~dp0_uv.bat"
if errorlevel 1 (
  pause
  exit /b 1
)
"%~dp0tools\uv.exe" run --no-project --python 3.11 "%~dp0launcher.py" ui %*
if errorlevel 1 (
  echo.
  echo Something went wrong - see the message above.
  pause
)
