@echo off
REM Double-click this to open the GUI in your browser.
REM ASCII-only on purpose: a .bat with non-ASCII text misbehaves under the
REM legacy console code pages. The web page itself shows the peso sign fine.

cd /d "%~dp0"

set PY=py
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 goto nopython

echo ============================================================
echo  Tama ba ang Diskwento?  --  GUI
echo ============================================================
echo.
echo  Opening http://localhost:8765/ in your browser.
echo  Leave this window open while you use the app.
echo  Press Ctrl+C here to stop it.
echo.
echo  Tip: run-gui-lan.bat instead to open it on your phone.
echo.

%PY% -m diskwento serve
goto end

:nopython
echo.
echo  Python was not found on your PATH.
echo.
echo  Install Python 3.9 or newer from:
echo      https://www.python.org/downloads/
echo.
echo  IMPORTANT: tick "Add python.exe to PATH" in the installer,
echo  then close this window and run the file again.
echo.
pause
exit /b 1

:end
pause
