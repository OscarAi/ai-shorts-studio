@echo off
rem Double-click this file in File Explorer to start Shorts Studio and open it in your browser.
rem (In the VS Code terminal: .\run_app.bat) The work is done by scripts\run_app.ps1;
rem every step is shown here and written to data\logs\run_app.log.
title Shorts Studio
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_app.ps1"
echo.
echo This window can be closed now.
pause
