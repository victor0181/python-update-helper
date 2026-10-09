@echo off
setlocal
cd /d "%~dp0"

rem ==============================================================
rem  KEEP THIS FILE PURE ASCII AND CRLF.
rem  cmd.exe reads a .bat byte by byte with the active code page, so a
rem  single non-ASCII character shifts every line boundary and you get
rem  errors like "'xx' is not recognized as an internal command".
rem  All Chinese text lives in pip_helper.py, which prints it correctly.
rem ==============================================================

if not exist "%~dp0pip_helper.py" (
  echo   [ERROR] pip_helper.py was not found next to this file.
  echo.
  pause
  exit /b 1
)
if not exist "%~dp0pip_helper_gui.py" (
  echo   [ERROR] pip_helper_gui.py was not found next to this file.
  echo.
  pause
  exit /b 1
)

rem ---- pick a launcher that actually works ----
set "BOOT="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "BOOT=py -3"
if not defined BOOT (
  py -c "import sys" >nul 2>nul
  if not errorlevel 1 set "BOOT=py"
)
if not defined BOOT (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "BOOT=python"
)
if not defined BOOT (
  python3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "BOOT=python3"
)

if not defined BOOT (
  echo   [ERROR] No usable Python was found on this machine.
  echo   Install Python 3.7 or newer, tick "Add Python to PATH",
  echo   then run this file again.
  echo.
  pause
  exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem ---- prefer pythonw so double-click opens only the window, not a console ----
set "GUI_BOOT="
pyw -3 -c "import tkinter" >nul 2>nul
if not errorlevel 1 set "GUI_BOOT=pyw -3"
if not defined GUI_BOOT (
  pyw -c "import tkinter" >nul 2>nul
  if not errorlevel 1 set "GUI_BOOT=pyw"
)
if not defined GUI_BOOT (
  pythonw -c "import tkinter" >nul 2>nul
  if not errorlevel 1 set "GUI_BOOT=pythonw"
)

if defined GUI_BOOT (
  start "" /b %GUI_BOOT% "%~dp0pip_helper_gui.py" %*
  exit /b 0
)

%BOOT% "%~dp0pip_helper_gui.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" echo   Done.
if not "%RC%"=="0" echo   Finished with exit code %RC%.
echo.
pause
exit /b %RC%
