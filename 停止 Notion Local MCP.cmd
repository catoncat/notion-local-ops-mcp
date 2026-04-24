@echo off

setlocal

chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%scripts\stop-mcp-manager.ps1"

if not exist "%SCRIPT%" (

  echo [ERROR] Script not found: %SCRIPT%

  pause

  exit /b 1

)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "& { [Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; . '%SCRIPT%' }"

set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (

  echo.

  echo [ERROR] Stop script exited with code %EXITCODE%

  pause

)

exit /b %EXITCODE%