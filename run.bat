@echo off
REM ============================================================
REM  FolderScatter launcher
REM  Prefer pythonw (no console window).
REM  Need a specific interpreter on your machine? Create
REM  run.local.bat next to this file with:
REM      set "SCATTER_PYW=D:\path\to\pythonw.exe"
REM      set "SCATTER_PY=D:\path\to\python.exe"
REM ============================================================
setlocal
if exist "%~dp0run.local.bat" call "%~dp0run.local.bat"

if defined SCATTER_PYW (
    start "" "%SCATTER_PYW%" "%~dp0main.py" %*
) else (
    start "" pythonw "%~dp0main.py" %*
)
