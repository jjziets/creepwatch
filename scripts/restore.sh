#!/bin/sh
# Restore Minecraft world volume from a tarball created by scripts/backup.sh.
#
# DESTRUCTIVE: replaces the contents of the minecraft_data volume with the
# archive. To keep the live world recoverable if the chosen restore turns out
# to be wrong, step 1 always takes a `pre-restore-<TS>.tar.gz` safety
# snapshot of the *current* world before anything destructive happens. The
# script aborts if that snapshot fails — never wipe the live world without
# something on disk to fall back to.
#
# Pre-restore snapshots are kept in BACKUP_DIR alongside regular
# `minecraft-*.tar.gz` archives, but named `pre-restore-*.tar.gz` so they
# are not pruned by BACKUP_MAX_ARCHIVES. We keep the newest
# RESTORE_PRERESTORE_KEEP (default 3) of them.
#
# Usage:
#   restore.sh slots                         — list restore slots 1–3 (newest first) with UTC times
#   restore.sh last | 1 | 2 | 3 | <file>    — last / slot N = Nth newest; or explicit tarball basename
# Env: BACKUP_DIR (default /backups), CREEPWATCH_PROJECT_DIR (host compose dir),
#      optional DOCKER_REAL for the real docker binary (same as backup.sh).
# Progress / logs: writes structured PROGRESS lines to RESTORE_PROGRESS_FILE
# and a persistent log to RESTORE_LOG_FILE (default /data/logs/restore.log).
# Legacy: RESTORE_PROGRESS_CHAT_ID + TELEGRAM_BOT_TOKEN are no longer used
# directly here — mc-guard renders progress from the file.
# BACKUP_DOCKER_HOST_DIR: host path for docker run -v (mc-guard sets when BACKUP_DIR is /backups in-container).

set -u

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
RESTORE_TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESTORE_PRERESTORE_KEEP="${RESTORE_PRERESTORE_KEEP:-3}"
SAFETY_ARCHIVE="pre-restore-${RESTORE_TIMESTAMP}.tar.gz"

# --- Persistent log (same approach as backup.sh) -------------------------------
RESTORE_LOG_DIR="${RESTORE_LOG_DIR:-/data/logs}"
if ! mkdir -p "$RESTORE_LOG_DIR" 2>/dev/null; then
    RESTORE_LOG_DIR="/tmp/creepwatch-logs"
    mkdir -p "$RESTORE_LOG_DIR" 2>/dev/null || true
fi
RESTORE_LOG_FILE="${RESTORE_LOG_FILE:-$RESTORE_LOG_DIR/restore.log}"

log() {
    line=$(printf '[%s restore %s] %s' "$(date -u +%FT%TZ)" "$RESTORE_TIMESTAMP" "$*")
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "$RESTORE_LOG_FILE" 2>/dev/null || true
}

# Args: <step> <status: running|ok|fail|skip> [label] [detail]
progress() {
    p_step=${1:?progress: step required}
    p_status=${2:?progress: status required}
    p_label=${3:-}
    p_detail=${4:-}
    log "step=$p_step status=$p_status label=\"$p_label\" detail=\"$p_detail\""
    [ -z "${RESTORE_PROGRESS_FILE:-}" ] && return 0
    printf 'PROGRESS\t%s\t%s\t%s\t%s\n' \
        "$p_step" "$p_status" "$p_label" "$p_detail" \
        >> "$RESTORE_PROGRESS_FILE" 2>/dev/null || true
}

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
log "start: target=$ARCHIVE_BN safety=$SAFETY_ARCHIVE backup_dir=$BACKUP_DIR host_dir=$BACKUP_DOCKER_HOST_DIR"

# --- Step 1: safety snapshot of the CURRENT world ------------------------------
# We do this BEFORE stopping minecraft so the live volume is still mounted;
# we use save-all flush first to land any pending writes. We deliberately do
# not save-off here — the next step stops the container anyway.
progress 1 running "Safety snapshot of current world" "$SAFETY_ARCHIVE"

# Preflight: confirm the live container is running and has a real world. If
# not, refuse the restore — we don't want to wipe a healthy volume based on
# a misconfigured environment, and we don't want to "restore over" something
# that we can't take a fallback snapshot of.
running=$(docker_cli inspect -f '{{.State.Running}}' minecraft 2>/dev/null) || running=false
if [ "$running" != "true" ]; then
    progress 1 fail "Safety snapshot" "minecraft container not running — refusing to restore"
    log "FAIL: minecraft container not running (running=$running). Refusing restore."
    exit 1
fi
if ! docker_cli run --rm -v minecraft_data:/data:ro alpine:latest sh -c \
        'test -f /data/world/level.dat || test -f /data/level.dat'; then
    progress 1 fail "Safety snapshot" "no level.dat on live volume — refusing to restore"
    log "FAIL: no level.dat on live volume; refusing restore."
    exit 1
fi

docker_cli exec minecraft rcon-cli save-all flush >/dev/null 2>&1 || \
    log "WARN: save-all flush before safety snapshot failed (continuing)"

mkdir -p "$BACKUP_DIR" 2>/dev/null || true
safety_path="$BACKUP_DIR/$SAFETY_ARCHIVE"
if ! docker_cli run --rm \
        -v minecraft_data:/data:ro \
        -v "$BACKUP_DOCKER_HOST_DIR":/backups \
        alpine:latest \
        tar czf "/backups/$SAFETY_ARCHIVE" -C /data . 2>/dev/null; then
    progress 1 fail "Safety snapshot" "tar failed — refusing to wipe live world"
    log "FAIL: safety snapshot tar failed; refusing to restore."
    rm -f "$safety_path" 2>/dev/null || true
    exit 1
fi

if ! [ -f "$safety_path" ]; then
    progress 1 fail "Safety snapshot" "archive missing at $safety_path (host bind path?)"
    log "FAIL: safety snapshot not visible at $safety_path. Refusing to restore."
    exit 1
fi
safety_bytes=$(wc -c < "$safety_path" 2>/dev/null | tr -d ' ' || echo 0)
# Same floor as backup.sh: anything <1 MiB is almost certainly truncated.
if [ "$safety_bytes" -lt 1048576 ] 2>/dev/null; then
    progress 1 fail "Safety snapshot" "archive too small (${safety_bytes} bytes)"
    log "FAIL: safety snapshot too small ($safety_bytes bytes); refusing to restore."
    rm -f "$safety_path"
    exit 1
fi
safety_size=$(du -h "$safety_path" 2>/dev/null | cut -f1)
progress 1 ok "Safety snapshot of current world" "$SAFETY_ARCHIVE ($safety_size)"

# Trim older pre-restore snapshots — keep the newest RESTORE_PRERESTORE_KEEP.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'pre-restore-*.tar.gz' \
    | sort -r \
    | tail -n "+$((RESTORE_PRERESTORE_KEEP + 1))" \
    | while IFS= read -r f; do
        [ -z "$f" ] && continue
        rm -f "$f" && log "pruned pre-restore $f"
      done

# --- Step 2: stop minecraft ----------------------------------------------------
progress 2 running "Stop Minecraft"
log "stopping minecraft ($ARCHIVE_BN)"
if ! docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" stop minecraft; then
  progress 2 fail "Stop Minecraft" "compose stop failed"
  log "FAIL: compose stop"
  exit 1
fi
progress 2 ok "Stop Minecraft"

# --- Step 3: wipe volume and extract -------------------------------------------
progress 3 running "Wipe volume and extract backup" "$ARCHIVE_BN"
log "wiping volume and extracting"
if ! docker_cli run --rm \
  -v minecraft_data:/data \
  -v "$BACKUP_DOCKER_HOST_DIR":/restore:ro \
  alpine:latest \
  sh -c "find /data -mindepth 1 -maxdepth 1 -exec rm -rf {} + && tar xzf \"/restore/$ARCHIVE_BN\" -C /data"; then
  progress 3 fail "Extract" "tar failed — safety snapshot $SAFETY_ARCHIVE remains"
  log "FAIL: extract. Safety snapshot $SAFETY_ARCHIVE is still on disk for recovery."
  docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" start minecraft || true
  exit 1
fi
progress 3 ok "Wipe volume and extract backup" "$ARCHIVE_BN"

# --- Step 4: start minecraft ---------------------------------------------------
progress 4 running "Start Minecraft"
log "starting minecraft"
if ! docker_cli compose --project-directory "$PROJECT" -f "$COMPOSE_FILE" start minecraft; then
  progress 4 fail "Start Minecraft" "compose start failed"
  log "FAIL: compose start"
  exit 1
fi
progress 4 ok "Start Minecraft"

log "restore complete ($ARCHIVE_BN); safety snapshot kept as $SAFETY_ARCHIVE"
