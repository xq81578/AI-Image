@echo off
setlocal
cd /d "%~dp0"
"C:\Python313\python.exe" "%~dp0server.py"
if errorlevel 1 pause
endlocal
