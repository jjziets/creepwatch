#!/bin/sh
# Restore Minecraft world volume from a tarball created by scripts/backup.sh.
# Stops the minecraft service, wipes the named volume mount at /data, extracts
# the archive, then starts minecraft again. Destructive — use only with care.
#
# Usage: restore.sh last | <minecraft-YYYYMMDDTHHMMSSZ.tar.gz>
# Env: BACKUP_DIR (default /backups), CREEPWATCH_PROJECT_DIR (host compose dir),
#      optional DOCKER_REAL for the real docker binary (same as backup.sh).

set -eu

docker_cli() {
  if [ -n "${DOCKER_REAL:-}" ] && [ -x "${DOCKER_REAL}" ]; then
    "${DOCKER_REAL}" "$@"
  else
    command docker "$@"
  fi
}

BACKUP_DIR="${BACKUP_DIR:-/backups}"
PROJECT="${CREEPWATCH_PROJECT_DIR:-}"

log() { printf '[%s restore] %s\n' "$(date -u +%FT%TZ)" "$*"; }

usage() {
  echo "Usage: $0 last | <minecraft-YYYYMMDDTHHMMSSZ.tar.gz>" >&2
  exit 2
}

validate_basename() {
  b=$1
  case "$b" in
    */*|*..*|'') return 1 ;;
  esac
  echo "$b" | grep -qE '^minecraft-[0-9]{8}T[0-9]{6}Z\.tar\.gz$' || return 1
  return 0
}

[ "$#" -eq 1 ] || usage
SPEC=$1

if [ -z "$PROJECT" ]; then
  log "FAIL: CREEPWATCH_PROJECT_DIR is not set"
  exit 1
fi

if ! [ -d "$PROJECT" ] || ! [ -f "$PROJECT/docker-compose.yml" ]; then
  log "FAIL: CREEPWATCH_PROJECT_DIR must contain docker-compose.yml"
  exit 1
fi

COMPOSE_FILE="$PROJECT/docker-compose.yml"

case "$SPEC" in
  last)
    # Filenames sort lexically by UTC timestamp; avoid test -nt (not POSIX sh).
    ARCHIVE=$(find "$BACKUP_DIR" -maxdepth 1 -name 'minecraft-*.tar.gz' -type f 2>/dev/null | sort -r | head -n 1)
    if [ -z "$ARCHIVE" ] || ! [ -f "$ARCHIVE" ]; then
      log "FAIL: no minecraft-*.tar.gz in $BACKUP_DIR"
      exit 1
    fi
    ARCHIVE_BN=$(basename "$ARCHIVE")
    ;;
  *)
    ARCHIVE_BN=$SPEC
    validate_basename "$ARCHIVE_BN" || usage
    ARCHIVE="$BACKUP_DIR/$ARCHIVE_BN"
    if ! [ -f "$ARCHIVE" ]; then
      log "FAIL: missing file $ARCHIVE"
      exit 1
    fi
    ;;
esac

log "stopping minecraft ($ARCHIVE_BN)"
if ! docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" stop minecraft; then
  log "FAIL: compose stop"
  exit 1
fi

log "wiping volume and extracting"
if ! docker_cli run --rm \
  -v minecraft_data:/data \
  -v "$BACKUP_DIR":/restore:ro \
  alpine:latest \
  sh -c "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf \"/restore/$ARCHIVE_BN\" -C /data"; then
  log "FAIL: extract"
  docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" start minecraft || true
  exit 1
fi

log "starting minecraft"
if ! docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" start minecraft; then
  log "FAIL: compose start"
  exit 1
fi

log "restore complete ($ARCHIVE_BN)"
