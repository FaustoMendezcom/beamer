# Updates this account's Beamer from the folder this script is in (Beamer.exe and
# Install-SecureDesktop.ps1 beside it), restarts it, then after 10 s runs the secure desktop installer
# as administrator with -ElevatedWindows. Run it in a normal PowerShell, as the signed-in user:
#   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\Beamer\Update-Beamer.ps1"
# The UAC prompt asks for an administrator's password; Beamer is back up by then, so it can be typed
# from the driving machine.
$ErrorActionPreference = 'Stop'
$source = $PSScriptRoot
$target = Join-Path $env:LOCALAPPDATA 'Beamer'
$exe = Join-Path $target 'Beamer.exe'

New-Item -ItemType Directory -Path $target -Force | Out-Null
$running = @(Get-Process Beamer -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
$running | Stop-Process -Force
$running | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
# The exe stays locked a moment after its processes have gone.
for ($attempt = 1; ; $attempt++) {
    try {
        Copy-Item -LiteralPath (Join-Path $source 'Beamer.exe') -Destination $exe -Force -ErrorAction Stop
        break
    } catch [System.IO.IOException] {
        if ($attempt -ge 20) { throw }
        Start-Sleep -Milliseconds 500
    }
}
Start-Process -FilePath $exe
Write-Host "Beamer updated at $exe and started; the installer follows in 10 s." -ForegroundColor Green
Start-Sleep -Seconds 10
$installer = Join-Path $source 'Install-SecureDesktop.ps1'
Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$installer`" -ClientPath `"$exe`" -ElevatedWindows"
