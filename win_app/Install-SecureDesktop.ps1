# Installs (or with -Remove, removes) Beamer's secure desktop input: the BeamerSecureDesktop
# service, as LocalSystem, from a copy of Beamer.exe under Program Files that only administrators
# can change. See docs/secure-desktop.md. Run as administrator; run again after updating Beamer.
[CmdletBinding()]
param(
    # The Beamer.exe that may use the secure desktop (the one that runs as administrator).
    [string]$ClientPath = (Join-Path $env:LOCALAPPDATA 'Beamer\Beamer.exe'),
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'BeamerSecureDesktop'
$InstallDir = Join-Path $env:ProgramFiles 'Beamer'
$ServiceExe = Join-Path $InstallDir 'BeamerSecure.exe'
$KeyPath = 'HKLM:\SOFTWARE\Beamer\SecureDesktop'
$PolicyPath = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this as administrator.'
}

function Stop-BeamerSecure {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force
        $service.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15))
    }
    # The helper it started, and the exe's own launcher process, may outlive it for a moment.
    $left = @(Get-Process -Name 'BeamerSecure' -ErrorAction SilentlyContinue)
    $left | Stop-Process -Force -ErrorAction SilentlyContinue
    $left | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
}

function Copy-WhenFree([string]$From, [string]$To) {
    # The exe stays locked a moment after its processes have gone.
    for ($attempt = 1; ; $attempt++) {
        try {
            Copy-Item -LiteralPath $From -Destination $To -Force -ErrorAction Stop
            return
        } catch [System.IO.IOException] {
            if ($attempt -ge 20) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

if ($Remove) {
    Stop-BeamerSecure
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & sc.exe delete $ServiceName | Out-Null
    }
    if (Test-Path $KeyPath) {
        $setPolicy = (Get-ItemProperty -Path $KeyPath -Name 'SetSasPolicy' -ErrorAction SilentlyContinue).SetSasPolicy
        if ($null -ne $setPolicy) {
            if ($setPolicy -eq -1) {
                Remove-ItemProperty -Path $PolicyPath -Name 'SoftwareSASGeneration' -ErrorAction SilentlyContinue
            } else {
                Set-ItemProperty -Path $PolicyPath -Name 'SoftwareSASGeneration' -Value $setPolicy -Type DWord
            }
        }
        Remove-Item -Path $KeyPath -Recurse -Force
    }
    Remove-Item -Path $ServiceExe -Force -ErrorAction SilentlyContinue
    if ((Test-Path $InstallDir) -and -not (Get-ChildItem $InstallDir)) { Remove-Item $InstallDir -Force }
    Write-Host 'Beamer secure desktop input removed.' -ForegroundColor Green
    return
}

$ClientPath = [IO.Path]::GetFullPath($ClientPath)
if (-not (Test-Path -LiteralPath $ClientPath -PathType Leaf) -or [IO.Path]::GetFileName($ClientPath) -ine 'Beamer.exe') {
    throw "No Beamer.exe at $ClientPath"
}

Stop-BeamerSecure
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Copy-WhenFree $ClientPath $ServiceExe

New-Item -Path $KeyPath -Force | Out-Null
Set-ItemProperty -Path $KeyPath -Name 'AllowedClient' -Value $ClientPath -Type String

# Services may send Ctrl+Alt+Del (1), alongside Ease of Access apps where that was allowed (2 -> 3).
# What it was is kept, so -Remove puts it back.
if ($null -eq (Get-ItemProperty -Path $KeyPath -Name 'SetSasPolicy' -ErrorAction SilentlyContinue)) {
    $current = (Get-ItemProperty -Path $PolicyPath -Name 'SoftwareSASGeneration' -ErrorAction SilentlyContinue).SoftwareSASGeneration
    if ($null -eq $current) {
        Set-ItemProperty -Path $KeyPath -Name 'SetSasPolicy' -Value -1 -Type DWord
        Set-ItemProperty -Path $PolicyPath -Name 'SoftwareSASGeneration' -Value 1 -Type DWord
    } elseif ($current -eq 0 -or $current -eq 2) {
        Set-ItemProperty -Path $KeyPath -Name 'SetSasPolicy' -Value $current -Type DWord
        Set-ItemProperty -Path $PolicyPath -Name 'SoftwareSASGeneration' -Value ($current + 1) -Type DWord
    }
}

$binary = "`"$ServiceExe`" --service"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    & sc.exe config $ServiceName binPath= $binary start= auto | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName $binary -DisplayName 'Beamer secure desktop input' `
        -Description 'Lets Beamer, running as administrator, type on the secure desktop: UAC prompts, the lock screen and Ctrl+Alt+Del.' `
        -StartupType Automatic | Out-Null
}
# Restart on failure, after 5 s, then 30 s.
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/""/0 | Out-Null
Start-Service -Name $ServiceName
Write-Host "Beamer secure desktop input installed: $ServiceExe, for $ClientPath" -ForegroundColor Green
