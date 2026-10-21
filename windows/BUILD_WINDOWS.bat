@echo off
setlocal
cd /d "%~dp0\.."
where py >nul 2>&1 || (echo Python launcher not installed & exit /b 1)
py -3.8 -m pip install "pyinstaller==5.13.2" "pywin32==306" || exit /b 1
py -3.8 -m PyInstaller --noconfirm --clean --onefile --name ITSO_Server --add-data "web;web" server.py || exit /b 1
py -3.8 -m PyInstaller --noconfirm --clean --onefile --name ITSO_Service --add-data "web;web" --paths . --hidden-import win32timezone --hidden-import servicemanager windows\itso_service.py || exit /b 1
py -3.8 -m PyInstaller --noconfirm --clean --onefile --windowed --name ITSO_Admin --add-data "web;web" --paths . windows\admin_panel.py || exit /b 1
if not exist dist\settings.json echo {"host":"127.0.0.1","port":3000,"data_dir":"C:/ITSO_DB/data"}>dist\settings.json
where makensis >nul 2>&1
if errorlevel 1 (
 echo [IMPORTANT] Install NSIS 3 to compile windows\ITSO_Install.nsi into ITSO_Setup.exe
) else (
 cd windows
 makensis ITSO_Install.nsi
)
echo Build completed. TEST on Win7 SP1 and Win10/11 before deployment.
endlocal
