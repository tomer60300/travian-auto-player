# setup-wsl-gitlab.ps1 - Install WSL2 Ubuntu + Docker + GitLab EE
$OutFile = "$env:TEMP\openclaw-wsl-gitlab.log"
$ErrorActionPreference = "Continue"

function Log($msg) {
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Output $line
    $line | Out-File -Append -FilePath $OutFile -Encoding UTF8
}

"" | Out-File -FilePath $OutFile -Encoding UTF8

Log "Step 1: Installing Ubuntu on WSL2..."
wsl --install Ubuntu-22.04 --no-launch 2>&1 | ForEach-Object { Log $_ }

Log "Step 2: Setting WSL default version to 2..."
wsl --set-default-version 2 2>&1 | ForEach-Object { Log $_ }

Log "Step 3: Launching Ubuntu to complete setup (default user: root)..."
# Create the distro with root as default user (no password prompt)
wsl -d Ubuntu-22.04 -u root -- bash -c "echo 'Ubuntu ready'" 2>&1 | ForEach-Object { Log $_ }

Log "Step 4: Installing Docker inside WSL..."
$dockerScript = @'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg lsb-release > /dev/null 2>&1
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin > /dev/null 2>&1
service docker start
docker --version
echo "Docker installed OK"
'@
wsl -d Ubuntu-22.04 -u root -- bash -c $dockerScript 2>&1 | ForEach-Object { Log $_ }

Log "Step 5: Starting GitLab EE 16.7.10 container..."
$gitlabScript = @'
service docker start 2>/dev/null
mkdir -p /srv/gitlab/config /srv/gitlab/logs /srv/gitlab/data

docker run --detach \
    --hostname gitlab.local \
    --name gitlab-ee \
    --restart unless-stopped \
    --publish 8929:80 \
    --publish 8930:443 \
    --publish 2222:22 \
    --shm-size 256m \
    --volume /srv/gitlab/config:/etc/gitlab \
    --volume /srv/gitlab/logs:/var/log/gitlab \
    --volume /srv/gitlab/data:/var/opt/gitlab \
    --env GITLAB_OMNIBUS_CONFIG="external_url 'http://localhost:8929'; gitlab_rails['initial_root_password'] = 'Gl@bT3st2026!'; gitlab_rails['gitlab_shell_ssh_port'] = 2222; prometheus_monitoring['enable'] = false; grafana['enable'] = false; alertmanager['enable'] = false; node_exporter['enable'] = false; redis_exporter['enable'] = false; postgres_exporter['enable'] = false; gitlab_exporter['enable'] = false; sidekiq['max_concurrency'] = 5; puma['worker_processes'] = 2; postgresql['shared_buffers'] = '128MB'" \
    gitlab/gitlab-ee:16.7.10-ee.0

echo "Waiting for GitLab to become healthy..."
for i in $(seq 1 60); do
    sleep 10
    health=$(docker inspect --format '{{.State.Health.Status}}' gitlab-ee 2>/dev/null || echo "unknown")
    echo "  [$((i*10))s] Health: $health"
    if [ "$health" = "healthy" ]; then
        echo "GitLab is UP!"
        break
    fi
done
echo "Done."
'@
wsl -d Ubuntu-22.04 -u root -- bash -c $gitlabScript 2>&1 | ForEach-Object { Log $_ }

Log "=== Setup complete ==="
Log "URL: http://localhost:8929"
Log "User: root"
Log "Password: Gl@bT3st2026!"
