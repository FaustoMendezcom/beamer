# Read-only: why wake-on-LAN does or does not wake this PC. Writes Beamer-wake-diagnostics.txt beside
# this script. Run in an administrator PowerShell (the event log and adapter settings need it):
#   powershell -ExecutionPolicy Bypass -File .\Collect-WakeDiagnostics.ps1
$out = Join-Path $PSScriptRoot 'Beamer-wake-diagnostics.txt'
& {
    "=== $(Get-Date)  $env:COMPUTERNAME"
    '=== Sleep states (powercfg /a)'
    powercfg /a
    '=== Last wake source (powercfg /lastwake)'
    powercfg /lastwake
    '=== Devices allowed to wake the PC (powercfg /devicequery wake_armed)'
    powercfg /devicequery wake_armed
    '=== Hibernate and standby timeouts on AC and battery (seconds)'
    powercfg /query SCHEME_CURRENT SUB_SLEEP | Select-String -Pattern 'Power Setting GUID|Current AC|Current DC'
    '=== Network connectivity in standby (0 = off, 1 = on, 2 = managed by Windows)'
    powercfg /query SCHEME_CURRENT SUB_NONE F15576E8-98B7-4186-B944-EAFA664402D9 2>&1 | Select-String -Pattern 'Current AC|Current DC|Power Setting'
    '=== Adapters'
    Get-NetAdapter | Format-Table Name, InterfaceDescription, Status, MacAddress, LinkSpeed -AutoSize | Out-String -Width 200
    Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress -AutoSize | Out-String
    '=== Adapter wake settings'
    Get-NetAdapterPowerManagement -ErrorAction SilentlyContinue |
        Format-List Name, AllowComputerToTurnOffDevice, DeviceSleepOnDisconnect, WakeOnMagicPacket, WakeOnPattern | Out-String -Width 200
    Get-NetAdapterAdvancedProperty -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match 'wake|WoL|magic|energy|power|sleep' } |
        Format-Table Name, DisplayName, DisplayValue -AutoSize | Out-String -Width 200
    '=== Sleep, hibernate and wake events, last 2 days'
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = (Get-Date).AddDays(-2);
            ProviderName = 'Microsoft-Windows-Kernel-Power', 'Microsoft-Windows-Power-Troubleshooter' } -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -in 1, 42, 107, 506, 507, 566 } |
        Select-Object -First 60 |
        Format-Table TimeCreated, Id, @{ n = 'Message'; e = { ($_.Message -split "`n" | Select-Object -First 3) -join ' | ' } } -Wrap -AutoSize |
        Out-String -Width 250
} *>&1 | Out-File -FilePath $out -Encoding utf8
Write-Host "Saved $out" -ForegroundColor Green
