# setup-gitlab.ps1 - Deploy GitLab EE 16.7.10 via Docker
$OutFile = "$env:TEMP\openclaw-gitlab-setup.log"
$ErrorActionPreference = "Continue"

function Log($msg) {
    $ts = Get-Date -Format "HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Output $line
    $line | Out-File -Append -FilePath $OutFile -Encoding UTF8
}

# Clear log
"" | Out-File -FilePath $OutFile -Encoding UTF8

$image = "gitlab/gitlab-ee:16.7.10-ee.0"
$name = "gitlab-ee"
$gitlabHome = "C:\gitlab"

Log "Creating directories..."
New-Item -ItemType Directory -Force -Path "$gitlabHome\config" | Out-Null
New-Item -ItemType Directory -Force -Path "$gitlabHome\logs" | Out-Null
New-Item -ItemType Directory -Force -Path "$gitlabHome\data" | Out-Null

Log "Pulling $image (this may take a while)..."
docker pull $image 2>&1 | ForEach-Object { Log $_ }

Log "Stopping old container if exists..."
docker stop $name 2>$null | Out-Null
docker rm $name 2>$null | Out-Null

Log "Starting GitLab EE container..."
docker run --detach `
    --hostname gitlab.local `
    --name $name `
    --restart unless-stopped `
    --publish 8929:80 `
    --publish 8930:443 `
    --publish 2222:22 `
    --volume "$gitlabHome\config:/etc/gitlab" `
    --volume "$gitlabHome\logs:/var/log/gitlab" `
    --volume "$gitlabHome\data:/var/opt/gitlab" `
    --shm-size 256m `
    --env GITLAB_OMNIBUS_CONFIG="external_url 'http://localhost:8929'; gitlab_rails['initial_root_password'] = 'Gl@bT3st2026!'; gitlab_rails['gitlab_shell_ssh_port'] = 2222; prometheus_monitoring['enable'] = false; grafana['enable'] = false; alertmanager['enable'] = false; node_exporter['enable'] = false; redis_exporter['enable'] = false; postgres_exporter['enable'] = false; gitlab_exporter['enable'] = false; sidekiq['max_concurrency'] = 5; puma['worker_processes'] = 2; postgresql['shared_buffers'] = '128MB'" `
    $image 2>&1 | ForEach-Object { Log $_ }

Log "Container started. GitLab will take 3-5 min to fully boot."
Log "URL: http://localhost:8929"
Log "User: root"
Log "Password: Gl@bT3st2026!"
Log ""
Log "Waiting for GitLab to become healthy..."

$timeout = 600
$elapsed = 0
while ($elapsed -lt $timeout) {
    Start-Sleep -Seconds 10
    $elapsed += 10
    $health = docker inspect --format "{{.State.Health.Status}}" $name 2>$null
    if (-not $health) {
        $status = docker inspect --format "{{.State.Status}}" $name 2>$null
        Log "  [$elapsed`s] Container status: $status"
    } else {
        Log "  [$elapsed`s] Health: $health"
        if ($health -eq "healthy") {
            Log "GitLab is UP and healthy!"
            break
        }
    }
}

if ($elapsed -ge $timeout) {
    Log "Timed out waiting for healthy status, but GitLab may still be starting."
}

Log "Done. Check $OutFile for full log."
