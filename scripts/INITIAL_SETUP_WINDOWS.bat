@echo off
chcp 65001 >nul
cd /d "%~dp0.."
py -3.8 server.py --init-admin
pause
