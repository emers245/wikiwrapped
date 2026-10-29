@echo off
rem Runs inside the Windows installer, before anything is installed into
rem this version's folder (%PREFIX%) and before its shortcuts are made.
rem
rem Upgrades: each version installs into its own folder, because the
rem installer refuses a folder that is not empty. So an earlier version is
rem removed here, by its own uninstaller: found through its entry in
rem "Installed apps" (the per-user uninstall keys), run silently, and with
rem _?= so that it runs in place and this script waits for it (without it,
rem an NSIS uninstaller copies itself away and returns at once). That
rem uninstaller removes the old program and its shortcuts; user data, in
rem %APPDATA%\WikiWrapped, are never touched. Running it before the
rem new shortcuts exist matters: they have the same names.
rem
rem The PowerShell below is written without double quotes inside it
rem ([char]34 is one), because cmd would end the quoted command there.
rem PowerShell is called by its full path: the installer runs this script
rem with a reduced PATH that leaves PowerShell's folder out.

"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$keys = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall';" ^
  "foreach ($key in Get-ChildItem $keys -ErrorAction SilentlyContinue) {" ^
  "  $entry = Get-ItemProperty $key.PSPath;" ^
  "  if ($entry.DisplayName -notlike 'WikiWrapped *' -or -not $entry.UninstallString) { continue }" ^
  "  $exe = $entry.UninstallString.Trim([char]34);" ^
  "  $folder = Split-Path $exe;" ^
  "  if ($folder -eq $env:PREFIX -or -not (Test-Path $exe)) { continue }" ^
  "  Write-Output ('Removing the previous version: ' + $entry.DisplayName + ' in ' + $folder);" ^
  "  Start-Process -FilePath $exe -ArgumentList '/S', ('_?=' + $folder) -Wait;" ^
  "  Remove-Item -LiteralPath $folder -Recurse -Force -ErrorAction SilentlyContinue;" ^
  "}"
if errorlevel 1 exit /b 1
exit /b 0
