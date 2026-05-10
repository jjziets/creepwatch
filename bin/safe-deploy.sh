#!/bin/sh
# Safe single-service compose deploy (issue #1): avoids accidental minecraft
# recreates when only updating mc-guard, and refuses while players are online.
#
# Usage: ./bin/safe-deploy.sh <service> [--force]
# Example: ./bin/safe-deploy.sh mc-guard

set -eu

service="${1:?service name required}"
shift

force=0
for arg in "$@"; do
    [ "$arg" = "--force" ] && force=1
done

players=$(docker exec minecraft rcon-cli list 2>/dev/null | sed -nE 's/^There are ([0-9]+).*/\1/p' || true)
players=${players:-0}

if [ "$players" -gt 0 ] && [ "$force" -eq 0 ]; then
    echo "❌ $players player(s) online; aborting. Pass --force to override."
    exit 1
fi

echo "▶ docker compose up -d --no-deps $service"
exec docker compose up -d --no-deps "$service"
