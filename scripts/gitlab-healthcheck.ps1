# GitLab Health Check Script
# Ensures WSL + GitLab are running

$wslState = (wsl -l -v 2>&1 | Select-String "Ubuntu-22.04")
if ($wslState -match "Stopped") {
    Write-Output "WSL stopped — starting GitLab..."
    wsl -d Ubuntu-22.04 -u root -- gitlab-ctl start 2>&1
    Start-Sleep -Seconds 30
    # Restart workhorse after puma warms up (race condition fix)
    wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart gitlab-workhorse 2>&1
    Start-Sleep -Seconds 5
}

# Check health from inside WSL (more reliable than going through Windows networking)
$health = wsl -d Ubuntu-22.04 -u root -- curl -s -o /dev/null -w '%{http_code}' http://localhost/-/health 2>&1
$ip = (wsl -d Ubuntu-22.04 -- hostname -I 2>&1).ToString().Trim().Split(" ")[0]

if ($health -match "200") {
    Write-Output "OK: GitLab healthy at http://${ip}"
    exit 0
} else {
    Write-Output "GitLab returned $health — restarting all services..."
    wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart 2>&1
    Start-Sleep -Seconds 60
    wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart gitlab-workhorse 2>&1
    Write-Output "Restart issued. IP: http://${ip}"
    exit 1
}
