#!/bin/bash
set -e
LOG=/tmp/gitlab-deploy.log
echo "" > $LOG

log() { echo "[$(date +%H:%M:%S)] $1" | tee -a $LOG; }

log "=== GitLab EE 16.7.10 Deployment ==="

log "Installing Docker..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq 2>&1 | tail -1 | tee -a $LOG
apt-get install -y -qq ca-certificates curl gnupg lsb-release 2>&1 | tail -1 | tee -a $LOG
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -qq 2>&1 | tail -1 | tee -a $LOG
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin 2>&1 | tail -1 | tee -a $LOG

log "Starting Docker daemon..."
service docker start 2>&1 | tee -a $LOG
docker --version 2>&1 | tee -a $LOG

log "Pulling GitLab EE 16.7.10..."
docker pull gitlab/gitlab-ee:16.7.10-ee.0 2>&1 | tee -a $LOG

log "Creating volumes..."
mkdir -p /srv/gitlab/{config,logs,data}

log "Removing old container if exists..."
docker stop gitlab-ee 2>/dev/null || true
docker rm gitlab-ee 2>/dev/null || true

log "Starting GitLab EE container..."
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
    gitlab/gitlab-ee:16.7.10-ee.0 2>&1 | tee -a $LOG

log "Waiting for GitLab to become healthy (up to 10 min)..."
for i in $(seq 1 60); do
    sleep 10
    health=$(docker inspect --format '{{.State.Health.Status}}' gitlab-ee 2>/dev/null || echo "starting")
    log "  [${i}0s] Health: $health"
    if [ "$health" = "healthy" ]; then
        log "=== GitLab is UP! ==="
        log "URL: http://localhost:8929"
        log "User: root"
        log "Password: Gl@bT3st2026!"
        exit 0
    fi
done

log "Timed out but GitLab may still be booting. Check: docker logs gitlab-ee"
