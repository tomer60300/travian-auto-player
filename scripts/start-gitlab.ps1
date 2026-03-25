# Start GitLab in WSL and set up port forwarding
# Run as Administrator (scheduled task handles this)

$logFile = "$env:USERPROFILE\.openclaw\workspace\scripts\gitlab-startup.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

function Log($msg) {
    "$timestamp | $msg" | Out-File -Append -FilePath $logFile
    Write-Output $msg
}

Log "=== GitLab startup triggered ==="

# Start GitLab services
Log "Starting GitLab services..."
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl start 2>&1 | Out-File -Append -FilePath $logFile

# Wait for services to initialize
Log "Waiting 15s for services to initialize..."
Start-Sleep -Seconds 15

# Get WSL IP
$wslIp = (wsl -d Ubuntu-22.04 -- hostname -I 2>&1).ToString().Trim().Split(" ")[0]
Log "WSL IP: $wslIp"

# Set up port forwarding: localhost:8929 -> WSL:80
# Remove old rule first
netsh interface portproxy delete v4tov4 listenport=8929 listenaddress=0.0.0.0 2>$null
netsh interface portproxy add v4tov4 listenport=8929 listenaddress=0.0.0.0 connectport=80 connectaddress=$wslIp
Log "Port forwarding: localhost:8929 -> ${wslIp}:80"

# Also forward SSH (port 2222 -> WSL:22) for git operations
netsh interface portproxy delete v4tov4 listenport=2222 listenaddress=0.0.0.0 2>$null
netsh interface portproxy add v4tov4 listenport=2222 listenaddress=0.0.0.0 connectport=22 connectaddress=$wslIp
Log "Port forwarding: localhost:2222 -> ${wslIp}:22"

# Allow through firewall if not already
$rule = Get-NetFirewallRule -DisplayName "GitLab WSL" -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -DisplayName "GitLab WSL" -Direction Inbound -LocalPort 8929,2222 -Protocol TCP -Action Allow | Out-Null
    Log "Firewall rule created"
}

# Wait for Puma socket to be ready, then restart workhorse to avoid race condition
Log "Waiting for Puma socket to be ready..."
$pumaReady = $false
for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Seconds 5
    $check = wsl -d Ubuntu-22.04 -u root -- curl -s --unix-socket /var/opt/gitlab/gitlab-rails/sockets/gitlab.socket -o /dev/null -w '%{http_code}' http://localhost/-/health 2>&1
    if ($check -match "200") {
        $pumaReady = $true
        break
    }
    Log "  Puma not ready yet, attempt $($i+1)/24..."
}

if ($pumaReady) {
    # Restart workhorse to pick up the live socket (avoids cached connection refused)
    Log "Puma ready — restarting workhorse to sync..."
    wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart gitlab-workhorse 2>&1 | Out-File -Append -FilePath $logFile
    Start-Sleep -Seconds 5
    Log "GitLab is UP at http://localhost:8929"
} else {
    Log "WARNING: Puma didn't come up in 120s. Trying full restart..."
    wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart 2>&1 | Out-File -Append -FilePath $logFile
}
