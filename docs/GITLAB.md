# GitLab v16.7.10-ee — Local Instance

Installed: 2026-03-12
Platform: WSL2 Ubuntu-22.04 (omnibus package)

---

## Access

| | |
|---|---|
| **URL** | `http://<WSL_IP>` (see "Get WSL IP" below) |
| **Username** | `root` |
| **Initial Password** | `REDACTED - rotate this credential; it was committed to a public repository and remains in git history` |

> ⚠️ The WSL2 IP changes on reboot. Always check with the command below.

---

## How to Start

```powershell
# 1. Start WSL (if not running)
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl start

# 2. Get the WSL IP (use this in your browser)
wsl -d Ubuntu-22.04 -u root -- hostname -I
# First IP is the one you need, e.g. 172.29.191.242

# 3. Wait ~2 minutes for Puma to preload, then open:
#    http://<WSL_IP>
```

Puma takes about **2 minutes** to fully boot. You'll see 502 errors until it's ready.

To check if it's ready:
```powershell
wsl -d Ubuntu-22.04 -u root -- curl -sI http://localhost/
# Look for "HTTP/1.1 302 Found" = ready
# "HTTP/1.1 502 Bad Gateway" = still loading
```

---

## How to Stop

```powershell
# Stop all GitLab services
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl stop

# Or stop WSL entirely (stops everything)
wsl --shutdown
```

---

## Service Management

```powershell
# Status of all services
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl status

# Restart everything
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart

# Restart just Puma (web server)
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart puma

# View logs (live)
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl tail

# View just Puma logs
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl tail puma
```

---

## Stack

| Component | Purpose |
|---|---|
| **Nginx** | Reverse proxy, serves on port 80 |
| **Puma** | Ruby web server (Rails app), 2 workers |
| **Sidekiq** | Background job processor |
| **PostgreSQL** | Main database |
| **Redis** | Cache & queues |
| **Gitaly** | Git storage backend (gRPC) |
| **GitLab Workhorse** | Smart proxy for large files/Git HTTP |
| **GitLab KAS** | Kubernetes Agent Server |
| **GitLab Shell** | SSH access handler |
| **Prometheus** | Metrics (disabled in config, still runs) |

---

## Data Locations (inside WSL)

| Path | What | Size |
|---|---|---|
| `/etc/gitlab/` | Configuration (`gitlab.rb`) | 184K |
| `/var/opt/gitlab/` | **All application data** | 425M |
| `/var/opt/gitlab/postgresql/` | Database | 196M |
| `/var/opt/gitlab/gitaly/` | Git repositories | 206M |
| `/var/opt/gitlab/git-data/` | Git repo storage | 20K |
| `/var/opt/gitlab/backups/` | Backups | 4K |
| `/var/opt/gitlab/gitlab-rails/` | Rails working files, uploads, sockets | 172K |
| `/var/log/gitlab/` | All logs | 9.4M |
| `/opt/gitlab/` | Application binaries (omnibus) | 3.2G |

### Backup

```bash
# Create a backup
wsl -d Ubuntu-22.04 -u root -- gitlab-backup create

# Backups saved to: /var/opt/gitlab/backups/
```

---

## Configuration

Main config file: `/etc/gitlab/gitlab.rb` (inside WSL)

Current tuning for WSL performance:
```ruby
external_url 'http://localhost'
puma['worker_processes'] = 2
puma['min_threads'] = 1
puma['max_threads'] = 4
puma['worker_timeout'] = 600    # edited in puma.rb directly
sidekiq['max_concurrency'] = 5
prometheus_monitoring['enable'] = false
```

After editing `gitlab.rb`:
```bash
wsl -d Ubuntu-22.04 -u root -- gitlab-ctl reconfigure
```

> ⚠️ After reconfigure, check that `worker_timeout` in
> `/var/opt/gitlab/gitlab-rails/etc/puma.rb` is still `600` (not `60`).
> The omnibus template may reset it. If it does, run:
> ```bash
> wsl -d Ubuntu-22.04 -u root -- sed -i 's/worker_timeout 60$/worker_timeout 600/' /var/opt/gitlab/gitlab-rails/etc/puma.rb
> wsl -d Ubuntu-22.04 -u root -- gitlab-ctl restart puma
> ```

---

## Troubleshooting

**502 Bad Gateway after start:**
Puma takes ~2 min to preload. Wait and retry.

**Can't connect from Windows browser:**
WSL2 IP changes on reboot. Check with `hostname -I`.

**Services don't auto-start:**
WSL doesn't have systemd autostart by default. You need to manually run `gitlab-ctl start` after each WSL/machine reboot.

**Puma keeps restarting:**
Check `worker_timeout` in `/var/opt/gitlab/gitlab-rails/etc/puma.rb`. Must be `600` (not `60`). The default 60s is too short for WSL.
