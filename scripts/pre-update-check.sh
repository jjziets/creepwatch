#!/bin/sh
# Watchtower pre-update lifecycle hook.
# Mounted into the minecraft container; a non-zero exit tells watchtower to
# skip the update cycle for this container. We skip whenever any player is
# online so auto-updates never kick someone mid-game.
#
# Reads TELEGRAM_BOT_TOKEN and ADMIN_CHAT_IDS from the container's env to
# notify on skip. Notification is best-effort — exit code is what matters.

list_output=$(timeout 10 rcon-cli list 2>/dev/null | head -n1)
count=$(echo "$list_output" | sed -nE 's/^There are ([0-9]+).*$/\1/p')
count=${count:-0}

if [ "$count" -gt 0 ]; then
    names=$(echo "$list_output" | sed -nE 's/^.*online: *(.*)$/\1/p')
    [ -z "$names" ] && names="(unknown)"
    echo "[pre-update] $count player(s) online — skipping update: $names"

    # Notify at most once every 12 hours so the nightly retry window
    # (e.g. hourly 01:00–08:00) only produces a single skip ping.
    notify_marker=/data/.creepwatch_last_skip_notify
    last_ts=$(cat "$notify_marker" 2>/dev/null || echo 0)
    now=$(date +%s)
    elapsed=$((now - last_ts))

    if [ "$elapsed" -gt 43200 ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ADMIN_CHAT_IDS:-}" ] && command -v curl >/dev/null 2>&1; then
        msg=$(printf "🟡 Minecraft auto-update postponed: %s player(s) online (%s). Will retry within the nightly update window." "$count" "$names")
        IFS=','
        for chat in $ADMIN_CHAT_IDS; do
            curl -s -m 5 -X POST \
                "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
                --data-urlencode "chat_id=$chat" \
                --data-urlencode "text=$msg" >/dev/null 2>&1 || true
        done
        echo "$now" > "$notify_marker" 2>/dev/null || true
    fi
    exit 1
fi

echo "[pre-update] no players online — proceeding with update"
exit 0
