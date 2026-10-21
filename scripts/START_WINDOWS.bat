@echo off
chcp 65001 >nul
cd /d "%~dp0.."
py -3.8 server.py --host 127.0.0.1 --port 3000
pause
