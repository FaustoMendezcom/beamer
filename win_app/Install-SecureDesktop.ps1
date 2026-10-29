# Installs (or with -Remove, removes) Beamer's secure desktop input: the BeamerSecureDesktop
# service, as LocalSystem, from a copy of Beamer.exe under Program Files that only administrators
# can change. See docs/secure-desktop.md. Run as administrator; run again after updating Beamer and
# after pairing, so the sign-in screen knows the same machines.
[CmdletBinding()]
param(
    # The Beamer.exe that may use the secure desktop. By default the signed-in user's.
    [string]$ClientPath,
    # Leave out the sign-in screen receiver: Beamer then works only once someone has signed in.
    [switch]$NoSignInScreen,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'BeamerSecureDesktop'
$InstallDir = Join-Path $env:ProgramFiles 'Beamer'
$ServiceExe = Join-Path $InstallDir 'BeamerSecure.exe'
$KeyPath = 'HKLM:\SOFTWARE\Beamer\SecureDesktop'
$PolicyPath = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
$LogonDir = Join-Path $InstallDir 'logon'
$FirewallRule = 'Beamer Sign-in Screen (TCP-In)'

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
    Remove-Item -Path $LogonDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue
    Remove-Item -Path (Join-Path $InstallDir 'logs') -Recurse -Force -ErrorAction SilentlyContinue
    if ((Test-Path $InstallDir) -and -not (Get-ChildItem $InstallDir)) { Remove-Item $InstallDir -Force }
    # Logs of earlier builds, which wrote under ProgramData.
    Remove-Item -Path (Join-Path $env:ProgramData 'Beamer\secure-*.log*') -Force -ErrorAction SilentlyContinue
    Write-Host 'Beamer secure desktop input removed.' -ForegroundColor Green
    return
}

# Whoever is signed in at the console, who may not be the administrator running this: a standard
# user approving it with an administrator's password.
$signedIn = $null
$consoleName = (Get-CimInstance Win32_ComputerSystem).UserName
if ($consoleName) {
    $sid = (New-Object Security.Principal.NTAccount($consoleName)).Translate([Security.Principal.SecurityIdentifier]).Value
    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"
    $signedIn = [pscustomobject]@{
        Name = $consoleName; Sid = $sid
        Profile = [Environment]::ExpandEnvironmentVariables((Get-ItemProperty -Path $profileKey).ProfileImagePath)
    }
}
$otherUser = $signedIn -and $signedIn.Sid -ne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if (-not $ClientPath) {
    $ClientPath = if ($otherUser) { Join-Path $signedIn.Profile 'AppData\Local\Beamer\Beamer.exe' }
                  else { Join-Path $env:LOCALAPPDATA 'Beamer\Beamer.exe' }
}
$ClientPath = [IO.Path]::GetFullPath($ClientPath)
if (-not (Test-Path -LiteralPath $ClientPath -PathType Leaf) -or [IO.Path]::GetFileName($ClientPath) -ine 'Beamer.exe') {
    throw "No Beamer.exe at $ClientPath"
}

Stop-BeamerSecure
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
# Earlier builds logged under ProgramData, a folder any user can make; they log beside the exe now.
Remove-Item -Path (Join-Path $env:ProgramData 'Beamer\secure-*.log*') -Force -ErrorAction SilentlyContinue
Copy-WhenFree $ClientPath $ServiceExe

New-Item -Path $KeyPath -Force | Out-Null
Set-ItemProperty -Path $KeyPath -Name 'AllowedClient' -Value $ClientPath -Type String
# Beamer checks the pipe is served by this exe, as SYSTEM, before writing anything to it.
Set-ItemProperty -Path $KeyPath -Name 'ServiceExe' -Value $ServiceExe -Type String
# A standard user's Beamer runs unelevated: the helper takes it from that account alone, and only
# while the account is not an administrator (docs/secure-desktop.md).
if ($otherUser) {
    Set-ItemProperty -Path $KeyPath -Name 'StandardUser' -Value $signedIn.Sid -Type String
    Write-Host "$($signedIn.Name)'s own Beamer may use it without running as administrator."
} else {
    Remove-ItemProperty -Path $KeyPath -Name 'StandardUser' -ErrorAction SilentlyContinue
}

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

# The app's own inbound rules on private networks, as Install-Beamer.ps1 makes them: a Beamer.exe
# copied in by hand has only what Windows asked about, often public alone, and is then unreachable
# once the network is private. Rules for other programs of the same name are left alone.
foreach ($rule in @(
        @{ Name = 'Beamer Receiver (TCP-In)'; Protocol = 'TCP'; Port = 24820 },
        @{ Name = 'Beamer Pairing (UDP-In)'; Protocol = 'UDP'; Port = 24821 },
        @{ Name = 'Beamer Pairing (TCP-In)'; Protocol = 'TCP'; Port = 24821 })) {
    $existing = @(Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue | Where-Object {
        [string]::Equals(($_ | Get-NetFirewallApplicationFilter).Program, $ClientPath, [StringComparison]::OrdinalIgnoreCase) })
    if (-not $existing) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol $rule.Protocol -LocalPort $rule.Port `
            -Action Allow -Profile Private -Program $ClientPath | Out-Null
    }
}

# The sign-in screen receiver: this PC's pairings, readable by SYSTEM and administrators only, as
# they hold the keys. A copy, refreshed by running this again; the receiver never writes it.
Remove-Item -Path $LogonDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue
if (-not $NoSignInScreen) {
    $SettingsPath = Join-Path (Split-Path -Parent $ClientPath) 'settings.json'
    if ($otherUser -and -not $PSBoundParameters.ContainsKey('ClientPath')) {
        $SettingsPath = Join-Path $signedIn.Profile 'AppData\Local\Beamer\settings.json'
    } elseif (-not (Test-Path -LiteralPath $SettingsPath)) {
        $SettingsPath = Join-Path $env:LOCALAPPDATA 'Beamer\settings.json'
    }
    if (Test-Path -LiteralPath $SettingsPath -PathType Leaf) {
        $settings = Get-Content -LiteralPath $SettingsPath -Raw | ConvertFrom-Json
        if (-not $settings.machine_id -or -not $settings.peers) { throw "$SettingsPath has no pairings yet: pair first, then run this again." }
        $folder = New-Item -ItemType Directory -Path $LogonDir -Force
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
            $account = New-Object Security.Principal.SecurityIdentifier($sid)
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($account, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
        $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
        Set-Acl -LiteralPath $folder.FullName -AclObject $acl
        Copy-Item -LiteralPath $SettingsPath -Destination (Join-Path $LogonDir 'settings.json') -Force
        $port = if ($settings.port) { [int]$settings.port } else { 24820 }
        New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow `
            -Profile Private -Program $ServiceExe | Out-Null
        Write-Host "The sign-in screen takes input from the $(@($settings.peers).Count) machine(s) paired in $SettingsPath."
        # The rule is for private networks only: on a public one the sign-in screen is unreachable.
        foreach ($network in @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq 'Public')) {
            Write-Warning ("$($network.InterfaceAlias) is a public network, where the sign-in screen cannot be reached. " +
                "If it is your own network: Set-NetConnectionProfile -InterfaceAlias '$($network.InterfaceAlias)' -NetworkCategory Private")
        }
    } else {
        Write-Warning "No settings at $SettingsPath; the sign-in screen receiver is left out until this runs again after pairing."
    }
}

$binary = "`"$ServiceExe`" --service"
if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    & sc.exe config $ServiceName binPath= $binary start= auto | Out-Null
} else {
    New-Service -Name $ServiceName -BinaryPathName $binary -DisplayName 'Beamer secure desktop input' `
        -Description 'Lets Beamer type on the secure desktop: UAC prompts, the lock screen and Ctrl+Alt+Del.' `
        -StartupType Automatic | Out-Null
}
# Restart on failure, after 5 s, then 30 s.
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/30000/""/0 | Out-Null
Start-Service -Name $ServiceName
Write-Host "Beamer secure desktop input installed: $ServiceExe, for $ClientPath" -ForegroundColor Green
