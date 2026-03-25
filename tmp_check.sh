#!/bin/bash
for i in $(seq 1 24); do
  code=$(curl -s --unix-socket /var/opt/gitlab/gitlab-rails/sockets/gitlab.socket -o /dev/null -w "%{http_code}" http://localhost/-/health)
  echo "attempt $i: $code"
  if [ "$code" = "200" ]; then exit 0; fi
  sleep 5
done
exit 1
