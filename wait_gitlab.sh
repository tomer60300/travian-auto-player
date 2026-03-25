#!/bin/bash
for i in $(seq 1 30); do
  sleep 10
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/ 2>/dev/null)
  echo "$(date +%H:%M:%S) HTTP $code"
  if [ "$code" = "302" ] || [ "$code" = "200" ]; then
    echo "GITLAB IS UP"
    exit 0
  fi
done
echo "TIMEOUT"
exit 1
