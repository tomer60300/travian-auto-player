@echo off
REM Start WSL + GitLab on boot
wsl -d Ubuntu-22.04 -u root -- bash -c "gitlab-ctl start && sleep 5 && netsh.exe interface portproxy add v4tov4 listenport=8929 listenaddress=0.0.0.0 connectport=80 connectaddress=$(hostname -I | awk '{print $1}')"
