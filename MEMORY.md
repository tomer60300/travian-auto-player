# MEMORY.md - Long-Term Memory

_Created 2026-02-18. Starting fresh._

## Key Facts
- Human: Tomer, Israel, timezone Asia/Jerusalem
- Prefers straight talk, no fluff, Hebrew on WhatsApp

## Infrastructure
- **GitLab v16.7.10-ee** running in WSL2 Ubuntu-22.04 (omnibus). Docs: `docs/GITLAB.md`
  - Access: `http://<WSL_IP>` (IP changes on reboot, check with `hostname -I`)
  - Credentials: root / OWnuPZkqXLpCf5CvAMr1o0amBNw7R4C0/XBZ+pS8bzo=
  - WSL doesn't auto-start services — need `gitlab-ctl start` after reboot
  - Puma tuned: 2 workers, 600s timeout (default 60s too short for WSL)
  - Data: `/var/opt/gitlab/`, config: `/etc/gitlab/gitlab.rb`

## Events & Notes
- 2026-02-18: First real session. Memory files were missing — created them now.
- 2026-03-12: Installed GitLab EE 16.7.10 in WSL2. Docker approach failed (container exit 255 due to WSL kernel limitations). Omnibus worked but needed puma tuning (worker_timeout 600, 2 workers) because WSL is slow on preload.
