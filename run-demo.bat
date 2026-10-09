@echo off
REM Double-click this to run the whole demo on Windows.
REM Deliberately ASCII-only: a .bat with non-ASCII text misbehaves under the
REM legacy console code pages. The Python side prints the peso sign fine.

REM Run from this file's own folder, whatever directory the shell started in.
cd /d "%~dp0"

REM "py" is the Windows Python launcher and is the usual way in; fall back to
REM "python" for installs that only put that on PATH.
set PY=py
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python
%PY% --version >nul 2>&1
if errorlevel 1 goto nopython

echo ============================================================
echo  Tama ba ang Diskwento?  --  demo
echo ============================================================
echo.
echo [1/3] Running the test suite...
echo.
%PY% -m unittest discover -s tests -t .
if errorlevel 1 goto testsfailed

echo.
echo [2/3] Checking what is installed...
echo.
%PY% -m diskwento doctor

echo.
echo [3/3] Auditing the sample receipts...
echo       (Ollama is used when running; otherwise the letters come from
echo        the deterministic template. Add --no-llm to force the template.)
echo.
%PY% -m diskwento demo

echo.
echo ============================================================
echo  Done.
echo ============================================================
echo.
pause
exit /b 0

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

:testsfailed
echo.
echo  The tests did not pass. Do not demo until this is green --
echo  a failure here means the discount math is wrong.
echo.
pause
exit /b 1
