@echo off
rem Requires supported Windows Python/PyInstaller; build must be tested on Win7 SP1.
chcp 65001 >nul
cd /d "%~dp0.."
py -3.8 -m PyInstaller --onedir --clean --name ITSO_Server --add-data "web;web" server.py
if errorlevel 1 echo Build failed. Install and verify PyInstaller for Python 3.8 first.
pause
