@echo off
setlocal

REM Create a Windows desktop shortcut that uses the bundled DAQ XY icon.
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$repo = (Resolve-Path '.').Path; " ^
  "$desktop = [Environment]::GetFolderPath('DesktopDirectory'); " ^
  "$shortcutPath = Join-Path $desktop 'DAQ XY Control.lnk'; " ^
  "$targetPath = Join-Path $repo 'Start_DAQ_XY_UI.bat'; " ^
  "$iconPath = Join-Path $repo 'src\daq_xy_qt_readback\assets\daq_xy_control_unique.ico'; " ^
  "$shell = New-Object -ComObject WScript.Shell; " ^
  "$shortcut = $shell.CreateShortcut($shortcutPath); " ^
  "$shortcut.TargetPath = $targetPath; " ^
  "$shortcut.WorkingDirectory = $repo; " ^
  "$shortcut.IconLocation = $iconPath + ',0'; " ^
  "$shortcut.Description = 'Launch DAQ XY Control'; " ^
  "$shortcut.Save(); " ^
  "Write-Host ('Created ' + $shortcutPath)"

if errorlevel 1 exit /b %errorlevel%

echo.
echo Press any key to exit.
pause >nul
