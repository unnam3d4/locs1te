Unicode true
!include "MUI2.nsh"
Name "БАЗА ДАННЫХ ИТСО"
OutFile "ITSO_Setup.exe"
InstallDir "C:\ITSO_DB"
RequestExecutionLevel admin
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Section "Установка" SecMain
  SetOutPath "$INSTDIR"
  CreateDirectory "$INSTDIR\data"
  File "..\dist\ITSO_Server.exe"
  File "..\dist\ITSO_Service.exe"
  File "..\dist\ITSO_Admin.exe"
  ; A previous install's data and settings are intentionally never overwritten.
  IfFileExists "$INSTDIR\settings.json" +2 0
    File "..\dist\settings.json"
  ExecWait '"$INSTDIR\ITSO_Service.exe" --startup auto install' $0
  ExecWait 'sc.exe failure ITSO_DB reset= 86400 actions= restart/60000/restart/60000/restart/60000'
  WriteUninstaller "$INSTDIR\Uninstall_ITSO.exe"
  CreateDirectory "$SMPROGRAMS\БАЗА ДАННЫХ ИТСО"
  CreateShortCut "$SMPROGRAMS\БАЗА ДАННЫХ ИТСО\Управление.lnk" "$INSTDIR\ITSO_Admin.exe"
  CreateShortCut "$DESKTOP\БАЗА ДАННЫХ ИТСО.lnk" "$INSTDIR\ITSO_Admin.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ITSO_DB" "DisplayName" "БАЗА ДАННЫХ ИТСО"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ITSO_DB" "UninstallString" '"$INSTDIR\Uninstall_ITSO.exe"'
  ; No broad firewall rule is added automatically. Use Windows Firewall to allow
  ; only the trusted LAN network when enabling a LAN IP in the settings.
SectionEnd

Section "Uninstall"
  ExecWait '"$INSTDIR\ITSO_Service.exe" stop'
  ExecWait '"$INSTDIR\ITSO_Service.exe" remove'
  Delete "$INSTDIR\ITSO_Server.exe"
  Delete "$INSTDIR\ITSO_Service.exe"
  Delete "$INSTDIR\ITSO_Admin.exe"
  Delete "$INSTDIR\Uninstall_ITSO.exe"
  Delete "$DESKTOP\БАЗА ДАННЫХ ИТСО.lnk"
  RMDir /r "$SMPROGRAMS\БАЗА ДАННЫХ ИТСО"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\ITSO_DB"
  ; Explicitly KEEP $INSTDIR\data and backups to avoid destroying user data.
SectionEnd
