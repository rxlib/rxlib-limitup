@echo off
setlocal
title rxlib
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
endlocal
