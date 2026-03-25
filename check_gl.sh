#!/bin/bash
for i in $(seq 1 10); do
  sleep 30
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost)
  echo "Check $i: HTTP $code"
  if [ "$code" = "200" ] || [ "$code" = "302" ]; then
    echo "GITLAB READY"
    cat /etc/gitlab/initial_root_password 2>/dev/null | grep Password
    exit 0
  fi
done
echo "NOT READY after 5 min"
gitlab-ctl status | head -5
