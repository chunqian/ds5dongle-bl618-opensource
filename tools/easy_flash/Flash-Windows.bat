@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\flash-windows.ps1" %*
set "RESULT=%ERRORLEVEL%"
if /i not "%~1"=="-CheckOnly" pause
exit /b %RESULT%
