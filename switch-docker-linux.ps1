$log = "$env:TEMP\openclaw-docker-switch.log"
"" | Out-File $log -Encoding UTF8
function Log($m) { $l="[$(Get-Date -f 'HH:mm:ss')] $m"; Write-Output $l; $l | Out-File -Append $log -Encoding UTF8 }

# Check if DockerMsftProvider or dockerd supports lcow/linux
Log "Current Docker info:"
docker info 2>&1 | Select-String "OSType|Server Version|Operating System" | ForEach-Object { Log $_ }

# Try switching via dockercli if available
$switchPath = "C:\Program Files\Docker\Docker\DockerCli.exe"
if (Test-Path $switchPath) {
    Log "Switching to Linux containers via DockerCli..."
    & $switchPath -SwitchLinuxEngine 2>&1 | ForEach-Object { Log $_ }
} else {
    Log "DockerCli not found. Checking for alternative..."
    # For Docker CE/moby on Windows Server, need to configure daemon.json
    $daemonJson = "C:\ProgramData\docker\config\daemon.json"
    Log "Daemon config path: $daemonJson"
    if (Test-Path $daemonJson) {
        Log "Current daemon.json:"
        Get-Content $daemonJson | ForEach-Object { Log $_ }
    } else {
        Log "No daemon.json found"
    }
}

Log "=== DONE ==="
