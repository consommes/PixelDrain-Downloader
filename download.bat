@echo off
chcp 65001 >nul
cd /d "%~dp0"
python pixeldrain_dl.py %*
if "%~1"=="" pause
