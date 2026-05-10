#!/bin/sh
# Daily Minecraft world backup.
#
# Quiesces the world over RCON (save-all flush + save-off), tars the
# minecraft_data volume from an ephemeral alpine container, then re-enables
# autosave. Players are not kicked. The save-on step is unconditional so we
# never leave the world frozen, even if the snapshot fails.
#
# Failures (and only failures) are reported to Telegram when TELEGRAM_BOT_TOKEN
# and ADMIN_CHAT_IDS are set (Compose env) or plucked from CREEPWATCH_ENV_FILE
# (host cron) or the legacy default path.
#
# Optional Cloudflare R2 (S3-compatible): set R2_BUCKET, R2_ACCESS_KEY_ID,
# R2_SECRET_ACCESS_KEY, R2_S3_ENDPOINT (https://<accountid>.r2.cloudflarestorage.com).
# After a successful tarball, uploads with aws-cli in Docker, then deletes older
# objects so at most R2_RETAIN_COUNT archives remain (default 3).
# Optional BACKUP_MAX_ARCHIVES: keep only that many newest local tarballs (else
# prune by BACKUP_RETENTION_DAYS, default 7).

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
    export R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_S3_ENDPOINT R2_PREFIX R2_RETAIN_COUNT BACKUP_MAX_ARCHIVES
fi

log() { printf '[%s backup] %s\n' "$(date -u +%FT%TZ)" "$*"; }

notify_failure() {
    [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && return 0
    [ -z "${ADMIN_CHAT_IDS:-}" ] && return 0
    text="$1"
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

log "save-all flush"
if ! docker_cli exec minecraft rcon-cli save-all flush >/dev/null 2>&1; then
    log "FAIL: save-all flush"
    notify_failure "🚨 Minecraft backup failed at save-all flush. Backup not created."
    exit 1
fi

log "save-off"
if ! docker_cli exec minecraft rcon-cli save-off >/dev/null 2>&1; then
    log "FAIL: save-off"
    notify_failure "🚨 Minecraft backup failed at save-off. Backup not created."
    exit 1
fi

log "snapshotting volume to $ARCHIVE_NAME"
backup_ok=1
if ! docker_cli run --rm \
        -v minecraft_data:/data:ro \
        -v "$BACKUP_DIR":/backups \
        alpine:latest \
        tar czf "/backups/$ARCHIVE_NAME" -C /data . 2>/dev/null; then
    backup_ok=0
    log "FAIL: tar"
fi

log "save-on"
docker_cli exec minecraft rcon-cli save-on >/dev/null 2>&1 || log "WARN: save-on returned non-zero"

if [ "$backup_ok" -ne 1 ]; then
    notify_failure "🚨 Minecraft backup tar failed. Server autosave re-enabled. Investigate /var/log/minecraft-backup.log or journalctl -u minecraft-backup."
    exit 1
fi

size=$(du -h "$BACKUP_DIR/$ARCHIVE_NAME" 2>/dev/null | cut -f1)
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

if r2_fully_configured; then
    R2_PREFIX_NORM=${R2_PREFIX:-minecraft/}
    R2_KEEP=${R2_RETAIN_COUNT:-3}
    r2_key=$(r2_object_key "$R2_PREFIX_NORM" "$ARCHIVE_NAME")
    log "uploading to R2 s3://$R2_BUCKET/$r2_key"
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
