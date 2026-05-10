#!/bin/sh
# Watchtower pre-update lifecycle hook.
# Mounted into the minecraft container; a non-zero exit tells watchtower to
# skip the update cycle for this container. We skip whenever any player is
# online so auto-updates never kick someone mid-game.
#
# Reads TELEGRAM_BOT_TOKEN and ADMIN_CHAT_IDS from the container's env to
# notify on skip. Notification is best-effort — exit code is what matters.
#
# Tracks consecutive calendar days with at least one skip in
# /data/.creepwatch_skip_streak (line1=YYYYMMDD of last increment, line2=streak)
# so long-running postponements escalate (issue #3).

list_output=$(timeout 10 rcon-cli list 2>/dev/null | head -n1)
count=$(echo "$list_output" | sed -nE 's/^There are ([0-9]+).*$/\1/p')
count=${count:-0}

if [ "$count" -gt 0 ]; then
    names=$(echo "$list_output" | sed -nE 's/^.*online: *(.*)$/\1/p')
    [ -z "$names" ] && names="(unknown)"
    echo "[pre-update] $count player(s) online — skipping update: $names"

    streak_file=/data/.creepwatch_skip_streak
    today=$(date +%Y%m%d)
    last_day=0
    streak=0
    if [ -f "$streak_file" ]; then
        last_day=$(sed -n '1p' "$streak_file" | tr -d '\r')
        streak=$(sed -n '2p' "$streak_file" | tr -d '\r')
    fi
    last_day=${last_day:-0}
    streak=${streak:-0}
    case "$streak" in ''|*[!0-9]*) streak=0 ;; esac
    case "$last_day" in ''|*[!0-9]*) last_day=0 ;; esac

    if [ "$last_day" != "$today" ]; then
        streak=$((streak + 1))
        tmp="$streak_file.tmp.$$"
        if printf '%s\n%s\n' "$today" "$streak" >"$tmp" 2>/dev/null; then
            mv "$tmp" "$streak_file" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
        fi
    fi

    # Notify at most once every 12 hours so the nightly retry window
    # (e.g. hourly 01:00–08:00) only produces a single skip ping.
    notify_marker=/data/.creepwatch_last_skip_notify
    last_ts=$(cat "$notify_marker" 2>/dev/null || echo 0)
    now=$(date +%s)
    elapsed=$((now - last_ts))

    if [ "$elapsed" -gt 43200 ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${ADMIN_CHAT_IDS:-}" ] && command -v curl >/dev/null 2>&1; then
        if [ "$streak" -ge 7 ]; then
            msg=$(printf '🚨 Update postponed %s+ days — manual /update may be needed. %s player(s) online (%s).' "$streak" "$count" "$names")
        elif [ "$streak" -ge 3 ]; then
            msg=$(printf '⚠️ Update postponed %s days running — coordinate with players. %s player(s) online (%s).' "$streak" "$count" "$names")
        else
            msg=$(printf '🟡 Minecraft auto-update postponed: %s player(s) online (%s). Will retry within the nightly update window.' "$count" "$names")
        fi
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
