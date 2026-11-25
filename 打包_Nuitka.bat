@echo off
setlocal
REM ===================================================================
REM  FolderScatter - Nuitka 单文件打包（plain：不加 UPX，不开 LTO）
REM  Nuitka 自带压缩已够用，产物约 20 MB。
REM
REM  准备：pip install nuitka PyQt6
REM  解释器：本机需要指定时，把路径写进 run.local.bat 的 SCATTER_PY
REM  运行时数据存在 %APPDATA%，输出目录保持干净。
REM ===================================================================
if exist "%~dp0run.local.bat" call "%~dp0run.local.bat"
if defined SCATTER_PY (set "PY=%SCATTER_PY%") else (set "PY=python")

set "ROOT=%~dp0"
set "OUT=%ROOT%dist_nuitka_plain_onefile"
cd /d "%ROOT%"
echo.
echo 正在打包单文件 exe ...（5-15 分钟，请等待）
echo.
"%PY%" -m nuitka --onefile --windows-console-mode=disable --enable-plugin=pyqt6 --lto=no --windows-icon-from-ico="%ROOT%appicon.ico" --include-data-files="%ROOT%appicon.ico=appicon.ico" --include-data-files="%ROOT%appicon_16.png=appicon_16.png" --include-data-files="%ROOT%appicon_32.png=appicon_32.png" --include-data-files="%ROOT%appicon_64.png=appicon_64.png" --include-data-files="%ROOT%appicon_128.png=appicon_128.png" --include-data-files="%ROOT%appicon_256.png=appicon_256.png" --include-data-files="%ROOT%appicon_512.png=appicon_512.png" --output-dir="%OUT%" --output-filename=FolderScatter_plain.exe --assume-yes-for-downloads --remove-output "%ROOT%main.py"
echo.
echo BUILD_EXITCODE=%ERRORLEVEL%
if /I "%~1"=="-nopause" goto :eof
pause
