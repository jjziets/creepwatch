#!/usr/bin/env python3
"""
Minecraft Whitelist Guard Bot
- Notifies all admins when unknown players try to join
- Any admin can Allow/Deny via buttons
- Slash commands for server management
- Only notifies on actual Minecraft version updates (not routine restarts)
"""
import subprocess, requests, time, re, logging, os, threading, pathlib, json, datetime
from dataclasses import dataclass
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RCON_CMD  = ["docker", "exec", "minecraft", "rcon-cli"]
API       = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Comma-separated Telegram *user* ids (same number as a private DM chat id with
# this bot from @userinfobot). Only these users may issue commands, use buttons,
# or bridge chat. Groups/channels are ignored even if the bot were added there.
def _parse_admin_ids(raw: str) -> list[int]:
    ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    if not ids:
        raise SystemExit(
            "ADMIN_CHAT_IDS must list at least one Telegram user id (private chat with the bot)."
        )
    for i in ids:
        if i <= 0:
            raise SystemExit(
                "ADMIN_CHAT_IDS must be positive user ids from @userinfobot (private chat). "
                "Do not use group or channel chat ids (they are negative or zero)."
            )
    return ids


ADMIN_CHAT_IDS = _parse_admin_ids(os.environ["ADMIN_CHAT_IDS"])
ADMIN_IDS = frozenset(ADMIN_CHAT_IDS)


def telegram_allows_admin_interaction(*, chat_type: str | None, chat_id: int | None, from_id: int | None) -> bool:
    """True only for a private DM with the bot where the sender is a listed admin."""
    if chat_type != "private" or chat_id is None or from_id is None:
        return False
    if from_id not in ADMIN_IDS:
        return False
    # In a user↔bot private chat, chat id always equals the human user's id.
    if int(chat_id) != int(from_id):
        return False
    return True

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
CHAT_RE       = re.compile(r"\]: <([^>]+)> (.+?)\s*$")
READY_RE      = re.compile(r"Done \([0-9.]+s\)!")
STOPPING_RE   = re.compile(r"Stopping( the)? server", re.IGNORECASE)
ERROR_RE      = re.compile(r"\[\d+:\d+:\d+\] \[[^\]]+/ERROR\]:?\s*(.+?)\s*$")
ERROR_COOLDOWN = 600  # seconds — collapse repeated identical errors


@dataclass(frozen=True)
class ErrorEvent:
    kind: str
    signature: str
    message: str
    alert: bool = True


def _feature_name(err: str) -> str:
    m = re.search(r"currently generating: ResourceKey\[[^/]+/ ([^\]]+)\]", err)
    return m.group(1) if m else "unknown_feature"


def classify_error(err: str) -> ErrorEvent:
    """Classify Minecraft ERROR lines before deciding whether Telegram should page admins."""
    lower = err.lower()

    if "detected setblock in a far chunk" in lower:
        feature = _feature_name(err)
        return ErrorEvent(
            kind="worldgen_far_chunk",
            signature=f"worldgen_far_chunk:{feature}",
            message=f"Worldgen far-chunk warning ({feature})",
            alert=False,
        )

    if "error sending packet clientbound/minecraft:disconnect" in lower:
        return ErrorEvent(
            kind="disconnect_packet",
            signature="disconnect_packet",
            message="Error sending packet clientbound/minecraft:disconnect",
            alert=False,
        )

    normalized = re.sub(r"BlockPos\{[^}]+\}", "BlockPos{...}", err)
    normalized = re.sub(r"\[-?\d+,\s*-?\d+\]", "[chunk]", normalized)
    normalized = re.sub(r"-?\d+", "#", normalized)
    return ErrorEvent(
        kind="error",
        signature=normalized[:120],
        message=err,
        alert=True,
    )


# ── RCON helpers ──────────────────────────────────────────────────────────────

def rcon(cmd: str) -> str:
    try:
        r = subprocess.run(RCON_CMD + [cmd], capture_output=True, text=True, timeout=10)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"RCON error: {e}"


# ── Chat bridge helpers ───────────────────────────────────────────────────────

def md_escape(text: str) -> str:
    """Escape Telegram legacy Markdown metacharacters in user-controlled text."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


markdown_escape = md_escape  # backwards-compatible name


def single_line(text: str, limit: int = 300) -> str:
    compact = " ".join(str(text).split())
    return compact[:limit]


def extract_chat(line: str):
    m = CHAT_RE.search(line)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


def format_player_chat_for_telegram(player: str, message: str) -> str:
    return f"💬 *{md_escape(player)}*: {md_escape(single_line(message, 800))}"


def build_admin_tellraw_command(admin_name: str, message: str) -> str:
    payload = [
        {"text": "[Admin] ", "color": "gold", "bold": True},
        {"text": f"{single_line(admin_name, 40)}: ", "color": "yellow"},
        {"text": single_line(message, 800), "color": "white"},
    ]
    return "tellraw @a " + json.dumps(payload, ensure_ascii=False)


def send_admin_chat_to_minecraft(chat_id: int, message: str, admin_name: str):
    text = single_line(message, 800)
    if not text:
        return
    out = rcon(build_admin_tellraw_command(admin_name, text))
    if out and "RCON error" in out:
        send(chat_id, f"⚠️ Could not send message to Minecraft: `{md_escape(out)}`")
    else:
        log.info(f"Forwarded Telegram admin chat from {admin_name}: {text}")


# ── RCON command validation (whitelist console args before rcon-cli) ─────────

MC_PROFILE_NAME = re.compile(r"^[a-zA-Z0-9_]{1,16}$")
BANIP_TARGET = re.compile(r"^[a-zA-Z0-9_.:*\-]{1,64}$")
GAMERULE_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
TIME_QUERY = frozenset({"daytime", "gametime", "day"})
TIME_SET_WORD = frozenset({"day", "night", "noon", "midnight"})
DIFFICULTY = frozenset({"peaceful", "easy", "normal", "hard"})
WEATHER_KIND = frozenset({"clear", "rain", "thunder"})


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _log_telegram_response(op: str, r, exc=None):
    if exc is not None:
        log.warning("%s request failed: %s", op, exc)
        return
    if r is None:
        return
    try:
        data = r.json()
    except Exception as e:
        log.warning("%s bad JSON (HTTP %s): %s", op, r.status_code, e)
        return
    if not r.ok or not data.get("ok"):
        log.warning("%s Telegram API error HTTP=%s body=%s", op, r.status_code, data)


def send(chat_id: int, text: str, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
        _log_telegram_response("sendMessage", r)
    except Exception as e:
        _log_telegram_response("sendMessage", None, e)


def broadcast(text: str):
    for cid in ADMIN_CHAT_IDS:
        send(cid, text)


def edit(chat_id: int, msg_id: int, text: str):
    try:
        r = requests.post(f"{API}/editMessageText", json={
            "chat_id": chat_id, "message_id": msg_id,
            "text": text, "parse_mode": "Markdown",
        }, timeout=10)
        _log_telegram_response("editMessageText", r)
    except Exception as e:
        _log_telegram_response("editMessageText", None, e)


# ── Commands ──────────────────────────────────────────────────────────────────

BLOCKED_FILE  = pathlib.Path("/data/blocked_players.txt")
PREFS_FILE    = pathlib.Path("/data/notify_prefs.json")
HEARTBEAT_FILE = pathlib.Path("/data/.creepwatch_heartbeat")
HEARTBEAT_INTERVAL_SEC = int(os.environ.get("CREEPWATCH_HEARTBEAT_SEC", "600"))
HEALTHCHECK_URL = os.environ.get("CREEPWATCH_HEALTHCHECK_URL", "").strip()
DEFAULT_PREFS = {
    "joins":     True,
    "leaves":    True,
    "approvals": True,
    "rejects":   True,
    "restarts":  True,
    "errors":    True,
    "chats":     True,
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
        [{"text": f"Chats:     {fmt(prefs['chats'])}",     "callback_data": "toggle:chats"}],
    ]}

TOGGLE_KEYS = ("joins", "leaves", "approvals", "rejects", "restarts", "errors", "chats")


HELP_TEXT = """🎮 *Vast Family Minecraft Bot*

*Whitelist*
/whitelist · /wl — list all whitelisted players
/approve · /a `<player>` — add player to whitelist
/remove · /rm `<player>` — remove player from whitelist
/wlreload · /wlr — `whitelist reload` after editing `whitelist.json` on disk

*Blocked*
/blocked · /bl — list denied players
/unblock · /ub `<player>` — unblock a denied player

*Server*
/online · /ol — who is online right now
/activity · /ac — last 20 join/leave events
/status · /st — server version and player count
/kick · /k `<player>` [reason] — disconnect a player
/msg · /tell `<player>` `<message>` — server message to one player (not global chat bridge)
/ban · /bn `<player>` [reason] — ban name
/banip · /bi `<target>` [reason] — ban IP or pattern (validated characters only)
/pardon · /pd `<player>` — unban name
/pardonip · /pdi `<target>` — unban IP pattern
/time — query daytime, gametime, day — or set day, night, noon, midnight, or ticks
/weather — clear, rain, thunder (optional duration in seconds)
/difficulty · /diff — peaceful, easy, normal, hard
/gamerule · /gr — query or set; value must be true, false, or digits only
/update · /up — pull latest MC image and recreate container; use /update force to override online check (needs env CREEPWATCH_PROJECT_DIR + compose mount)

*Notifications*
/settings · /se — toggle your join, leave, approval, reject, restart, error, chat alerts

*Chat bridge*
Send any non-command message here and it appears in Minecraft as `[Admin]`.

/help · /h — show this message"""


def cmd_whitelist(chat_id: int):
    out = rcon("whitelist list")
    send(chat_id, f"📋 *Whitelist*\n{md_escape(out)}")

def cmd_remove(chat_id: int, player: str, admin_name: str):
    if not player:
        send(chat_id, "Usage: /remove `<player>`")
        return
    out = rcon(f"whitelist remove {player}")
    log.info(f"Removed {player} by {admin_name}: {out}")
    pe, ae = md_escape(player), md_escape(admin_name)
    text = f"🚫 *{pe}* removed from whitelist by {ae}."
    send(chat_id, text)
    notify_event("rejects", text, exclude=chat_id)

def cmd_approve(chat_id: int, player: str, admin_name: str):
    if not player:
        send(chat_id, "Usage: /approve `<player>`")
        return
    out = rcon(f"whitelist add {player}")
    log.info(f"Approved {player} by {admin_name}: {out}")
    pe, ae = md_escape(player), md_escape(admin_name)
    text = f"✅ *{pe}* added to whitelist by {ae}."
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)

def cmd_online(chat_id: int):
    out = rcon("list")
    send(chat_id, f"👥 *Online players*\n{md_escape(out)}")

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
                events.append(f"`{ts_local.strftime('%H:%M')}` {md_escape(m.group(2))}")
            except Exception as e:
                log.warning(f"activity parse error: {e}")
        if events:
            tz_label = DISPLAY_TZ.key if hasattr(DISPLAY_TZ, "key") else str(DISPLAY_TZ)
            body = "\n".join(events[-20:])
            send(chat_id, f"📜 *Recent activity* ({md_escape(tz_label)})\n" + body)
        else:
            send(chat_id, "📜 No recent activity found.")
    except Exception as e:
        send(chat_id, f"Error reading logs: {md_escape(str(e))}")

def cmd_status(chat_id: int):
    ver  = rcon("version")
    lst  = rcon("list")
    send(chat_id, f"🖥️ *Server status*\n{md_escape(ver)}\n\n{md_escape(lst)}")

def cmd_settings(chat_id: int):
    send(chat_id,
         "🔔 *Your notification settings*\nTap to toggle. Only your own notifications change.",
         keyboard=settings_keyboard(get_prefs(chat_id)))

def cmd_blocked(chat_id: int):
    players = blocked_list()
    if players:
        lines = "\n".join(f"• {md_escape(p)}" for p in sorted(players))
        send(chat_id, "🚫 *Blocked players*\n" + lines)
    else:
        send(chat_id, "🚫 *Blocked players*\nNone yet.")

def cmd_unblock(chat_id: int, player: str, admin_name: str):
    if not player:
        send(chat_id, "Usage: /unblock `<player>`")
        return
    unblock_player(player)
    pe, ae = md_escape(player), md_escape(admin_name)
    text = f"✅ *{pe}* unblocked by {ae}. They can request access again."
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_kick(chat_id: int, arg: str, admin_name: str):
    toks = arg.strip().split(None, 1)
    if not toks or not toks[0]:
        send(chat_id, "Usage: /kick `<player>` [reason]")
        return
    player = toks[0]
    reason = toks[1].strip() if len(toks) > 1 else ""
    if reason:
        out = rcon(f"kick {player} {reason}")
    else:
        out = rcon(f"kick {player}")
    log.info("Kick %s by %s: %s", player, admin_name, out)
    pe, ae = md_escape(player), md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    if reason:
        rs = md_escape(single_line(reason, 200))
        text = f"🥾 *{pe}* kicked by {ae} — {rs}\n{tail}"
    else:
        text = f"🥾 *{pe}* kicked by {ae}.\n{tail}"
    send(chat_id, text)
    notify_event("rejects", text, exclude=chat_id)


def cmd_msg(chat_id: int, arg: str, admin_name: str):
    toks = arg.strip().split(None, 1)
    if len(toks) < 2 or not toks[1].strip():
        send(chat_id, "Usage: /msg `<player>` `<message>` (same for `/tell`)")
        return
    player, body = toks[0], single_line(toks[1], 500)
    if not MC_PROFILE_NAME.match(player):
        send(chat_id, "Invalid player name (1–16 letters, digits, underscore).")
        return
    out = rcon(f"msg {player} {body}")
    log.info("msg to %s by %s", player, admin_name)
    pe, ae = md_escape(player), md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"📩 *{pe}* ← server message from {ae}\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_whitelist_reload(chat_id: int, arg: str, admin_name: str):
    if arg.strip():
        send(chat_id, "Usage: /wlreload (no arguments)")
        return
    out = rcon("whitelist reload")
    log.info("whitelist reload by %s: %s", admin_name, out)
    ae = md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"🔄 *whitelist reload* by {ae}\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_ban(chat_id: int, arg: str, admin_name: str):
    toks = arg.strip().split(None, 1)
    if not toks or not toks[0]:
        send(chat_id, "Usage: /ban `<player>` [reason]")
        return
    player = toks[0]
    if not MC_PROFILE_NAME.match(player):
        send(chat_id, "Invalid player name (1–16 letters, digits, underscore).")
        return
    reason = single_line(toks[1], 200) if len(toks) > 1 else ""
    out = rcon(f"ban {player} {reason}".strip()) if reason else rcon(f"ban {player}")
    log.info("ban %s by %s", player, admin_name)
    pe, ae = md_escape(player), md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"⛔ *{pe}* banned by {ae}.\n{tail}"
    send(chat_id, text)
    notify_event("rejects", text, exclude=chat_id)


def cmd_banip(chat_id: int, arg: str, admin_name: str):
    toks = arg.strip().split(None, 1)
    if not toks or not toks[0]:
        send(chat_id, "Usage: /banip `<ip_or_pattern>` [reason]")
        return
    target = toks[0]
    if not BANIP_TARGET.match(target):
        send(chat_id, "Invalid ban-ip target (letters, digits, `._:*-` only, max 64).")
        return
    reason = single_line(toks[1], 200) if len(toks) > 1 else ""
    out = rcon(f"ban-ip {target} {reason}".strip()) if reason else rcon(f"ban-ip {target}")
    log.info("ban-ip %s by %s", target, admin_name)
    tt, ae = md_escape(target), md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"⛔ `ban-ip` *{tt}* by {ae}.\n{tail}"
    send(chat_id, text)
    notify_event("rejects", text, exclude=chat_id)


def cmd_pardon(chat_id: int, arg: str, admin_name: str):
    tok = arg.strip().split(None, 1)
    player = tok[0] if tok else ""
    if not player:
        send(chat_id, "Usage: /pardon `<player>`")
        return
    if not MC_PROFILE_NAME.match(player):
        send(chat_id, "Invalid player name (1–16 letters, digits, underscore).")
        return
    out = rcon(f"pardon {player}")
    log.info("pardon %s by %s", player, admin_name)
    pe, ae = md_escape(player), md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"✅ *{pe}* pardoned by {ae}.\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_pardonip(chat_id: int, arg: str, admin_name: str):
    tok = arg.strip().split(None, 1)
    target = tok[0] if tok else ""
    if not target:
        send(chat_id, "Usage: /pardonip `<ip_or_pattern>`")
        return
    if not BANIP_TARGET.match(target):
        send(chat_id, "Invalid pardon-ip target (letters, digits, `._:*-` only, max 64).")
        return
    out = rcon(f"pardon-ip {target}")
    log.info("pardon-ip %s by %s", target, admin_name)
    tt, ae = md_escape(target), md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"✅ `pardon-ip` *{tt}* by {ae}.\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_time(chat_id: int, arg: str, admin_name: str):
    parts = arg.strip().split()
    if len(parts) == 2 and parts[0].lower() == "query":
        q = parts[1].lower()
        if q not in TIME_QUERY:
            send(chat_id, "`time query` must be: `daytime`, `gametime`, or `day`.")
            return
        out = rcon(f"time query {q}")
    elif len(parts) == 2 and parts[0].lower() == "set":
        raw = parts[1]
        low = raw.lower()
        if low in TIME_SET_WORD:
            out = rcon(f"time set {low}")
        elif re.fullmatch(r"\d{1,9}", raw):
            ticks = int(raw)
            if ticks > 2_147_000_000:
                send(chat_id, "Tick value too large.")
                return
            out = rcon(f"time set {ticks}")
        else:
            send(chat_id, "`time set` needs `day`, `night`, `noon`, `midnight`, or tick digits.")
            return
    else:
        send(
            chat_id,
            "Usage: `/time query daytime|gametime|day` or `/time set day|night|noon|midnight|<ticks>`",
        )
        return
    log.info("time %s by %s", arg.strip(), admin_name)
    ae = md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    detail = md_escape(" ".join(parts))
    text = f"🕐 *time* `{detail}` by {ae}\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_weather(chat_id: int, arg: str, admin_name: str):
    parts = arg.strip().split()
    if len(parts) == 1 and parts[0].lower() in WEATHER_KIND:
        out = rcon(f"weather {parts[0].lower()}")
    elif len(parts) == 2 and parts[0].lower() in WEATHER_KIND:
        if not re.fullmatch(r"\d{1,6}", parts[1]):
            send(chat_id, "Duration must be 1–6 digits (seconds).")
            return
        out = rcon(f"weather {parts[0].lower()} {parts[1]}")
    else:
        send(chat_id, "Usage: `/weather clear|rain|thunder` [seconds]")
        return
    log.info("weather %s by %s", arg.strip(), admin_name)
    ae = md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    detail = md_escape(" ".join(parts))
    text = f"🌦️ *weather* `{detail}` by {ae}\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_difficulty(chat_id: int, arg: str, admin_name: str):
    d = arg.strip().lower()
    if d not in DIFFICULTY:
        send(chat_id, "Usage: `/difficulty peaceful|easy|normal|hard`")
        return
    out = rcon(f"difficulty {d}")
    log.info("difficulty %s by %s", d, admin_name)
    ae = md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"⚔️ `difficulty {d}` by {ae}\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def cmd_gamerule(chat_id: int, arg: str, admin_name: str):
    toks = arg.strip().split(None, 1)
    if not toks or not toks[0]:
        send(chat_id, "Usage: `/gamerule <name>` or `/gamerule <name> <value>`")
        return
    name = toks[0]
    if not GAMERULE_NAME.match(name):
        send(chat_id, "Invalid gamerule name (letters, digits, underscore; start with a letter).")
        return
    if len(toks) == 1:
        out = rcon(f"gamerule {name}")
    else:
        val = toks[1].strip()
        vlow = val.lower()
        if vlow in ("true", "false"):
            out = rcon(f"gamerule {name} {vlow}")
        elif re.fullmatch(r"\d+", val):
            out = rcon(f"gamerule {name} {val}")
        else:
            send(chat_id, "Gamerule value must be `true`, `false`, or a non-negative integer.")
            return
    log.info("gamerule %s by %s", arg.strip(), admin_name)
    ae = md_escape(admin_name)
    tail = md_escape(out) if out else "(no output)"
    text = f"📐 `gamerule` by {ae}\n{tail}"
    send(chat_id, text)
    notify_event("approvals", text, exclude=chat_id)


def parse_rcon_list_player_count(list_out: str) -> int:
    m = re.search(r"There are (\d+) of a max", list_out, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def compose_project_dir() -> str | None:
    """Host directory mounted read-only with docker-compose.yml (for /update)."""
    p = os.environ.get("CREEPWATCH_PROJECT_DIR", "").strip()
    if not p:
        return None
    if (pathlib.Path(p) / "docker-compose.yml").is_file():
        return p
    return None


def run_mc_update_job(admin_label: str, force: bool):
    """Run in a background thread: pull + recreate minecraft."""
    try:
        list_out = rcon("list")
        n = parse_rcon_list_player_count(list_out)
        if n > 0 and not force:
            broadcast(
                "⛔ `/update` aborted — players still online:\n"
                f"{md_escape(list_out)}\n\nUse `/update force` after everyone logs off."
            )
            return
        project = compose_project_dir()
        if not project:
            broadcast(
                "⚠️ `/update` is not configured: set environment variable "
                "`CREEPWATCH_PROJECT_DIR` on mc-guard to the host directory that "
                "contains `docker-compose.yml`, and mount that path read-only."
            )
            return
        ae = md_escape(admin_label)
        broadcast(f"🛑 *Manual Minecraft update* by {ae} — pulling image and recreating `minecraft`…")
        compose = pathlib.Path(project) / "docker-compose.yml"
        base_cmd = [
            "docker", "compose",
            "--project-directory", project,
            "-f", str(compose),
        ]
        pull = subprocess.run(
            base_cmd + ["pull", "minecraft"],
            capture_output=True, text=True, timeout=900,
        )
        if pull.returncode != 0:
            err = (pull.stderr or pull.stdout or "").strip()
            broadcast(f"❌ `docker compose pull` failed:\n{md_escape(err[:2000])}")
            return
        up = subprocess.run(
            base_cmd + ["up", "-d", "--no-deps", "minecraft"],
            capture_output=True, text=True, timeout=300,
        )
        if up.returncode != 0:
            err = (up.stderr or up.stdout or "").strip()
            broadcast(f"❌ `docker compose up` failed:\n{md_escape(err[:2000])}")
            return
        broadcast("✅ *Manual update* — pull and recreate finished. Watch for 🟢 ready / 🆙 version lines.")
        log.info("Manual minecraft update completed (%s)", admin_label)
    except Exception as e:
        log.exception("run_mc_update_job failed")
        broadcast(f"❌ Update error: {md_escape(str(e))[:800]}")


def cmd_update(chat_id: int, arg: str, admin_name: str):
    parts = arg.lower().split()
    force = "force" in parts
    list_out = rcon("list")
    n = parse_rcon_list_player_count(list_out)
    if n > 0 and not force:
        send(
            chat_id,
            "⛔ *Players online* — cannot update yet.\n"
            f"{md_escape(list_out)}\n\nUse `/update force` if everyone should leave first.",
        )
        return
    if not compose_project_dir():
        send(
            chat_id,
            "⚠️ `/update` is not configured on this bot instance "
            "(`CREEPWATCH_PROJECT_DIR` + compose bind mount). See README.",
        )
        return
    threading.Thread(
        target=run_mc_update_job,
        args=(admin_name, force),
        daemon=True,
    ).start()
    send(chat_id, "🕐 Update started in the background — admins get progress messages here.")


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
    elif cmd in ("/kick", "/k"):              cmd_kick(chat_id, arg, sender_name)
    elif cmd in ("/msg", "/tell"):            cmd_msg(chat_id, arg, sender_name)
    elif cmd in ("/wlreload", "/wlr"):       cmd_whitelist_reload(chat_id, arg, sender_name)
    elif cmd in ("/ban", "/bn"):             cmd_ban(chat_id, arg, sender_name)
    elif cmd in ("/banip", "/bi"):           cmd_banip(chat_id, arg, sender_name)
    elif cmd in ("/pardon", "/pd"):          cmd_pardon(chat_id, arg, sender_name)
    elif cmd in ("/pardonip", "/pdi"):       cmd_pardonip(chat_id, arg, sender_name)
    elif cmd == "/time":                     cmd_time(chat_id, arg, sender_name)
    elif cmd == "/weather":                  cmd_weather(chat_id, arg, sender_name)
    elif cmd in ("/difficulty", "/diff"):     cmd_difficulty(chat_id, arg, sender_name)
    elif cmd in ("/gamerule", "/gr"):        cmd_gamerule(chat_id, arg, sender_name)
    elif cmd in ("/settings", "/se"):         cmd_settings(chat_id)
    elif cmd in ("/update", "/up"):          cmd_update(chat_id, arg, sender_name)
    else:                                     send(chat_id, "Unknown command. Try /help")


# ── Approval flow ─────────────────────────────────────────────────────────────

def send_approval_request(player: str):
    pe = md_escape(player)
    keyboard = {"inline_keyboard": [[
        {"text": f"✅ Allow {player}"[:60], "callback_data": f"allow:{player}"},
        {"text": "❌ Deny", "callback_data": f"deny:{player}"},
    ]]}
    for cid in ADMIN_CHAT_IDS:
        try:
            r = requests.post(f"{API}/sendMessage", json={
                "chat_id": cid,
                "text": (
                    "🎮 *New player wants to join!*\n\n"
                    f"Player: *{pe}*\n\n"
                    "Allow them on the Vast Family Minecraft server?"
                ),
                "parse_mode": "Markdown",
                "reply_markup": keyboard,
            }, timeout=10)
            data = r.json()
            if r.ok and data.get("ok"):
                pending[data["result"]["message_id"]] = player
                log.info(f"Approval request for {player} sent to {cid}")
            else:
                _log_telegram_response("sendMessage(approval)", r)
        except Exception as e:
            _log_telegram_response("sendMessage(approval)", None, e)


def poll_callbacks():
    global offset
    r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 5}, timeout=10)
    if not r.ok:
        _log_telegram_response("getUpdates", r)
        return
    for update in r.json().get("result", []):
        offset = update["update_id"] + 1

        # Handle Telegram messages: only pre-approved admins in private DMs.
        # Authorise on from.id (the sender), not chat.id (differs in groups/channels).
        msg = update.get("message", {})
        if msg.get("text"):
            chat = msg.get("chat") or {}
            from_user = msg.get("from") or {}
            reply_chat_id = chat.get("id")
            from_id = from_user.get("id")
            sender_name = from_user.get("first_name", str(from_id or "unknown"))
            if not telegram_allows_admin_interaction(
                chat_type=chat.get("type"),
                chat_id=reply_chat_id,
                from_id=from_id,
            ):
                if msg["text"].startswith("/"):
                    log.info(
                        "ignored command from non-admin chat_type=%s chat_id=%s from_id=%s",
                        chat.get("type"),
                        reply_chat_id,
                        from_id,
                    )
                continue
            if msg["text"].startswith("/"):
                handle_command(reply_chat_id, msg["text"], sender_name)
            else:
                send_admin_chat_to_minecraft(reply_chat_id, msg["text"], sender_name)
            continue

        # Handle inline button callbacks (same lockdown as messages).
        cb = update.get("callback_query")
        if not cb:
            continue

        cb_msg = cb.get("message") or {}
        cb_chat = cb_msg.get("chat") or {}
        from_user = cb.get("from") or {}
        from_id = from_user.get("id")
        chat_id = cb_chat.get("id")
        if not telegram_allows_admin_interaction(
            chat_type=cb_chat.get("type"),
            chat_id=chat_id,
            from_id=from_id,
        ):
            requests.post(
                f"{API}/answerCallbackQuery",
                json={
                    "callback_query_id": cb["id"],
                    "text": "⛔ Not authorised.",
                    "show_alert": True,
                },
                timeout=10,
            )
            continue

        data       = cb["data"]
        msg_id     = cb_msg["message_id"]
        sender_id = from_id
        action, _, arg = data.partition(":")
        admin_name = from_user.get("first_name", str(sender_id or "unknown"))

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
            pe, ae = md_escape(player), md_escape(admin_name)
            result_text = f"✅ *{pe}* was allowed by {ae}."
            pref_key = "approvals"
        elif action == "deny":
            block_player(player)
            pe, ae = md_escape(player), md_escape(admin_name)
            result_text = f"❌ *{pe}* was denied by {ae} and added to the blocked list."
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
    """Yield log lines, reconnecting when the minecraft container restarts."""
    backoff = 5
    while True:
        proc = subprocess.Popen(
            ["docker", "logs", "-f", "--tail", "0", "minecraft"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            if proc.stdout is None:
                log.warning("minecraft log stream has no stdout; retrying in %ss", backoff)
                time.sleep(backoff)
                continue
            for line in proc.stdout:
                yield line.rstrip()
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        log.warning("minecraft log stream ended (container restart?); reattaching in %ss", backoff)
        time.sleep(backoff)

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
        # Successful image update resets Watchtower skip-escalation streak (see scripts/pre-update-check.sh).
        try:
            pathlib.Path("/data/.creepwatch_skip_streak").write_text("0\n0\n")
        except Exception as e:
            log.warning("Could not reset skip streak file: %s", e)
        lv, cv = md_escape(str(last_version or "")), md_escape(current_version)
        if last_version and last_version != "":
            broadcast(f"🆙 *Minecraft updated!*\n\n{lv} → *{cv}*\n\nSend /help for commands.")
        else:
            # Very first start ever
            broadcast(f"🛡️ Whitelist Guard *online* — Minecraft *{cv}*\nSend /help for commands.")
        log.info(f"Version changed: {last_version} -> {current_version}")
    else:
        log.info(f"Version unchanged ({current_version}), no notification sent.")


# ── Main ──────────────────────────────────────────────────────────────────────

def heartbeat_loop():
    """Touch a file on /data so host cron or monitoring can detect a stuck bot."""
    while True:
        try:
            HEARTBEAT_FILE.write_text(str(int(time.time())))
        except Exception as e:
            log.warning("heartbeat write failed: %s", e)
        if HEALTHCHECK_URL:
            try:
                rh = requests.get(HEALTHCHECK_URL, timeout=15)
                if not rh.ok:
                    log.warning(
                        "healthcheck URL HTTP=%s body=%s",
                        rh.status_code,
                        (rh.text or "")[:500],
                    )
            except Exception as e:
                log.warning("healthcheck URL request failed: %s", e)
        time.sleep(max(60, HEARTBEAT_INTERVAL_SEC))


def main():
    log.info("Minecraft Whitelist Guard started")

    # Check version in background so log tailing starts immediately
    threading.Thread(target=check_version_and_notify, daemon=False).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()

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
            event = classify_error(err)
            if not event.alert:
                log.info(f"Suppressed Minecraft log noise ({event.kind}): {event.message}")
                continue
            sig = event.signature
            now = time.time()
            if now - recent_errors.get(sig, 0) > ERROR_COOLDOWN:
                recent_errors[sig] = now
                safe = md_escape(event.message[:1200])
                notify_event("errors", f"⚠️ *Server error*\n{safe}")
            continue

        chat = extract_chat(line)
        if chat:
            player, message = chat
            notify_event("chats", format_player_chat_for_telegram(player, message))
            continue

        m = JOINED_RE.search(line)
        if m:
            name = md_escape(m.group(1))
            notify_event("joins", f"🟢 *{name}* joined the game.")
            continue

        m = LEFT_RE.search(line)
        if m:
            name = md_escape(m.group(1))
            notify_event("leaves", f"⚪ *{name}* left the game.")
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
