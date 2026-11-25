@echo off
REM 双击启动（无控制台）。真正的启动逻辑在 run.bat，
REM 本机需要指定解释器时，把路径写进 run.local.bat（该文件不入库）。
call "%~dp0run.bat" %*
