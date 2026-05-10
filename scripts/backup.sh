#!/bin/sh
# Daily Minecraft world backup.
#
# Quiesces the world over RCON (save-all flush + save-off), tars the
# minecraft_data volume from an ephemeral alpine container, then re-enables
# autosave. Players are not kicked. The save-on step is unconditional so we
# never leave the world frozen, even if the snapshot fails.
#
# Failures are reported to Telegram when TELEGRAM_BOT_TOKEN and ADMIN_CHAT_IDS
# are set (Compose env) or plucked from CREEPWATCH_ENV_FILE (host cron) or the
# legacy default path. Every failure is also written to stderr for cron/journald.
#
# Preflight (before save-off): minecraft container running, world level.dat on
# the volume, and enough free disk under BACKUP_DIR. Skips backup if unhealthy.
# Post-tar: by default `gzip -l` (header read only — safe for huge worlds). Set
# BACKUP_STRICT_GZIP_TEST=1 to use `gzip -t` (reads entire archive; can OOM/timeout).
#
# Optional Cloudflare R2 (S3-compatible): set R2_BUCKET, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY, R2_S3_ENDPOINT (https://<accountid>.r2.cloudflarestorage.com).
# After a successful tarball, uploads with aws-cli in Docker, then deletes older
# objects so at most R2_RETAIN_COUNT archives remain (default 3).
# Optional BACKUP_MAX_ARCHIVES: keep only that many newest local tarballs (else
# prune by BACKUP_RETENTION_DAYS, default 7).
#
# Optional BACKUP_PROGRESS_CHAT_ID: Telegram chat id (digits only) to receive
# short stage updates; set by mc-guard when an admin runs /backup (not from .env pluck).

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
    export R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_S3_ENDPOINT R2_PREFIX R2_RETAIN_COUNT BACKUP_MAX_ARCHIVES
    export BACKUP_SKIP_PREFLIGHT BACKUP_MIN_FREE_MB BACKUP_MIN_ARCHIVE_BYTES BACKUP_STRICT_GZIP_TEST
fi

log() { printf '[%s backup] %s\n' "$(date -u +%FT%TZ)" "$*"; }

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

# Single-chat progress (mc-guard sets BACKUP_PROGRESS_CHAT_ID for /backup).
progress_ping() {
    text=$1
    [ -z "${BACKUP_PROGRESS_CHAT_ID:-}" ] && return 0
    [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && return 0
    case "$BACKUP_PROGRESS_CHAT_ID" in *[!0-9]*) return 0 ;; esac
    log "progress telegram: $text"
    curl -s -m 8 -X POST \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=$BACKUP_PROGRESS_CHAT_ID" \
        --data-urlencode "text=$text" >/dev/null 2>&1 || true
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

if ! preflight_backup; then
    notify_failure "🚨 Minecraft backup skipped — preflight failed (container not running, missing level.dat on volume, or low disk). No snapshot taken."
    exit 1
fi

progress_ping "📋 Preflight OK — flushing world (save-all)…"

log "save-all flush"
if ! docker_cli exec minecraft rcon-cli save-all flush >/dev/null 2>&1; then
    log "FAIL: save-all flush"
    notify_failure "🚨 Minecraft backup failed at save-all flush (RCON). Backup not created — check server logs."
    exit 1
fi

progress_ping "💾 save-all done — pausing autosave (save-off)…"

log "save-off"
if ! docker_cli exec minecraft rcon-cli save-off >/dev/null 2>&1; then
    log "FAIL: save-off"
    notify_failure "🚨 Minecraft backup failed at save-off (RCON). Backup not created."
    docker_cli exec minecraft rcon-cli save-on >/dev/null 2>&1 || true
    exit 1
fi

progress_ping "📸 Snapshotting volume → $ARCHIVE_NAME (may take several minutes)…"

log "snapshotting volume to $ARCHIVE_NAME"
backup_ok=1
tar_err=$(mktemp)
if ! docker_cli run --rm \
        -v minecraft_data:/data:ro \
        -v "$BACKUP_DIR":/backups \
        alpine:latest \
        tar czf "/backups/$ARCHIVE_NAME" -C /data . 2>"$tar_err"; then
    backup_ok=0
    log "FAIL: tar"
    tar_tail=$(tail -c 400 "$tar_err" 2>/dev/null | tr '\n' ' ' || true)
    log "tar stderr tail: $tar_tail"
fi
rm -f "$tar_err"

log "save-on"
saveon_ok=1
if ! docker_cli exec minecraft rcon-cli save-on >/dev/null 2>&1; then
    saveon_ok=0
    log "FAIL: save-on after snapshot"
fi

if [ "$backup_ok" -ne 1 ]; then
    notify_failure "🚨 Minecraft backup tar failed. Check disk, volume, and docker logs. save-on was attempted."
    [ "$saveon_ok" -eq 1 ] || notify_failure "🚨 Minecraft backup: tar failed and save-on also failed — use console/RCON save-on if needed."
    exit 1
fi

if [ "$saveon_ok" -ne 1 ]; then
    notify_failure "🚨 Minecraft backup: tarball OK on disk but save-on failed — autosave may still be off; fix via RCON save-on or restart."
    exit 1
fi

archive_path="$BACKUP_DIR/$ARCHIVE_NAME"
archive_bytes=$(wc -c < "$archive_path" 2>/dev/null | tr -d ' ' || echo 0)
if ! [ "$archive_bytes" -ge "$BACKUP_MIN_ARCHIVE_BYTES" ] 2>/dev/null; then
    rm -f "$archive_path"
    notify_failure "🚨 Minecraft backup rejected: archive too small (${archive_bytes} bytes, min ${BACKUP_MIN_ARCHIVE_BYTES}) — possible corruption or empty world; file removed."
    exit 1
fi

if [ -n "${BACKUP_STRICT_GZIP_TEST:-}" ]; then
    log "postflight: gzip -t full test on $ARCHIVE_NAME (BACKUP_STRICT_GZIP_TEST)"
    if ! docker_cli run --rm -v "$BACKUP_DIR":/backups:ro alpine:latest gzip -t "/backups/$ARCHIVE_NAME" 2>/dev/null; then
        rm -f "$archive_path"
        notify_failure "🚨 Minecraft backup rejected: gzip -t failed (truncated or corrupt tarball); file removed."
        exit 1
    fi
else
    log "postflight: gzip header check on $ARCHIVE_NAME (set BACKUP_STRICT_GZIP_TEST=1 for full -t)"
    if ! docker_cli run --rm -v "$BACKUP_DIR":/backups:ro alpine:latest gzip -l "/backups/$ARCHIVE_NAME" >/dev/null 2>&1; then
        rm -f "$archive_path"
        notify_failure "🚨 Minecraft backup rejected: gzip header unreadable (truncated or not gzip); file removed."
        exit 1
    fi
fi

size=$(du -h "$archive_path" 2>/dev/null | cut -f1)
log "backup complete ($size)"

progress_ping "✅ Local backup OK — $ARCHIVE_NAME ($size), gzip verified."

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

if r2_fully_configured; then
    R2_PREFIX_NORM=${R2_PREFIX:-minecraft/}
    R2_KEEP=${R2_RETAIN_COUNT:-3}
    r2_key=$(r2_object_key "$R2_PREFIX_NORM" "$ARCHIVE_NAME")
    log "uploading to R2 s3://$R2_BUCKET/$r2_key"
    progress_ping "☁️ Uploading to R2…"
    if ! docker_cli run --rm \
        -e "AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID" \
        -e "AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY" \
        -e "AWS_DEFAULT_REGION=auto" \
        -v "$BACKUP_DIR":/backups:ro \
        amazon/aws-cli:latest \
        s3 cp "/backups/$ARCHIVE_NAME" "s3://${R2_BUCKET}/${r2_key}" \
        --endpoint-url "$R2_S3_ENDPOINT" \
        --region auto >/dev/null 2>&1; then
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
    progress_ping "☁️ R2 upload and retention finished."
else
    if [ -n "${R2_BUCKET:-}" ] || [ -n "${R2_ACCESS_KEY_ID:-}" ] || [ -n "${R2_SECRET_ACCESS_KEY:-}" ] || [ -n "${R2_S3_ENDPOINT:-}" ]; then
        log "WARN: R2 partly configured — need R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_S3_ENDPOINT; skipping R2"
    fi
fi

# --- Local retention -------------------------------------------------------------
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
else
    find "$BACKUP_DIR" -name 'minecraft-*.tar.gz' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true
    log "pruned local backups older than $RETAIN_DAYS days"
fi

progress_ping "🏁 Backup pipeline finished — $ARCHIVE_NAME ($size)."
