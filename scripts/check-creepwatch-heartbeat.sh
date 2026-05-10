#!/bin/sh
# Host-side stale check for mc-guard (issue #6). The bot writes a Unix timestamp
# to /data/.creepwatch_heartbeat inside the container (same path on the host if
# ./data is bind-mounted).
#
# Usage:
#   scripts/check-creepwatch-heartbeat.sh /path/to/data/.creepwatch_heartbeat [max_age_sec]
#
# Example cron (every 15 minutes, fail if older than 30 minutes):
#   */15 * * * * /home/vast/minecraft/scripts/check-creepwatch-heartbeat.sh /home/vast/minecraft/data/.creepwatch_heartbeat >>/var/log/creepwatch-heartbeat.log 2>&1 || logger -t creepwatch "mc-guard heartbeat stale"

set -eu

file="${1:?path to .creepwatch_heartbeat required}"
max_age_sec="${2:-1800}"

if [ ! -f "$file" ]; then
  echo "creepwatch-heartbeat: missing file: $file" >&2
  exit 1
fi

stamp=$(tr -d '\r\n' <"$file" 2>/dev/null || true)
case "$stamp" in ''|*[!0-9]*) stamp=0 ;; esac

now=$(date +%s)
age=$((now - stamp))
if [ "$age" -lt 0 ]; then
  age=0
fi

if [ "$age" -gt "$max_age_sec" ]; then
  echo "creepwatch-heartbeat: STALE — age=${age}s max=${max_age_sec}s file=$file stamp=$stamp" >&2
  exit 1
fi

exit 0
