@echo off
rem Helper for run.bat / run-ui.bat: makes sure tools\uv.exe exists (uv installs Python 3.11 and the packages, so nothing has to be installed first).
set "UV=%~dp0tools\uv.exe"
if exist "%UV%" goto :eof
echo First start: downloading the setup tool (uv, about 20 MB) from github.com ...
set "ARCH=x86_64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=aarch64"
if not exist "%~dp0tools" mkdir "%~dp0tools"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $z='%~dp0tools\uv.zip'; Invoke-WebRequest -UseBasicParsing 'https://github.com/astral-sh/uv/releases/download/0.12.18/uv-%ARCH%-pc-windows-msvc.zip' -OutFile $z; Expand-Archive -Force $z '%~dp0tools'; Remove-Item $z"
if not exist "%UV%" (
  echo.
  echo Could not download the setup tool. Check the internet connection, a VPN or proxy that blocks github.com, and try again.
  exit /b 1
)
