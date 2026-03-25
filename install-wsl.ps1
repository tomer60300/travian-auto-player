$log = "$env:TEMP\openclaw-wsl-install.log"
"" | Out-File $log -Encoding UTF8
function Log($m) { $l="[$(Get-Date -f 'HH:mm:ss')] $m"; Write-Output $l; $l | Out-File -Append $log -Encoding UTF8 }

Log "Checking WSL feature..."
$wslFeature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux
Log "WSL Feature state: $($wslFeature.State)"

$vmFeature = Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform
Log "VirtualMachinePlatform state: $($vmFeature.State)"

if ($wslFeature.State -ne "Enabled") {
    Log "Enabling WSL feature..."
    Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Windows-Subsystem-Linux -NoRestart -All 2>&1 | ForEach-Object { Log $_ }
}
if ($vmFeature.State -ne "Enabled") {
    Log "Enabling VirtualMachinePlatform..."
    Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -NoRestart -All 2>&1 | ForEach-Object { Log $_ }
}

Log "Setting WSL default version to 2..."
wsl --set-default-version 2 2>&1 | ForEach-Object { Log $_ }

Log "Installing Ubuntu 22.04..."
wsl --install Ubuntu-22.04 --no-launch 2>&1 | ForEach-Object { Log $_ }

Log "Checking result..."
wsl --list --verbose 2>&1 | ForEach-Object { Log $_ }

Log "=== DONE ==="
