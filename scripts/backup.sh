#!/bin/sh
# Daily Minecraft world backup.
#
# Quiesces the world over RCON (save-all flush + save-off), tars the
# minecraft_data volume from an ephemeral alpine container, then re-enables
# autosave. Players are not kicked. The save-on step is unconditional so we
# never leave the world frozen, even if the snapshot fails.
#
# Failures (and only failures) are reported to Telegram if TELEGRAM_BOT_TOKEN
# and ADMIN_CHAT_IDS are present in /home/vast/minecraft/.env.

set -u

BACKUP_DIR="${BACKUP_DIR:-/home/vast/minecraft/backups}"
RETAIN_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE_NAME="minecraft-${TIMESTAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

env_file=/home/vast/minecraft/.env
if [ -f "$env_file" ]; then
    # Compose-style .env values may contain spaces, so don't `source` them.
    # Pluck the two keys we need with a literal grep.
    TELEGRAM_BOT_TOKEN=$(grep -m1 '^TELEGRAM_BOT_TOKEN=' "$env_file" | cut -d= -f2-)
    ADMIN_CHAT_IDS=$(grep -m1 '^ADMIN_CHAT_IDS=' "$env_file" | cut -d= -f2-)
    export TELEGRAM_BOT_TOKEN ADMIN_CHAT_IDS
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
if ! docker exec minecraft rcon-cli save-all flush >/dev/null 2>&1; then
    log "FAIL: save-all flush"
    notify_failure "🚨 Minecraft backup failed at save-all flush. Backup not created."
    exit 1
fi

log "save-off"
if ! docker exec minecraft rcon-cli save-off >/dev/null 2>&1; then
    log "FAIL: save-off"
    notify_failure "🚨 Minecraft backup failed at save-off. Backup not created."
    exit 1
fi

log "snapshotting volume to $ARCHIVE_NAME"
backup_ok=1
if ! docker run --rm \
        -v minecraft_data:/data:ro \
        -v "$BACKUP_DIR":/backups \
        alpine:latest \
        tar czf "/backups/$ARCHIVE_NAME" -C /data . 2>/dev/null; then
    backup_ok=0
    log "FAIL: tar"
fi

log "save-on"
docker exec minecraft rcon-cli save-on >/dev/null 2>&1 || log "WARN: save-on returned non-zero"

if [ "$backup_ok" -ne 1 ]; then
    notify_failure "🚨 Minecraft backup tar failed. Server autosave re-enabled. Investigate /var/log/minecraft-backup.log or journalctl -u minecraft-backup."
    exit 1
fi

size=$(du -h "$BACKUP_DIR/$ARCHIVE_NAME" 2>/dev/null | cut -f1)
log "backup complete ($size)"

find "$BACKUP_DIR" -name 'minecraft-*.tar.gz' -mtime "+$RETAIN_DAYS" -delete 2>/dev/null || true
log "pruned backups older than $RETAIN_DAYS days"
