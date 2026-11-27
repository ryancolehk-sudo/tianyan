Unicode true
!include "MUI2.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!ifndef PROGRAM
  !error "PROGRAM is required"
!endif
!ifndef OUTPUT
  !error "OUTPUT is required"
!endif
!ifndef RELEASE_ID
  !error "RELEASE_ID is required"
!endif
!ifndef VERSION
  !error "VERSION is required"
!endif
Name "天衍系统"
OutFile "${OUTPUT}"
InstallDir "$PROGRAMFILES64\Tianyan"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "天衍系统"
VIAddVersionKey "FileDescription" "天衍系统 Windows 安装程序"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Tianyan contributors"
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

!macro TianyanShortcuts
  CreateDirectory "$SMPROGRAMS\天衍系统"
  CreateShortcut "$DESKTOP\天衍系统.lnk" "$INSTDIR\versions\${RELEASE_ID}\runtime\pythonw.exe" '-I -B -m app.native.launcher --config "$APPDATA\Tianyan\runtime.json"' "$INSTDIR\versions\${RELEASE_ID}\frontend\tianyan-brand.ico" 0
  CreateShortcut "$SMPROGRAMS\天衍系统\天衍系统.lnk" "$INSTDIR\versions\${RELEASE_ID}\runtime\pythonw.exe" '-I -B -m app.native.launcher --config "$APPDATA\Tianyan\runtime.json"' "$INSTDIR\versions\${RELEASE_ID}\frontend\tianyan-brand.ico" 0
!macroend

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "需要 Windows 10/11 64 位系统。"
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "需要 Windows 10 或更新版本。"
    Abort
  ${EndIf}
  SetShellVarContext all
  ReadRegStr $0 HKLM "Software\Tianyan" "Program"
  ${If} $0 == "$INSTDIR\versions\${RELEASE_ID}"
    nsExec::ExecToLog '"$0\runtime\python.exe" -I -B -m app.native.install install --program "$0" --data "$APPDATA\Tianyan" --wrapper "$INSTDIR\TianyanService.exe"'
    Pop $1
    ${If} $1 == 0
      !insertmacro TianyanShortcuts
      MessageBox MB_ICONINFORMATION "后台服务已就绪。可从桌面快捷方式打开天衍系统。"
      SetErrorLevel 0
    ${Else}
      MessageBox MB_ICONSTOP "服务检查未通过，用户资料已保留。请查看安装目录中的 WINDOWS_INSTALL.md 和本机日志。"
      SetErrorLevel 1
    ${EndIf}
    Quit
  ${EndIf}
FunctionEnd

Section "天衍系统" SEC_MAIN
  SetOutPath "$INSTDIR\versions\${RELEASE_ID}"
  File /r "${PROGRAM}\*"
  DetailPrint "正在准备 Microsoft 运行库……"
  ExecWait '"$INSTDIR\versions\${RELEASE_ID}\redist\VC_redist.x64.exe" /install /quiet /norestart' $0
  ${If} $0 == 3010
    SetRebootFlag true
  ${ElseIf} $0 != 0
  ${AndIf} $0 != 1638
    MessageBox MB_ICONSTOP "运行库安装未完成。请检查 Windows 安装状态后重试。"
    SetErrorLevel 1
    Abort
  ${EndIf}
  DetailPrint "正在安装后台服务并检查数据服务，请稍候……"
  nsExec::ExecToLog '"$INSTDIR\versions\${RELEASE_ID}\runtime\python.exe" -I -B -m app.native.install install --program "$INSTDIR\versions\${RELEASE_ID}" --data "$APPDATA\Tianyan" --wrapper "$INSTDIR\TianyanService.exe"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "安装未完成。已有资料和恢复记录已保留，请查看天衍系统维护说明。"
    SetErrorLevel 1
    Abort
  ${EndIf}
  !insertmacro TianyanShortcuts
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Tianyan" "DisplayName" "天衍系统"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Tianyan" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Tianyan" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKLM "Software\Tianyan" "Program" "$INSTDIR\versions\${RELEASE_ID}"
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  ReadRegStr $1 HKLM "Software\Tianyan" "Program"
  ${If} $1 == ""
    MessageBox MB_ICONSTOP "缺少安装记录，已保留程序和用户资料。"
    Abort
  ${EndIf}
  nsExec::ExecToLog '"$1\runtime\python.exe" -I -B -m app.native.install uninstall --program "$1" --data "$APPDATA\Tianyan" --wrapper "$INSTDIR\TianyanService.exe"'
  Pop $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "后台服务未能停止，已保留程序和用户资料。"
    Abort
  ${EndIf}
  Delete "$DESKTOP\天衍系统.lnk"
  Delete "$SMPROGRAMS\天衍系统\天衍系统.lnk"
  RMDir "$SMPROGRAMS\天衍系统"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\Tianyan"
  DeleteRegKey HKLM "Software\Tianyan"
  Delete "$INSTDIR\TianyanService.exe"
  Delete "$INSTDIR\TianyanService.xml"
  Delete "$INSTDIR\Uninstall.exe"
  ; Version folders and ProgramData are intentionally retained. They are the
  ; user's recovery source; uninstall never recursively removes either tree.
  DetailPrint "后台服务已卸载。用户数据和可恢复版本已保留。"
SectionEnd
