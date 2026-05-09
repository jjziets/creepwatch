#!/usr/bin/env python3
"""
Minecraft Whitelist Guard Bot
- Notifies all admins when unknown players try to join
- Any admin can Allow/Deny via buttons
- Slash commands for server management
- Only notifies on actual Minecraft version updates (not routine restarts)
"""
import subprocess, requests, time, re, logging, os, threading, pathlib, json, datetime
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RCON_CMD  = ["docker", "exec", "minecraft", "rcon-cli"]
API       = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Comma-separated Telegram chat IDs of admins who can manage the server
# and receive notifications. Example: ADMIN_CHAT_IDS=111111,222222
ADMIN_CHAT_IDS = [int(x) for x in os.environ["ADMIN_CHAT_IDS"].split(",") if x.strip()]

# Display timezone for human-facing timestamps (e.g. /activity output).
# Falls back to UTC if the named zone is missing.
try:
    DISPLAY_TZ = ZoneInfo(os.environ.get("TZ", "UTC"))
except Exception:
    DISPLAY_TZ = ZoneInfo("UTC")

pending = {}  # msg_id -> player
offset  = 0

LOST_CONN_RE  = re.compile(r"(\w+) \([^)]+\) lost connection: You are not white-listed", re.IGNORECASE)
DISCONNECT_RE = re.compile(r"Disconnecting (\w+) \([^)]+\): You are not white-listed", re.IGNORECASE)
JOINED_RE     = re.compile(r"\]: (\w+) joined the game\s*$")
LEFT_RE       = re.compile(r"\]: (\w+) left the game\s*$")
READY_RE      = re.compile(r"Done \([0-9.]+s\)!")
STOPPING_RE   = re.compile(r"Stopping( the)? server", re.IGNORECASE)
ERROR_RE      = re.compile(r"\[\d+:\d+:\d+\] \[[^\]]+/ERROR\]:?\s*(.+?)\s*$")
ERROR_COOLDOWN = 600  # seconds — collapse repeated identical errors


# ── RCON helpers ──────────────────────────────────────────────────────────────

def rcon(cmd: str) -> str:
    try:
        r = subprocess.run(RCON_CMD + [cmd], capture_output=True, text=True, timeout=10)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"RCON error: {e}"


# ── Telegram helpers ──────────────────────────────────────────────────────────

def send(chat_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(f"{API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        log.warning(f"send error: {e}")

def broadcast(text: str):
    for cid in ADMIN_CHAT_IDS:
        send(cid, text)

def edit(chat_id: int, msg_id: int, text: str):
    try:
        requests.post(f"{API}/editMessageText", json={
            "chat_id": chat_id, "message_id": msg_id,
            "text": text, "parse_mode": "Markdown",
        }, timeout=10)
    except Exception as e:
        log.warning(f"edit error: {e}")


# ── Commands ──────────────────────────────────────────────────────────────────

BLOCKED_FILE  = pathlib.Path("/data/blocked_players.txt")
PREFS_FILE    = pathlib.Path("/data/notify_prefs.json")
DEFAULT_PREFS = {
    "joins":     True,
    "leaves":    True,
    "approvals": True,
    "rejects":   True,
    "restarts":  True,
    "errors":    True,
}

def blocked_list() -> set:
    if BLOCKED_FILE.exists():
        return {l.strip() for l in BLOCKED_FILE.read_text().splitlines() if l.strip()}
    return set()

def block_player(player: str):
    players = blocked_list()
    players.add(player)
    BLOCKED_FILE.write_text("\n".join(sorted(players)))

def unblock_player(player: str):
    players = blocked_list()
    players.discard(player)
    BLOCKED_FILE.write_text("\n".join(sorted(players)))


def load_prefs() -> dict:
    if PREFS_FILE.exists():
        try:
            return json.loads(PREFS_FILE.read_text())
        except Exception:
            return {}
    return {}

def get_prefs(chat_id: int) -> dict:
    return {**DEFAULT_PREFS, **load_prefs().get(str(chat_id), {})}

def set_pref(chat_id: int, key: str, value: bool):
    all_prefs = load_prefs()
    all_prefs.setdefault(str(chat_id), {})[key] = value
    PREFS_FILE.write_text(json.dumps(all_prefs, indent=2))

def notify_event(key: str, text: str, exclude: int = None):
    for cid in ADMIN_CHAT_IDS:
        if cid == exclude:
            continue
        if get_prefs(cid).get(key, True):
            send(cid, text)

def settings_keyboard(prefs: dict) -> dict:
    fmt = lambda on: "🔔 ON" if on else "🔕 OFF"
    return {"inline_keyboard": [
        [{"text": f"Joins:     {fmt(prefs['joins'])}",     "callback_data": "toggle:joins"}],
        [{"text": f"Leaves:    {fmt(prefs['leaves'])}",    "callback_data": "toggle:leaves"}],
        [{"text": f"Approvals: {fmt(prefs['approvals'])}", "callback_data": "toggle:approvals"}],
        [{"text": f"Rejects:   {fmt(prefs['rejects'])}",   "callback_data": "toggle:rejects"}],
        [{"text": f"Restarts:  {fmt(prefs['restarts'])}",  "callback_data": "toggle:restarts"}],
        [{"text": f"Errors:    {fmt(prefs['errors'])}",    "callback_data": "toggle:errors"}],
    ]}

TOGGLE_KEYS = ("joins", "leaves", "approvals", "rejects", "restarts", "errors")


HELP_TEXT = """🎮 *Vast Family Minecraft Bot*

*Whitelist*
/whitelist · /wl — list all whitelisted players
/approve · /a `<player>` — add player to whitelist
/remove · /rm `<player>` — remove player from whitelist

*Blocked*
/blocked · /bl — list denied players
/unblock · /ub `<player>` — unblock a denied player

*Server*
/online · /ol — who is online right now
/activity · /ac — last 20 join/leave events
/status · /st — server version and player count

*Notifications*
/settings · /se — toggle your join, leave, approval, reject, restart, error alerts

/help · /h — show this message"""


def cmd_whitelist(chat_id: int):
    out = rcon("whitelist list")
    send(chat_id, f"📋 *Whitelist*\n{out}")

def cmd_remove(chat_id: int, player: str, admin_name: str):
    if not player:
        send(chat_id, "Usage: /remove `<player>`")
        return
    out = rcon(f"whitelist remove {player}")
    log.info(f"Removed {player} by {admin_name}: {out}")
    text = f"🚫 *{player}* removed from whitelist by {admin_name}."
    send(chat_id, text)
    notify_event("rejects", text, exclude=chat_id)

def cmd_approve(chat_id: int, player: str, admin_name: str):
    if not player:
        send(chat_id, "Usage: /approve `<player>`")
        return
    out = rcon(f"whitelist add {player}")
    log.info(f"Approved {player} by {admin_name}: {out}")
    text = f"✅ *{player}* added to whitelist by {admin_name}."
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)

def cmd_online(chat_id: int):
    out = rcon("list")
    send(chat_id, f"👥 *Online players*\n{out}")

def cmd_activity(chat_id: int):
    try:
        # `-t` prepends each line with docker's RFC3339 UTC timestamp,
        # which we re-render in the display timezone — independent of
        # whatever timezone the minecraft container itself logs in.
        result = subprocess.run(
            ["docker", "logs", "-t", "--tail", "500", "minecraft"],
            capture_output=True, text=True, timeout=15
        )
        lines = (result.stdout + result.stderr).splitlines()
        events = []
        for line in lines:
            if not any(x in line for x in ("joined the game", "left the game", "lost connection")):
                continue
            m = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S*\s+.*INFO\]:\s*(.+)$', line)
            if not m:
                continue
            try:
                ts_utc   = datetime.datetime.fromisoformat(m.group(1)).replace(tzinfo=datetime.timezone.utc)
                ts_local = ts_utc.astimezone(DISPLAY_TZ)
                events.append(f"`{ts_local.strftime('%H:%M')}` {m.group(2)}")
            except Exception as e:
                log.warning(f"activity parse error: {e}")
        if events:
            tz_label = DISPLAY_TZ.key if hasattr(DISPLAY_TZ, "key") else str(DISPLAY_TZ)
            send(chat_id, f"📜 *Recent activity* ({tz_label})\n" + "\n".join(events[-20:]))
        else:
            send(chat_id, "📜 No recent activity found.")
    except Exception as e:
        send(chat_id, f"Error reading logs: {e}")

def cmd_status(chat_id: int):
    ver  = rcon("version")
    lst  = rcon("list")
    send(chat_id, f"🖥️ *Server status*\n{ver}\n\n{lst}")

def cmd_settings(chat_id: int):
    send(chat_id,
         "🔔 *Your notification settings*\nTap to toggle. Only your own notifications change.",
         keyboard=settings_keyboard(get_prefs(chat_id)))

def cmd_blocked(chat_id: int):
    players = blocked_list()
    if players:
        send(chat_id, "🚫 *Blocked players*\n" + "\n".join(f"• {p}" for p in sorted(players)))
    else:
        send(chat_id, "🚫 *Blocked players*\nNone yet.")

def cmd_unblock(chat_id: int, player: str, admin_name: str):
    if not player:
        send(chat_id, "Usage: /unblock `<player>`")
        return
    unblock_player(player)
    text = f"✅ *{player}* unblocked by {admin_name}. They can request access again."
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)

def handle_command(chat_id: int, text: str, sender_name: str):
    parts = text.strip().split(None, 1)
    cmd   = parts[0].lower().split("@")[0]
    arg   = parts[1].strip() if len(parts) > 1 else ""
    log.info(f"Command {cmd} from {sender_name} ({chat_id})")

    if   cmd in ("/help", "/h"):              send(chat_id, HELP_TEXT)
    elif cmd in ("/whitelist", "/wl"):        cmd_whitelist(chat_id)
    elif cmd in ("/approve", "/a"):           cmd_approve(chat_id, arg, sender_name)
    elif cmd in ("/remove", "/rm"):           cmd_remove(chat_id, arg, sender_name)
    elif cmd in ("/blocked", "/bl"):          cmd_blocked(chat_id)
    elif cmd in ("/unblock", "/ub"):          cmd_unblock(chat_id, arg, sender_name)
    elif cmd in ("/online", "/ol"):           cmd_online(chat_id)
    elif cmd in ("/activity", "/ac"):         cmd_activity(chat_id)
    elif cmd in ("/status", "/st"):           cmd_status(chat_id)
    elif cmd in ("/settings", "/se"):         cmd_settings(chat_id)
    else:                                     send(chat_id, "Unknown command. Try /help")


# ── Approval flow ─────────────────────────────────────────────────────────────

def send_approval_request(player: str):
    keyboard = {"inline_keyboard": [[
        {"text": f"✅ Allow {player}", "callback_data": f"allow:{player}"},
        {"text": f"❌ Deny",           "callback_data": f"deny:{player}"},
    ]]}
    for cid in ADMIN_CHAT_IDS:
        r = requests.post(f"{API}/sendMessage", json={
            "chat_id": cid,
            "text": f"🎮 *New player wants to join!*\n\nPlayer: `{player}`\n\nAllow them on the Vast Family Minecraft server?",
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        }, timeout=10)
        data = r.json()
        if data.get("ok"):
            pending[data["result"]["message_id"]] = player
            log.info(f"Approval request for {player} sent to {cid}")
        else:
            log.error(f"Telegram error to {cid}: {data}")


def poll_callbacks():
    global offset
    r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10)
    if not r.ok:
        return
    for update in r.json().get("result", []):
        offset = update["update_id"] + 1

        # Handle slash commands from admins only
        msg = update.get("message", {})
        if msg.get("text", "").startswith("/"):
            sender_id   = msg["chat"]["id"]
            sender_name = msg["from"].get("first_name", str(sender_id))
            if sender_id in ADMIN_CHAT_IDS:
                handle_command(sender_id, msg["text"], sender_name)
            else:
                send(sender_id, "⛔ You are not authorised to manage this server.")
            continue

        # Ignore all non-command messages silently
        if msg.get("text"):
            sender_id = msg["chat"]["id"]
            if sender_id not in ADMIN_CHAT_IDS:
                # Don't respond to randoms at all
                continue

        # Handle inline button callbacks
        cb = update.get("callback_query")
        if not cb:
            continue

        sender_id = cb["from"]["id"]
        if sender_id not in ADMIN_CHAT_IDS:
            requests.post(f"{API}/answerCallbackQuery", json={
                "callback_query_id": cb["id"],
                "text": "⛔ Not authorised.",
                "show_alert": True,
            }, timeout=10)
            continue

        data       = cb["data"]
        msg_id     = cb["message"]["message_id"]
        chat_id    = cb["message"]["chat"]["id"]
        action, _, arg = data.partition(":")
        admin_name = cb["from"].get("first_name", str(sender_id))

        if action == "toggle":
            new_value = None
            if arg in TOGGLE_KEYS:
                new_value = not get_prefs(sender_id).get(arg, True)
                set_pref(sender_id, arg, new_value)
                log.info(f"{admin_name} set {arg}={new_value}")
            requests.post(f"{API}/editMessageReplyMarkup", json={
                "chat_id": chat_id, "message_id": msg_id,
                "reply_markup": settings_keyboard(get_prefs(sender_id)),
            }, timeout=10)
            requests.post(f"{API}/answerCallbackQuery", json={
                "callback_query_id": cb["id"],
                "text": f"{arg} {'on' if new_value else 'off'}" if new_value is not None else "",
            }, timeout=10)
            continue

        player = arg
        if action == "allow":
            out = rcon(f"whitelist add {player}")
            log.info(f"whitelist add {player} by {admin_name}: {out}")
            result_text = f"✅ *{player}* was allowed by {admin_name}."
            pref_key = "approvals"
        elif action == "deny":
            block_player(player)
            result_text = f"❌ *{player}* was denied by {admin_name} and added to the blocked list."
            log.info(f"Denied and blocked {player} by {admin_name}")
            pref_key = "rejects"
        else:
            requests.post(f"{API}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}, timeout=10)
            continue

        edit(chat_id, msg_id, result_text)
        notify_event(pref_key, result_text, exclude=sender_id)

        requests.post(f"{API}/answerCallbackQuery",
                      json={"callback_query_id": cb["id"]}, timeout=10)
        pending.pop(msg_id, None)


# ── Log tailing ───────────────────────────────────────────────────────────────

def tail_logs():
    proc = subprocess.Popen(
        ["docker", "logs", "-f", "--tail", "0", "minecraft"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    for line in proc.stdout:
        yield line.rstrip()

def extract_player(line: str):
    for pat in (LOST_CONN_RE, DISCONNECT_RE):
        m = pat.search(line)
        if m:
            return m.group(1).strip()
    return None


# ── Startup version check ─────────────────────────────────────────────────────

def check_version_and_notify():
    """Only broadcast if the Minecraft version has actually changed."""
    # Wait for MC server to be ready (up to 60s)
    ver_out = ""
    for _ in range(30):
        ver_out = rcon("version")
        if ver_out and "version" in ver_out.lower() and "error" not in ver_out.lower():
            break
        time.sleep(2)

    m = re.search(r"(\d+\.\d+[\.\d]*)", ver_out)
    current_version = m.group(1) if m else ver_out.strip()

    version_file = pathlib.Path("/data/mc_last_version")
    last_version  = version_file.read_text().strip() if version_file.exists() else None

    if current_version != last_version:
        version_file.write_text(current_version)
        if last_version and last_version != "":
            broadcast(f"🆙 *Minecraft updated!*\n\n{last_version} → *{current_version}*\n\nSend /help for commands.")
        else:
            # Very first start ever
            broadcast(f"🛡️ Whitelist Guard *online* — Minecraft *{current_version}*\nSend /help for commands.")
        log.info(f"Version changed: {last_version} -> {current_version}")
    else:
        log.info(f"Version unchanged ({current_version}), no notification sent.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Minecraft Whitelist Guard started")

    # Check version in background so log tailing starts immediately
    threading.Thread(target=check_version_and_notify, daemon=False).start()

    already_notified = set()
    recent_errors    = {}  # signature -> last_seen_ts

    def callback_loop():
        while True:
            try:
                poll_callbacks()
            except Exception as e:
                log.warning(f"Callback poll error: {e}")
            time.sleep(2)

    threading.Thread(target=callback_loop, daemon=True).start()

    for line in tail_logs():
        if READY_RE.search(line):
            notify_event("restarts", "🟢 Minecraft server is ready.")
            continue

        if STOPPING_RE.search(line):
            notify_event("restarts", "🛑 Minecraft server is stopping.")
            continue

        m = ERROR_RE.search(line)
        if m:
            err = m.group(1).strip()
            sig = err[:80]
            now = time.time()
            if now - recent_errors.get(sig, 0) > ERROR_COOLDOWN:
                recent_errors[sig] = now
                safe = err.replace("`", "'")[:500]
                notify_event("errors", f"⚠️ *Server error*\n`{safe}`")
            continue

        m = JOINED_RE.search(line)
        if m:
            notify_event("joins", f"🟢 *{m.group(1)}* joined the game.")
            continue

        m = LEFT_RE.search(line)
        if m:
            notify_event("leaves", f"⚪ *{m.group(1)}* left the game.")
            continue

        player = extract_player(line)
        if player and player not in already_notified:
            if player in blocked_list():
                log.info(f"Blocked player {player} tried to join — ignoring silently.")
            else:
                log.info(f"Whitelist rejection: {player}")
                already_notified.add(player)
                def expire(p):
                    time.sleep(300)
                    already_notified.discard(p)
                threading.Thread(target=expire, args=(player,), daemon=True).start()
                send_approval_request(player)

if __name__ == "__main__":
    main()
