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

# Sorted basenames, newest first (lexical sort matches the UTC timestamp).
find_backup_basenames() {
  find "$BACKUP_DIR" -maxdepth 1 -name 'minecraft-*.tar.gz' -type f -printf '%f\n' 2>/dev/null | sort -r
}

# Print YYYY-MM-DD HH:MM:SS UTC from basename, or empty if pattern mismatch.
human_utc_from_bn() {
  bn=$1
  echo "$bn" | sed -n 's/^minecraft-\([0-9][0-9][0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)T\([0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)Z\.tar\.gz$/\1-\2-\3 \4:\5:\6 UTC/p'
}

_archive_basename_to_epoch() {
    abe_bn=$1
    abe_iso=$(printf '%s\n' "$abe_bn" | sed -n 's/^minecraft-\([0-9][0-9][0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)T\([0-9][0-9]\)\([0-9][0-9]\)\([0-9][0-9]\)Z\.tar\.gz$/\1-\2-\3T\4:\5:\6Z/p')
    [ -z "$abe_iso" ] && return 1
    date -u -d "$abe_iso" +%s 2>/dev/null
}

# Read newest-first basenames on stdin; print only those forming a 24h-gap
# slot ladder. Mirrors mc-guard's pick_slot_archives so /restore <N> picks
# the same archive whether resolved in Python or by the host CLI.
slot_archives_24h_gap() {
    sa_max=${1:-3}
    sa_gap=${2:-86400}
    sa_last=0
    sa_n=0
    while IFS= read -r sa_bn; do
        [ -z "$sa_bn" ] && continue
        sa_ts=$(_archive_basename_to_epoch "$sa_bn") || continue
        if [ "$sa_last" -eq 0 ]; then
            printf '%s\n' "$sa_bn"
            sa_last=$sa_ts
            sa_n=$((sa_n + 1))
            [ "$sa_n" -ge "$sa_max" ] && break
        else
            sa_diff=$((sa_last - sa_ts))
            if [ "$sa_diff" -ge "$sa_gap" ]; then
                printf '%s\n' "$sa_bn"
                sa_last=$sa_ts
                sa_n=$((sa_n + 1))
                [ "$sa_n" -ge "$sa_max" ] && break
            fi
        fi
    done
}

sha256_hex() {
    sh_path=$1
    [ -f "$sh_path" ] || return 0
    sha256sum "$sh_path" 2>/dev/null | awk '{print $1}'
}

r2_object_key() {
    pref=${1:-}
    fn=$2
    case "$pref" in
        "") echo "$fn" ;;
        */) echo "${pref}${fn}" ;;
        *) echo "${pref}/${fn}" ;;
    esac
}

r2_fully_configured() {
    [ -n "${R2_BUCKET:-}" ] && [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ] && [ -n "${R2_S3_ENDPOINT:-}" ]
}

# Pluck R2 / retention env from .env when invoked outside mc-guard. Same
# pattern as backup.sh.
env_file="${CREEPWATCH_ENV_FILE:-/home/vast/minecraft/.env}"
if [ -f "$env_file" ]; then
    pluck() { grep -m1 "^$1=" "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '\r'; }
    [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && TELEGRAM_BOT_TOKEN=$(pluck TELEGRAM_BOT_TOKEN)
    [ -z "${ADMIN_CHAT_IDS:-}" ] && ADMIN_CHAT_IDS=$(pluck ADMIN_CHAT_IDS)
    [ -z "${R2_BUCKET:-}" ] && R2_BUCKET=$(pluck R2_BUCKET)
    [ -z "${R2_ACCESS_KEY_ID:-}" ] && R2_ACCESS_KEY_ID=$(pluck R2_ACCESS_KEY_ID)
    [ -z "${R2_SECRET_ACCESS_KEY:-}" ] && R2_SECRET_ACCESS_KEY=$(pluck R2_SECRET_ACCESS_KEY)
    [ -z "${R2_S3_ENDPOINT:-}" ] && R2_S3_ENDPOINT=$(pluck R2_S3_ENDPOINT)
    [ -z "${R2_PREFIX:-}" ] && R2_PREFIX=$(pluck R2_PREFIX)
    [ -z "${BACKUP_DOCKER_HOST_DIR:-}" ] && BACKUP_DOCKER_HOST_DIR=$(pluck BACKUP_DOCKER_HOST_DIR)
    export R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_S3_ENDPOINT R2_PREFIX BACKUP_DOCKER_HOST_DIR
fi

# Re-evaluate after pluck.
BACKUP_DOCKER_HOST_DIR="${BACKUP_DOCKER_HOST_DIR:-$BACKUP_DIR}"

# Print expected SHA-256 for an R2 object key, or empty if R2 has no
# `sha256` user metadata (older archives uploaded before this change).
r2_sha256_for_key() {
    rk=$1
    docker_cli run --rm \
        -e "AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
        -e "AWS_DEFAULT_REGION=auto" \
        amazon/aws-cli:latest \
        s3api head-object --bucket "$R2_BUCKET" --key "$rk" \
        --endpoint-url "$R2_S3_ENDPOINT" --region auto \
        --query 'Metadata.sha256' --output text 2>/dev/null \
        | awk 'NF && $0 != "None" {print; exit}'
}

# Download an R2 object to BACKUP_DOCKER_HOST_DIR (host path the daemon
# resolves). Returns 0 on success.
r2_download_to_local() {
    rk=$1
    fn=$2
    docker_cli run --rm \
        -e "AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
        -e "AWS_DEFAULT_REGION=auto" \
        -v "$BACKUP_DOCKER_HOST_DIR":/dl \
        amazon/aws-cli:latest \
        s3 cp "s3://${R2_BUCKET}/${rk}" "/dl/$fn" \
        --endpoint-url "$R2_S3_ENDPOINT" --region auto >/dev/null 2>&1
}

# --- slots (no compose / project needed) ---------------------------------------
if [ "$#" -eq 1 ] && [ "$1" = "slots" ]; then
  mkdir -p "$BACKUP_DIR" 2>/dev/null || true
  slot_list=$(find_backup_basenames | slot_archives_24h_gap 3)
  i=1
  while [ "$i" -le 3 ]; do
    bn=$(printf '%s\n' "$slot_list" | sed -n "${i}p")
    lab=""
    [ "$i" -eq 1 ] && lab=" (newest)"
    if [ -z "$bn" ]; then
      if [ "$i" -eq 1 ]; then
        printf 'Slot %s%s: (no backup)\n' "$i" "$lab"
      else
        printf 'Slot %s%s: (no backup ≥24h older than slot %d)\n' "$i" "$lab" "$((i - 1))"
      fi
    else
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

# Resolve last | 1 | 2 | 3 | basename → ARCHIVE (abs path) and ARCHIVE_BN.
# Slot resolution uses the same 24h-gap rule as mc-guard, so /restore 2
# always points at "newest archive ≥24h before slot 1".
ARCHIVE=""
ARCHIVE_BN=""
case "$SPEC" in
  last|1|2|3)
    idx=$SPEC
    case "$SPEC" in last) idx=1 ;; esac
    ARCHIVE_BN=$(find_backup_basenames | slot_archives_24h_gap 3 | sed -n "${idx}p")
    if [ -z "$ARCHIVE_BN" ]; then
      log "FAIL: slot $SPEC empty — no archive ≥24h older than slot $((idx - 1)) in $BACKUP_DIR"
      exit 1
    fi
    ARCHIVE="$BACKUP_DIR/$ARCHIVE_BN"
    ;;
  *)
    ARCHIVE_BN=$SPEC
    validate_basename "$ARCHIVE_BN" || usage
    ARCHIVE="$BACKUP_DIR/$ARCHIVE_BN"
    ;;
esac

# --- Verify or fetch from R2 ---------------------------------------------------
# Goal: never restore from a local file that we are not sure matches R2,
# but also never re-download a multi-hundred-MB archive when local already
# matches. R2 stores the local sha256 as user metadata at upload time.
local_present=0
[ -f "$ARCHIVE" ] && local_present=1
expected_sha=""
if r2_fully_configured; then
    R2_PREFIX_NORM=${R2_PREFIX:-minecraft/}
    r2_key=$(r2_object_key "$R2_PREFIX_NORM" "$ARCHIVE_BN")
    expected_sha=$(r2_sha256_for_key "$r2_key" || true)
fi

if [ -n "$expected_sha" ]; then
    if [ "$local_present" -eq 1 ]; then
        local_sha=$(sha256_hex "$ARCHIVE")
        if [ "$local_sha" = "$expected_sha" ]; then
            log "local matches R2 sha256 (local copy will be used)"
        else
            log "local sha256 mismatch (local=$local_sha r2=$expected_sha) — re-downloading from R2"
            if ! r2_download_to_local "$r2_key" "$ARCHIVE_BN"; then
                log "FAIL: R2 download for $ARCHIVE_BN"
                exit 1
            fi
            new_sha=$(sha256_hex "$ARCHIVE")
            if [ "$new_sha" != "$expected_sha" ]; then
                log "FAIL: post-download sha256 still mismatch (got=$new_sha expected=$expected_sha) — refusing to restore"
                exit 1
            fi
            log "R2 download verified ok"
        fi
    else
        log "local missing — fetching from R2 ($r2_key)"
        if ! r2_download_to_local "$r2_key" "$ARCHIVE_BN"; then
            log "FAIL: R2 download for $ARCHIVE_BN"
            exit 1
        fi
        new_sha=$(sha256_hex "$ARCHIVE")
        if [ "$new_sha" != "$expected_sha" ]; then
            log "FAIL: downloaded sha256 mismatch (got=$new_sha expected=$expected_sha) — refusing to restore"
            exit 1
        fi
        log "R2 download verified ok"
    fi
elif r2_fully_configured && [ "$local_present" -eq 0 ]; then
    # R2 reachable but no sha metadata (older upload). Pull anyway and trust
    # the gzip integrity check we do post-extract.
    log "R2 has no sha256 metadata for $ARCHIVE_BN — downloading without checksum verify"
    R2_PREFIX_NORM=${R2_PREFIX:-minecraft/}
    r2_key=$(r2_object_key "$R2_PREFIX_NORM" "$ARCHIVE_BN")
    if ! r2_download_to_local "$r2_key" "$ARCHIVE_BN"; then
        log "FAIL: R2 download for $ARCHIVE_BN"
        exit 1
    fi
elif [ "$local_present" -eq 0 ]; then
    log "FAIL: missing file $ARCHIVE and R2 not configured"
    exit 1
fi

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
