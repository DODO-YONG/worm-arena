@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Worm Arena Server

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
if not defined PY (
  echo Python not found. Please install it from https://www.python.org
  pause
  exit /b 1
)

%PY% -X utf8 server.py %*
pause
