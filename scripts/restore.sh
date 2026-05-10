#!/bin/sh
# Restore Minecraft world volume from a tarball created by scripts/backup.sh.
# Stops the minecraft service, wipes the named volume mount at /data, extracts
# the archive, then starts minecraft again. Destructive — use only with care.
#
# Usage:
#   restore.sh slots                         — list restore slots 1–3 (newest first) with UTC times
#   restore.sh last | 1 | 2 | 3 | <file>    — last / slot N = Nth newest; or explicit tarball basename
# Env: BACKUP_DIR (default /backups), CREEPWATCH_PROJECT_DIR (host compose dir),
#      optional DOCKER_REAL for the real docker binary (same as backup.sh).
# Optional RESTORE_PROGRESS_CHAT_ID + TELEGRAM_BOT_TOKEN for Telegram stage lines (mc-guard sets).
# BACKUP_DOCKER_HOST_DIR: host path for docker run -v (mc-guard sets when BACKUP_DIR is /backups in-container).

set -eu

docker_cli() {
  if [ -n "${DOCKER_REAL:-}" ] && [ -x "${DOCKER_REAL}" ]; then
    "${DOCKER_REAL}" "$@"
  else
    command docker "$@"
  fi
}

BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_DOCKER_HOST_DIR="${BACKUP_DOCKER_HOST_DIR:-$BACKUP_DIR}"
PROJECT="${CREEPWATCH_PROJECT_DIR:-}"

log() { printf '[%s restore] %s\n' "$(date -u +%FT%TZ)" "$*"; }

usage() {
  echo "Usage: $0 slots | last | 1 | 2 | 3 | <minecraft-YYYYMMDDTHHMMSSZ.tar.gz>" >&2
  exit 2
}

# Sorted paths, newest first (lexical sort matches UTC timestamp in filename).
find_backup_archives() {
  find "$BACKUP_DIR" -maxdepth 1 -name 'minecraft-*.tar.gz' -type f 2>/dev/null | sort -r
}

# Print YYYY-MM-DD HH:MM:SS UTC from basename, or empty if pattern mismatch.
human_utc_from_bn() {
  bn=$1
  echo "$bn" | sed -n 's/^minecraft-\([0-9][0-9][0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)T\([0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)Z\.tar\.gz$/\1-\2-\3 \4:\5:\6 UTC/p'
}

# --- slots (no compose / project needed) ---------------------------------------
if [ "$#" -eq 1 ] && [ "$1" = "slots" ]; then
  mkdir -p "$BACKUP_DIR" 2>/dev/null || true
  i=1
  while [ "$i" -le 3 ]; do
    path=$(find_backup_archives | sed -n "${i}p")
    lab=""
    [ "$i" -eq 1 ] && lab=" (newest)"
    if [ -z "$path" ] || ! [ -f "$path" ]; then
      printf 'Slot %s%s: (no backup)\n' "$i" "$lab"
    else
      bn=$(basename "$path")
      utc=$(human_utc_from_bn "$bn")
      [ -z "$utc" ] && utc="(parse filename for UTC time)"
      printf 'Slot %s%s: %s — %s\n' "$i" "$lab" "$bn" "$utc"
    fi
    i=$((i + 1))
  done
  exit 0
fi

[ "$#" -eq 1 ] || usage
SPEC=$1

validate_basename() {
  b=$1
  case "$b" in
    */*|*..*|'') return 1 ;;
  esac
  echo "$b" | grep -qE '^minecraft-[0-9]{8}T[0-9]{6}Z\.tar\.gz$' || return 1
  return 0
}

# Single-chat progress (mc-guard sets RESTORE_PROGRESS_CHAT_ID for /restore).
progress_ping() {
  text=$1
  [ -z "${RESTORE_PROGRESS_CHAT_ID:-}" ] && return 0
  [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && return 0
  case "$RESTORE_PROGRESS_CHAT_ID" in *[!0-9]*) return 0 ;; esac
  log "progress telegram: $text"
  curl -s -m 8 -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=$RESTORE_PROGRESS_CHAT_ID" \
    --data-urlencode "text=$text" >/dev/null 2>&1 || true
}

if [ -z "$PROJECT" ]; then
  log "FAIL: CREEPWATCH_PROJECT_DIR is not set"
  exit 1
fi

if ! [ -d "$PROJECT" ] || ! [ -f "$PROJECT/docker-compose.yml" ]; then
  log "FAIL: CREEPWATCH_PROJECT_DIR must contain docker-compose.yml"
  exit 1
fi

COMPOSE_FILE="$PROJECT/docker-compose.yml"

# Resolve last | 1 | 2 | 3 | basename → ARCHIVE (abs path) and ARCHIVE_BN
ARCHIVE=""
ARCHIVE_BN=""
case "$SPEC" in
  last|1|2|3)
    idx=$SPEC
    case "$SPEC" in last) idx=1 ;; esac
    ARCHIVE=$(find_backup_archives | sed -n "${idx}p")
    if [ -z "$ARCHIVE" ] || ! [ -f "$ARCHIVE" ]; then
      log "FAIL: no backup for slot/spec $SPEC (not enough archives in $BACKUP_DIR)"
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

log "restore target $ARCHIVE_BN ($ARCHIVE)"
progress_ping "Starting restore task — $ARCHIVE_BN"

log "stopping minecraft ($ARCHIVE_BN)"
progress_ping "Stopping Minecraft container…"
if ! docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" stop minecraft; then
  log "FAIL: compose stop"
  progress_ping "Restore failed: could not stop Minecraft."
  exit 1
fi

log "wiping volume and extracting"
progress_ping "Clearing world volume and extracting backup (may take several minutes)…"
if ! docker_cli run --rm \
  -v minecraft_data:/data \
  -v "$BACKUP_DOCKER_HOST_DIR":/restore:ro \
  alpine:latest \
  sh -c "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf \"/restore/$ARCHIVE_BN\" -C /data"; then
  log "FAIL: extract"
  progress_ping "Restore failed during extract — starting Minecraft again."
  docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" start minecraft || true
  exit 1
fi

progress_ping "Extract finished — starting Minecraft…"

log "starting minecraft"
if ! docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" start minecraft; then
  log "FAIL: compose start"
  progress_ping "Restore failed: extract OK but Minecraft would not start."
  exit 1
fi

log "restore complete ($ARCHIVE_BN)"
progress_ping "Restore done — $ARCHIVE_BN applied; Minecraft starting (watch server logs for ready)."
