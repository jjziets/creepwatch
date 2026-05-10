#!/bin/sh
# Deploy only mc-guard after updating this repo on disk. Does not pull or
# recreate the minecraft container (use safe-deploy.sh / watchtower for that).
#
# Usage (on the compose host):
#   ./scripts/deploy-mc-guard.sh /absolute/path/to/compose-project
# Or:
#   CREEPWATCH_COMPOSE_DIR=/path ./scripts/deploy-mc-guard.sh
#
# Intended for GitHub Actions SSH deploy after merge to main.

set -eu

ROOT="${1:-${CREEPWATCH_COMPOSE_DIR:-}}"
if [ -z "$ROOT" ]; then
  echo "usage: $0 <compose-project-dir>  (or set CREEPWATCH_COMPOSE_DIR)" >&2
  exit 1
fi

cd "$ROOT"

if [ ! -d .git ]; then
  echo "error: not a git repository: $ROOT" >&2
  exit 1
fi

if [ ! -f docker-compose.yml ]; then
  echo "error: docker-compose.yml not found in $ROOT" >&2
  exit 1
fi

git fetch origin main
git checkout main
git pull --ff-only origin main

docker compose -f docker-compose.yml up -d --no-deps mc-guard

echo "deploy-mc-guard: done (mc-guard only; minecraft unchanged)."
