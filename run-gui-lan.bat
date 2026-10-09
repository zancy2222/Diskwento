@echo off
REM Opens the GUI to your whole local network so a phone can reach it.
REM Only do this on a network you trust: anyone on the same wifi can open
REM the page and audit receipts through your machine.

cd /d "%~dp0"

set PY=py
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python

echo ============================================================
echo  Tama ba ang Diskwento?  --  GUI on the local network
echo ============================================================
echo.
echo  On your phone, open:  http://YOUR-PC-IP:8765/
echo  Your IP addresses:
echo.
ipconfig | findstr /C:"IPv4"
echo.
echo  NOTE: the live camera needs HTTPS or localhost, so on a phone
echo  use "Pumili ng litrato" -- it opens the camera app anyway.
echo.

%PY% -m diskwento serve --lan --no-browser
pause
