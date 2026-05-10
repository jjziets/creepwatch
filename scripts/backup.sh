#!/bin/sh
# Daily Minecraft world backup.
#
# Quiesces the world over RCON (save-all flush + save-off), tars the
# minecraft_data volume from an ephemeral alpine container, then re-enables
# autosave. Players are not kicked. The save-on step is unconditional so we
# never leave the world frozen, even if the snapshot fails.
#
# Progress: writes structured events to BACKUP_PROGRESS_FILE (TSV lines:
# `PROGRESS\tstep\tstatus\tlabel\tdetail`) so mc-guard can edit a single
# Telegram message instead of spamming one line per stage. Also appends a
# human log line to BACKUP_LOG_FILE (default /data/logs/backup.log) so we
# can post-mortem failures from past runs.
#
# Failures are reported to Telegram when TELEGRAM_BOT_TOKEN and ADMIN_CHAT_IDS
# are set (Compose env) or plucked from CREEPWATCH_ENV_FILE (host cron) or the
# legacy default path. Every failure is also written to stderr for cron/journald.
#
# Preflight (before save-off): minecraft container running, world level.dat on
# the volume, and enough free disk under BACKUP_DIR. Skips backup if unhealthy.
# Post-tar: by default `gzip -l` (header read only — safe for huge worlds) using
# BACKUP_GZIP_TOOLS_IMAGE (GNU gzip; Alpine BusyBox gzip has no -l). Set
# BACKUP_STRICT_GZIP_TEST=1 to use `gzip -t` (reads entire archive; can OOM/timeout).
#
# Optional Cloudflare R2 (S3-compatible): set R2_BUCKET, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY, R2_S3_ENDPOINT (https://<accountid>.r2.cloudflarestorage.com).
# After a successful tarball, uploads with aws-cli in Docker, then deletes older
# objects so at most R2_RETAIN_COUNT archives remain (default 3).
# Optional BACKUP_MAX_ARCHIVES: keep only that many newest local tarballs (else
# prune by BACKUP_RETENTION_DAYS, default 7).
#
# BACKUP_DOCKER_HOST_DIR: host path for docker run -v bind mounts. mc-guard sets this
# when BACKUP_DIR is a container path (/backups); the Docker daemon resolves -v on the host.

set -u

docker_cli() {
    if [ -n "${DOCKER_REAL:-}" ] && [ -x "${DOCKER_REAL}" ]; then
        "${DOCKER_REAL}" "$@"
    else
        command docker "$@"
    fi
}

BACKUP_DIR="${BACKUP_DIR:-/home/vast/minecraft/backups}"
RETAIN_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE_NAME="minecraft-${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

env_file="${CREEPWATCH_ENV_FILE:-/home/vast/minecraft/.env}"
if [ -f "$env_file" ]; then
    # Compose-style .env values may contain spaces, so don't `source` them.
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
        TELEGRAM_BOT_TOKEN=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$env_file" | cut -d= -f2-)
    fi
    if [ -z "${ADMIN_CHAT_IDS:-}" ]; then
        ADMIN_CHAT_IDS=$(grep -m1 '^ADMIN_CHAT_IDS=' "$env_file" | cut -d= -f2-)
    fi
    export TELEGRAM_BOT_TOKEN ADMIN_CHAT_IDS
    # Optional R2 / retention (same line-based pluck; do not source the whole file).
    pluck() { grep -m1 "^$1=" "$env_file" 2>/dev/null | cut -d= -f2- | tr -d '\r'; }
    [ -z "${R2_BUCKET:-}" ] && R2_BUCKET=$(pluck R2_BUCKET)
    [ -z "${R2_ACCESS_KEY_ID:-}" ] && R2_ACCESS_KEY_ID=$(pluck R2_ACCESS_KEY_ID)
    [ -z "${R2_SECRET_ACCESS_KEY:-}" ] && R2_SECRET_ACCESS_KEY=$(pluck R2_SECRET_ACCESS_KEY)
    [ -z "${R2_S3_ENDPOINT:-}" ] && R2_S3_ENDPOINT=$(pluck R2_S3_ENDPOINT)
    [ -z "${R2_PREFIX:-}" ] && R2_PREFIX=$(pluck R2_PREFIX)
    [ -z "${R2_RETAIN_COUNT:-}" ] && R2_RETAIN_COUNT=$(pluck R2_RETAIN_COUNT)
    [ -z "${BACKUP_MAX_ARCHIVES:-}" ] && BACKUP_MAX_ARCHIVES=$(pluck BACKUP_MAX_ARCHIVES)
    [ -z "${BACKUP_SKIP_PREFLIGHT:-}" ] && BACKUP_SKIP_PREFLIGHT=$(pluck BACKUP_SKIP_PREFLIGHT)
    [ -z "${BACKUP_MIN_FREE_MB:-}" ] && BACKUP_MIN_FREE_MB=$(pluck BACKUP_MIN_FREE_MB)
    [ -z "${BACKUP_MIN_ARCHIVE_BYTES:-}" ] && BACKUP_MIN_ARCHIVE_BYTES=$(pluck BACKUP_MIN_ARCHIVE_BYTES)
    [ -z "${BACKUP_STRICT_GZIP_TEST:-}" ] && BACKUP_STRICT_GZIP_TEST=$(pluck BACKUP_STRICT_GZIP_TEST)
    [ -z "${BACKUP_GZIP_TOOLS_IMAGE:-}" ] && BACKUP_GZIP_TOOLS_IMAGE=$(pluck BACKUP_GZIP_TOOLS_IMAGE)
    [ -z "${BACKUP_DOCKER_HOST_DIR:-}" ] && BACKUP_DOCKER_HOST_DIR=$(pluck BACKUP_DOCKER_HOST_DIR)
    [ -z "${BACKUP_LOG_DIR:-}" ] && BACKUP_LOG_DIR=$(pluck BACKUP_LOG_DIR)
    export R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_S3_ENDPOINT R2_PREFIX R2_RETAIN_COUNT BACKUP_MAX_ARCHIVES
    export BACKUP_SKIP_PREFLIGHT BACKUP_MIN_FREE_MB BACKUP_MIN_ARCHIVE_BYTES BACKUP_STRICT_GZIP_TEST BACKUP_GZIP_TOOLS_IMAGE BACKUP_DOCKER_HOST_DIR BACKUP_LOG_DIR
fi

# --- Persistent log: keep recent runs across container restarts so the
# /logs command in mc-guard can show history. /data is bind-mounted from the
# host; falls back to /tmp for host invocations (cron / systemd timer).
BACKUP_LOG_DIR="${BACKUP_LOG_DIR:-/data/logs}"
if ! mkdir -p "$BACKUP_LOG_DIR" 2>/dev/null; then
    BACKUP_LOG_DIR="/tmp/creepwatch-logs"
    mkdir -p "$BACKUP_LOG_DIR" 2>/dev/null || true
fi
BACKUP_LOG_FILE="${BACKUP_LOG_FILE:-$BACKUP_LOG_DIR/backup.log}"

log() {
    line=$(printf '[%s backup %s] %s' "$(date -u +%FT%TZ)" "$TIMESTAMP" "$*")
    printf '%s\n' "$line"
    printf '%s\n' "$line" >> "$BACKUP_LOG_FILE" 2>/dev/null || true
}

# Emit a structured progress event so mc-guard can render a step board.
# Args: <step> <status: running|ok|fail|skip> [label] [detail]
# Label/detail must not contain tabs. mc-guard tails the file by offset.
progress() {
    p_step=${1:?progress: step required}
    p_status=${2:?progress: status required}
    p_label=${3:-}
    p_detail=${4:-}
    log "step=$p_step status=$p_status label=\"$p_label\" detail=\"$p_detail\""
    [ -z "${BACKUP_PROGRESS_FILE:-}" ] && return 0
    printf 'PROGRESS\t%s\t%s\t%s\t%s\n' \
        "$p_step" "$p_status" "$p_label" "$p_detail" \
        >> "$BACKUP_PROGRESS_FILE" 2>/dev/null || true
}

# GNU gzip (BusyBox gzip in alpine:latest does not support "gzip -l").
BACKUP_GZIP_TOOLS_IMAGE="${BACKUP_GZIP_TOOLS_IMAGE:-debian:bookworm-slim}"
# docker run -v source path is always on the Docker host (see header).
BACKUP_DOCKER_HOST_DIR="${BACKUP_DOCKER_HOST_DIR:-$BACKUP_DIR}"

notify_failure() {
    text="$1"
    log "FAIL notify: $text"
    printf '%s\n' "$text" >&2
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${ADMIN_CHAT_IDS:-}" ]; then
        return 0
    fi
    saved_ifs=$IFS
    IFS=','
    for chat in $ADMIN_CHAT_IDS; do
        curl -s -m 5 -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=$chat" \
            --data-urlencode "text=$text" >/dev/null 2>&1 || true
    done
    IFS=$saved_ifs
}

# --- Preflight: do not touch save-off / tar if the host or volume looks bad -----
BACKUP_MIN_FREE_MB="${BACKUP_MIN_FREE_MB:-512}"
BACKUP_MIN_ARCHIVE_BYTES="${BACKUP_MIN_ARCHIVE_BYTES:-1048576}"

preflight_backup() {
    if [ -n "${BACKUP_SKIP_PREFLIGHT:-}" ]; then
        log "preflight skipped (BACKUP_SKIP_PREFLIGHT set)"
        return 0
    fi
    log "preflight: docker state for minecraft"
    running=$(docker_cli inspect -f '{{.State.Running}}' minecraft 2>/dev/null) || running=false
    if [ "$running" != "true" ]; then
        log "FAIL: minecraft container not running (running=$running)"
        return 1
    fi
    log "preflight: world data on volume minecraft_data"
    if ! docker_cli run --rm -v minecraft_data:/data:ro alpine:latest sh -c \
        'test -f /data/world/level.dat || test -f /data/level.dat'; then
        log "FAIL: no level.dat under /data/world or /data (volume empty or corrupt?)"
        return 1
    fi
    log "preflight: free disk for $BACKUP_DIR"
    need_kb=$((BACKUP_MIN_FREE_MB * 1024))
    avail_kb=$(df -Pk "$BACKUP_DIR" 2>/dev/null | awk 'END{print $4}')
    if [ -z "$avail_kb" ] || ! [ "$avail_kb" -ge 0 ] 2>/dev/null; then
        log "FAIL: could not read free space (df)"
        return 1
    fi
    if [ "$avail_kb" -lt "$need_kb" ]; then
        log "FAIL: low disk (${avail_kb} KiB free, need >= ${need_kb} KiB — set BACKUP_MIN_FREE_MB to tune)"
        return 1
    fi
    return 0
}

log "start: archive=$ARCHIVE_NAME backup_dir=$BACKUP_DIR host_dir=$BACKUP_DOCKER_HOST_DIR"

progress 1 running "Preflight"
if ! preflight_backup; then
    progress 1 fail "Preflight" "container not running, missing level.dat, or low disk"
    notify_failure "🚨 Minecraft backup skipped — preflight failed (container not running, missing level.dat on volume, or low disk). No snapshot taken."
    exit 1
fi
progress 1 ok "Preflight"

progress 2 running "Save-all flush"
log "save-all flush"
if ! docker_cli exec minecraft rcon-cli save-all flush >/dev/null 2>&1; then
    progress 2 fail "Save-all flush" "RCON failed"
    log "FAIL: save-all flush"
    notify_failure "🚨 Minecraft backup failed at save-all flush (RCON). Backup not created — check server logs."
    exit 1
fi
progress 2 ok "Save-all flush"

progress 3 running "Save-off (pause writes)"
log "save-off"
if ! docker_cli exec minecraft rcon-cli save-off >/dev/null 2>&1; then
    progress 3 fail "Save-off" "RCON failed"
    log "FAIL: save-off"
    notify_failure "🚨 Minecraft backup failed at save-off (RCON). Backup not created."
    docker_cli exec minecraft rcon-cli save-on >/dev/null 2>&1 || true
    exit 1
fi
progress 3 ok "Save-off (writes paused, server still up)"

progress 4 running "Compress world" "$ARCHIVE_NAME"
log "snapshotting volume to $ARCHIVE_NAME"
backup_ok=1
tar_err=$(mktemp)
if ! docker_cli run --rm \
        -v minecraft_data:/data:ro \
        -v "$BACKUP_DOCKER_HOST_DIR":/backups \
        alpine:latest \
        tar czf "/backups/$ARCHIVE_NAME" -C /data . 2>"$tar_err"; then
    backup_ok=0
    log "FAIL: tar"
    tar_tail=$(tail -c 400 "$tar_err" 2>/dev/null | tr '\n' ' ' || true)
    log "tar stderr tail: $tar_tail"
fi
rm -f "$tar_err"

# Always resume autosave, even if tar failed. Otherwise the world is frozen.
progress 5 running "Save-on (resume writes)"
log "save-on"
saveon_ok=1
if ! docker_cli exec minecraft rcon-cli save-on >/dev/null 2>&1; then
    saveon_ok=0
    log "FAIL: save-on after snapshot"
fi

if [ "$backup_ok" -ne 1 ]; then
    progress 4 fail "Compress world" "tar exited non-zero — see backup.log"
    if [ "$saveon_ok" -eq 1 ]; then
        progress 5 ok "Save-on (resume writes)"
    else
        progress 5 fail "Save-on" "RCON failed — autosave may still be off"
    fi
    notify_failure "🚨 Minecraft backup tar failed. Check disk, volume, and docker logs. save-on was attempted."
    [ "$saveon_ok" -eq 1 ] || notify_failure "🚨 Minecraft backup: tar failed and save-on also failed — use console/RCON save-on if needed."
    exit 1
fi

if [ "$saveon_ok" -ne 1 ]; then
    progress 4 ok "Compress world" "$ARCHIVE_NAME"
    progress 5 fail "Save-on" "RCON failed — autosave may still be off"
    notify_failure "🚨 Minecraft backup: tarball OK on disk but save-on failed — autosave may still be off; fix via RCON save-on or restart."
    exit 1
fi
progress 5 ok "Save-on (resume writes)"

archive_path="$BACKUP_DIR/$ARCHIVE_NAME"
if ! [ -f "$archive_path" ]; then
    progress 4 fail "Compress world" "archive missing at $archive_path (host bind path?)"
    log "FAIL: tarball missing at $archive_path after tar (check BACKUP_DOCKER_HOST_DIR=$BACKUP_DOCKER_HOST_DIR vs BACKUP_DIR=$BACKUP_DIR)"
    notify_failure "🚨 Minecraft backup failed: archive not visible at expected path after snapshot (docker host bind path misconfigured?)."
    exit 1
fi
archive_bytes=$(wc -c < "$archive_path" 2>/dev/null | tr -d ' ' || echo 0)
size=$(du -h "$archive_path" 2>/dev/null | cut -f1)
progress 4 ok "Compress world" "$ARCHIVE_NAME ($size)"

progress 6 running "Verify archive"
if ! [ "$archive_bytes" -ge "$BACKUP_MIN_ARCHIVE_BYTES" ] 2>/dev/null; then
    progress 6 fail "Verify archive" "too small (${archive_bytes} bytes)"
    rm -f "$archive_path"
    notify_failure "🚨 Minecraft backup rejected: archive too small (${archive_bytes} bytes, min ${BACKUP_MIN_ARCHIVE_BYTES}) — possible corruption or empty world; file removed."
    exit 1
fi

if [ -n "${BACKUP_STRICT_GZIP_TEST:-}" ]; then
    log "postflight: gzip -t full test on $ARCHIVE_NAME (BACKUP_STRICT_GZIP_TEST)"
    if ! docker_cli run --rm -v "$BACKUP_DOCKER_HOST_DIR":/backups:ro "$BACKUP_GZIP_TOOLS_IMAGE" gzip -t "/backups/$ARCHIVE_NAME" 2>/dev/null; then
        progress 6 fail "Verify archive" "gzip -t failed (corrupt)"
        rm -f "$archive_path"
        notify_failure "🚨 Minecraft backup rejected: gzip -t failed (truncated or corrupt tarball); file removed."
        exit 1
    fi
else
    log "postflight: gzip header check on $ARCHIVE_NAME (set BACKUP_STRICT_GZIP_TEST=1 for full -t)"
    if ! docker_cli run --rm -v "$BACKUP_DOCKER_HOST_DIR":/backups:ro "$BACKUP_GZIP_TOOLS_IMAGE" gzip -l "/backups/$ARCHIVE_NAME" >/dev/null 2>&1; then
        progress 6 fail "Verify archive" "gzip header unreadable"
        rm -f "$archive_path"
        notify_failure "🚨 Minecraft backup rejected: gzip header unreadable (truncated or not gzip); file removed."
        exit 1
    fi
fi
progress 6 ok "Verify archive" "gzip OK · $size"

log "backup complete ($size)"

# --- Optional Cloudflare R2 upload + remote retention -----------------------------
r2_fully_configured() {
    [ -n "${R2_BUCKET:-}" ] && [ -n "${R2_ACCESS_KEY_ID:-}" ] && [ -n "${R2_SECRET_ACCESS_KEY:-}" ] && [ -n "${R2_S3_ENDPOINT:-}" ]
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

progress 7 running "R2 upload"
if r2_fully_configured; then
    R2_PREFIX_NORM=${R2_PREFIX:-minecraft/}
    R2_KEEP=${R2_RETAIN_COUNT:-3}
    r2_key=$(r2_object_key "$R2_PREFIX_NORM" "$ARCHIVE_NAME")
    log "uploading to R2 s3://$R2_BUCKET/$r2_key"
    if ! docker_cli run --rm \
        -e "AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
        -e "AWS_DEFAULT_REGION=auto" \
        -v "$BACKUP_DOCKER_HOST_DIR":/backups:ro \
        amazon/aws-cli:latest \
        s3 cp "/backups/$ARCHIVE_NAME" "s3://${R2_BUCKET}/${r2_key}" \
        --endpoint-url "$R2_S3_ENDPOINT" \
        --region auto >/dev/null 2>&1; then
        progress 7 fail "R2 upload" "aws-cli s3 cp failed"
        log "FAIL: R2 upload"
        notify_failure "🚨 Minecraft backup: local tarball OK but R2 upload failed (check R2_* env and bucket policy)."
        exit 1
    fi
    log "R2 upload ok; pruning remote (keep newest $R2_KEEP)"
    if ! r2_ls=$(docker_cli run --rm \
        -e "AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
        -e "AWS_DEFAULT_REGION=auto" \
        amazon/aws-cli:latest \
        s3 ls "s3://${R2_BUCKET}/${R2_PREFIX_NORM}" \
        --endpoint-url "$R2_S3_ENDPOINT" \
        --region auto 2>/dev/null); then
        log "WARN: R2 list failed; remote prune skipped"
        r2_ls=""
    fi
    printf '%s\n' "$r2_ls" | awk '$4 ~ /^minecraft-[0-9]{8}T[0-9]{6}Z\.tar\.gz$/ { print $4 }' | sort -r | tail -n "+$((R2_KEEP + 1))" | while IFS= read -r k; do
        [ -z "$k" ] && continue
        r2_del=$(r2_object_key "$R2_PREFIX_NORM" "$k")
        log "R2 delete s3://$R2_BUCKET/$r2_del"
        docker_cli run --rm \
            -e "AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
            -e "AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
            -e "AWS_DEFAULT_REGION=auto" \
            amazon/aws-cli:latest \
            s3 rm "s3://${R2_BUCKET}/${r2_del}" \
            --endpoint-url "$R2_S3_ENDPOINT" \
            --region auto >/dev/null 2>&1 || log "WARN: R2 rm failed for $k"
    done
    progress 7 ok "R2 upload" "s3://${R2_BUCKET}/${r2_key} · kept ${R2_KEEP}"
else
    if [ -n "${R2_BUCKET:-}" ] || [ -n "${R2_ACCESS_KEY_ID:-}" ] || [ -n "${R2_SECRET_ACCESS_KEY:-}" ] || [ -n "${R2_S3_ENDPOINT:-}" ]; then
        log "WARN: R2 partly configured — need R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_S3_ENDPOINT; skipping R2"
        progress 7 skip "R2 upload" "partly configured — skipped"
    else
        progress 7 skip "R2 upload" "not configured"
    fi
fi

# --- Local retention -------------------------------------------------------------
progress 8 running "Local retention"
if [ -n "${BACKUP_MAX_ARCHIVES:-}" ]; then
    # shellcheck disable=SC2016
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'minecraft-*.tar.gz' \
        | sort -r \
        | tail -n "+$((BACKUP_MAX_ARCHIVES + 1))" \
        | while IFS= read -r f; do
            [ -z "$f" ] && continue
            rm -f "$f" && log "pruned local $f"
        done
    log "local retention: newest $BACKUP_MAX_ARCHIVES archive(s)"
    progress 8 ok "Local retention" "kept newest ${BACKUP_MAX_ARCHIVES}"
else
    find "$BACKUP_DIR" -name 'minecraft-*.tar.gz' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true
    log "pruned local backups older than $RETAIN_DAYS days"
    progress 8 ok "Local retention" "pruned > ${RETAIN_DAYS}d"
fi

log "done: archive=$ARCHIVE_NAME size=$size"
