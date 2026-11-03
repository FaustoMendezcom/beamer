# Read-only: gathers what Beamer's secure desktop and sign-in screen receiver did, into
# Beamer-diagnostics.txt beside this script. Run in an administrator PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\Collect-BeamerDiagnostics.ps1
$out = Join-Path $PSScriptRoot 'Beamer-diagnostics.txt'
& {
    "=== $(Get-Date)  $env:COMPUTERNAME  console user: $((Get-CimInstance Win32_ComputerSystem).UserName)"
    "=== Last boot: $((Get-CimInstance Win32_OperatingSystem).LastBootUpTime)"
    '=== Service'
    Get-Service BeamerSecureDesktop -ErrorAction SilentlyContinue | Format-List Name, Status, StartType | Out-String
    (Get-CimInstance Win32_Service -Filter "Name='BeamerSecureDesktop'" -ErrorAction SilentlyContinue).PathName
    '=== HKLM\SOFTWARE\Beamer\SecureDesktop'
    Get-ItemProperty HKLM:\SOFTWARE\Beamer\SecureDesktop -ErrorAction SilentlyContinue | Format-List AllowedClient, ServiceExe, StandardUser, ElevatedWindows | Out-String
    '=== Files'
    Get-ChildItem 'C:\Program Files\Beamer' -Recurse -ErrorAction SilentlyContinue | Select-Object FullName, Length, LastWriteTime | Format-Table -AutoSize | Out-String -Width 200
    '=== Firewall'
    Get-NetFirewallRule -DisplayName 'Beamer*' -ErrorAction SilentlyContinue | Format-Table DisplayName, Enabled, Profile, Direction -AutoSize | Out-String
    Get-NetConnectionProfile | Format-Table InterfaceAlias, NetworkCategory, IPv4Connectivity -AutoSize | Out-String
    Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress -AutoSize | Out-String
    '=== Processes'
    Get-CimInstance Win32_Process -Filter "Name='BeamerSecure.exe' or Name='Beamer.exe'" | ForEach-Object {
        "$($_.ProcessId)  session $($_.SessionId)  $($_.CommandLine)"
    }
    foreach ($log in 'secure-service.log', 'logon-receiver.log', 'secure-helper.log') {
        "=== $log (last 60 lines)"
        Get-Content (Join-Path 'C:\Program Files\Beamer\logs' $log) -Tail 60 -ErrorAction SilentlyContinue
    }
} *>&1 | Out-File -FilePath $out -Encoding utf8
Write-Host "Saved $out" -ForegroundColor Green
