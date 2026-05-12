#!/usr/bin/env python3
"""
Minecraft Whitelist Guard Bot
- Notifies all admins when unknown players try to join
- Any admin can Allow/Deny via buttons
- Slash commands for server management
- Only notifies on actual Minecraft version updates (not routine restarts)
"""
import contextlib, subprocess, requests, time, re, logging, os, threading, pathlib, json, datetime, tempfile
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from progress_board import (
    MIN_EDIT_INTERVAL_SEC,
    ProgressBoard,
    ProgressFileTail,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RCON_CMD  = ["docker", "exec", "minecraft", "rcon-cli"]
API       = f"https://api.telegram.org/bot{BOT_TOKEN}"

BACKUP_DIR = pathlib.Path(os.environ.get("CREEPWATCH_BACKUP_DIR", "/backups"))
BACKUP_SCRIPT = pathlib.Path(os.environ.get("CREEPWATCH_BACKUP_SCRIPT", "/scripts/backup.sh"))
RESTORE_SCRIPT = pathlib.Path(os.environ.get("CREEPWATCH_RESTORE_SCRIPT", "/scripts/restore.sh"))
BACKUP_ARCHIVE_RE = re.compile(r"^minecraft-[0-9]{8}T[0-9]{6}Z\.tar\.gz$")
# sendChatAction value while backup runs — "upload_document" is usually easier to spot than "typing" in bot DMs.
BACKUP_TELEGRAM_CHAT_ACTION = "upload_document"

_maintenance_lock = threading.Lock()
_pending_maintenance: dict | None = None

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


def send(chat_id: int, text: str, keyboard=None) -> int | None:
    """Send a message; return its Telegram message_id on success or None.

    The return value lets callers later editMessageText (e.g. for a streaming
    backup/restore progress board) without a separate API round-trip.
    """
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
        _log_telegram_response("sendMessage", r)
        try:
            data = r.json()
        except Exception:
            return None
        if data.get("ok"):
            try:
                return int(data["result"]["message_id"])
            except (KeyError, TypeError, ValueError):
                return None
        return None
    except Exception as e:
        _log_telegram_response("sendMessage", None, e)
        return None


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    """Telegram chat action (typing, upload_document, …); client shows it above the input, not as a message."""
    try:
        r = requests.post(
            f"{API}/sendChatAction",
            data={"chat_id": chat_id, "action": action},
            timeout=5,
        )
        _log_telegram_response("sendChatAction", r)
    except Exception as e:
        _log_telegram_response("sendChatAction", None, e)


def _chat_action_keepalive(chat_id: int, stop: threading.Event, action: str) -> None:
    while not stop.is_set():
        send_chat_action(chat_id, action)
        if stop.wait(4.0):
            break


@contextlib.contextmanager
def backup_typing_indicator(chat_id: int | None, action: str = None):
    """Refresh sendChatAction while a long backup runs (private admin chat only)."""
    if action is None:
        action = BACKUP_TELEGRAM_CHAT_ACTION
    if chat_id is None:
        yield
        return
    stop = threading.Event()
    th = threading.Thread(
        target=_chat_action_keepalive, args=(chat_id, stop, action), daemon=True
    )
    th.start()
    try:
        yield
    finally:
        stop.set()
        th.join(timeout=2)


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

*Players*  /online · /activity · /status
/kick `<p>` `[r]` · /msg `<p>` `<m>` · /ban `<p>` `[r]` · /pardon `<p>` · /banip `<t>` · /pardonip `<t>`

*Whitelist & blocks*
/whitelist · /approve `<p>` · /remove `<p>` · /wlreload · /blocked · /unblock `<p>`

*World*
/time · /weather · /difficulty · /gamerule
/backup — snapshot world (live step board in your chat; queued if anyone is online)
/restore slots — show slot 1–3 (24h gap rule)
/restore `last` · `1` · `2` · `3` · `<file>` — restore (always takes pre-restore safety snapshot first)
/update `[force]` — pull latest MC image and recreate (auto pre-update backup)

*Diagnostics*
/logs `backup`|`restore` `[N]` — tail last N lines (max 50) of the script log
/settings — toggle which events ping you (joins, leaves, errors, chat, …)

Plain text in this DM relays to in-game chat as `[Admin]`.
Common aliases: /bu /rs /up /lg /wl /a /rm /bl /ub /ol /ac /st /se /h"""


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


VILLAGER_MAX_COUNT = 5

# Modern (1.20.5+) component-syntax for fully-kitted tools. Note the
# enchant id is `minecraft:sweeping_edge` since 1.21; older worlds used
# `minecraft:sweeping`. The server here is on DataVersion 4790 (build
# Apr 2026) which uses the renamed id.
SWORD_BASE_ITEM = "netherite_sword"
SWORD_ENCHANT_COMPONENT = (
    '[minecraft:enchantments={'
    '"minecraft:sharpness":5,'
    '"minecraft:mending":1,'
    '"minecraft:unbreaking":3,'
    '"minecraft:looting":3,'
    '"minecraft:sweeping_edge":3'
    '}]'
)
SWORD_SUMMARY = (
    "Sharpness V · Mending · Unbreaking III · Looting III · Sweeping Edge III"
)

PICKAXE_BASE_ITEM = "netherite_pickaxe"
PICKAXE_ENCHANT_COMPONENT = (
    '[minecraft:enchantments={'
    '"minecraft:efficiency":5,'
    '"minecraft:unbreaking":3,'
    '"minecraft:mending":1,'
    '"minecraft:fortune":3'
    '}]'
)
PICKAXE_SUMMARY = "Efficiency V · Unbreaking III · Mending · Fortune III"

# Tridents come in two mutually-exclusive enchant flavours: the "thrown"
# build (Loyalty + Channeling) and the "movement" build (Riptide). We
# expose the thrown build here — Loyalty returns the trident, Channeling
# fires lightning on hit during thunderstorms, Impaling adds bonus
# damage to aquatic mobs.
TRIDENT_BASE_ITEM = "trident"
TRIDENT_ENCHANT_COMPONENT = (
    '[minecraft:enchantments={'
    '"minecraft:loyalty":3,'
    '"minecraft:impaling":5,'
    '"minecraft:channeling":1,'
    '"minecraft:mending":1,'
    '"minecraft:unbreaking":3'
    '}]'
)
TRIDENT_SUMMARY = "Loyalty III · Impaling V · Channeling · Mending · Unbreaking III"

# Aquatic helmet: water-breathing + underwater mining + max armour
# protection. Two components on the item:
#  1) minecraft:enchantments — the five enchant ids/levels.
#  2) minecraft:attribute_modifiers — adds +10 `minecraft:armor` from the
#     head slot. Combined with the helmet's base 2 armor that's 12 armor
#     points, ≈48–50% damage reduction when this is the only armor worn
#     (4% per point, capped at 80% at 20 points). With other armor on, it
#     stacks normally toward the 20-point cap. Protection IV adds on top.
#
# The `id:` field is a resource location unique to this modifier; we
# namespace it `creepwatch:ts_armor` so a future modifier from another
# command cannot collide and accidentally cancel the bonus.
TURTLE_SHELL_BASE_ITEM = "turtle_helmet"
TURTLE_SHELL_COMPONENT = (
    "["
    "minecraft:enchantments={"
    '"minecraft:respiration":3,'
    '"minecraft:aqua_affinity":1,'
    '"minecraft:protection":4,'
    '"minecraft:unbreaking":3,'
    '"minecraft:mending":1'
    "},"
    "minecraft:attribute_modifiers={modifiers:["
    '{type:"minecraft:armor",amount:10,operation:"add_value",'
    'slot:"head",id:"creepwatch:ts_armor"}'
    "]}"
    "]"
)
TURTLE_SHELL_SUMMARY = (
    "Respiration III · Aqua Affinity · Protection IV · Unbreaking III · Mending"
    " · +10 armor (≈50% damage reduction)"
)

GIVE_FAILURE_SIGNALS = (
    "Entity not found",
    "No entity was found",
    "RCON error",
    "Unknown or incomplete command",
    "Failed to parse",
    "Unknown enchantment",
)

# Vanilla structure registry ids used by the hidden /<structure>
# spawning commands. Modern (1.18+) `/place structure <id>` syntax. The
# offset on the place command keeps the player out of the wall the
# structure rests against — small for compact structures, larger for
# multi-chunk monsters like mansion and monument.
SHIPWRECK_STRUCTURE = "minecraft:shipwreck_beached"      # sand-buried wreck w/ chests
MANSION_STRUCTURE = "minecraft:mansion"                  # ~60×60 woodland mansion
BURIED_TREASURE_STRUCTURE = "minecraft:buried_treasure"  # single chest under sand
OCEAN_RUIN_STRUCTURE = "minecraft:ocean_ruin_warm"       # sandstone ruins
MONUMENT_STRUCTURE = "minecraft:monument"                # ocean monument w/ guardians
IGLOO_STRUCTURE = "minecraft:igloo"                      # snowy igloo w/ optional cellar
RUINED_PORTAL_STRUCTURE = "minecraft:ruined_portal"      # vanilla picks variant by biome

STRUCTURE_FAILURE_SIGNALS = (
    "Entity not found",
    "No entity was found",
    "RCON error",
    "Failed to parse",
    "Unknown or incomplete command",
    "Could not place",
    "couldn't place",
    "Cannot place",
)


def _place_structure_near_player(
    chat_id: int,
    arg: str,
    admin_name: str,
    *,
    icon: str,
    structure_id: str,
    offset: str,
    cmd_label: str,
    description: str,
    note: str = "",
) -> None:
    """Shared body for hidden admin commands that spawn a structure near
    the target player via vanilla `place structure`. Mirrors the
    `_give_enchanted_item` helper used by /sword, /pickaxe, /ts: every
    structure command is now a single helper call plus its tuple of
    (icon, structure_id, offset, label, description, note)."""
    toks = arg.strip().split()
    if not toks:
        send(chat_id, f"Usage: /{cmd_label} `<player>`")
        return
    player = toks[0]
    if not MC_PROFILE_NAME.match(player):
        send(chat_id, "Invalid player name (1–16 letters, digits, underscore).")
        return
    place_cmd = f"execute at {player} run place structure {structure_id} {offset}"
    out = rcon(place_cmd)
    log.info("%s for %s by %s: %s", cmd_label, player, admin_name, out)
    pe, ae = md_escape(player), md_escape(admin_name)
    if any(sig in out for sig in STRUCTURE_FAILURE_SIGNALS):
        send(chat_id, f"❌ /{cmd_label} for *{pe}* failed:\n`{md_escape(out[:400])}`")
        return
    tail = md_escape(out) if out else "(no output)"
    note_block = f"\n{note}" if note else ""
    send(
        chat_id,
        f"{icon} Spawned {description} near *{pe}* (by {ae}).{note_block}\n`{tail}`",
    )


def cmd_mansion(chat_id: int, arg: str, admin_name: str):
    """Woodland mansion. Hidden from /help."""
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="🏰",
        structure_id=MANSION_STRUCTURE,
        offset="~50 ~ ~50",
        cmd_label="mansion",
        description="a woodland mansion ~50 blocks NE",
        note="Generation may cause a brief lag spike — that's the chunks rewriting.",
    )


def cmd_ship(chat_id: int, arg: str, admin_name: str):
    """Beached shipwreck (treasure ship). Hidden from /help."""
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="🚢",
        structure_id=SHIPWRECK_STRUCTURE,
        offset="~3 ~ ~3",
        cmd_label="ship",
        description="a beached shipwreck",
        note="For the best fit, stand on a sandy shore.",
    )


def cmd_buried(chat_id: int, arg: str, admin_name: str):
    """Buried treasure (single chest). Hidden from /help."""
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="💰",
        structure_id=BURIED_TREASURE_STRUCTURE,
        offset="~3 ~-1 ~3",
        cmd_label="buried",
        description="a buried treasure chest",
        note="Best fit: stand on a sandy beach (chest is one block under).",
    )


def cmd_ruin(chat_id: int, arg: str, admin_name: str):
    """Warm ocean ruins (sandstone). Hidden from /help."""
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="🏛️",
        structure_id=OCEAN_RUIN_STRUCTURE,
        offset="~10 ~ ~10",
        cmd_label="ruin",
        description="warm ocean ruins",
        note="Best fit: shallow warm water (the structure is designed half-submerged).",
    )


def cmd_monument(chat_id: int, arg: str, admin_name: str):
    """Ocean monument. Hidden from /help.

    Ocean monuments are the second-largest vanilla structure after the
    mansion; offset is biggest of all the structure commands so the
    player is well clear of the prismarine walls. Guardians spawn
    inside on generation, which is part of the experience but also a
    fast way to lose the player's gear — heads up in the note.
    """
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="🏯",
        structure_id=MONUMENT_STRUCTURE,
        offset="~60 ~-10 ~60",
        cmd_label="monument",
        description="an ocean monument ~60 blocks NE",
        note="Best fit: deep water nearby. Guardians spawn inside on placement.",
    )


def cmd_igloo(chat_id: int, arg: str, admin_name: str):
    """Snowy igloo. Hidden from /help."""
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="🛖",
        structure_id=IGLOO_STRUCTURE,
        offset="~5 ~ ~5",
        cmd_label="igloo",
        description="an igloo",
        note="Best fit: stand on snow. Some igloos get a basement with a brewing setup.",
    )


def cmd_portal(chat_id: int, arg: str, admin_name: str):
    """Ruined nether portal. Hidden from /help.

    Vanilla `place structure minecraft:ruined_portal` picks the variant
    fitting the surrounding biome (desert/jungle/mountain/swamp/etc).
    We don't pin a specific variant so the structure looks natural
    wherever the player is."""
    _place_structure_near_player(
        chat_id, arg, admin_name,
        icon="🌀",
        structure_id=RUINED_PORTAL_STRUCTURE,
        offset="~10 ~ ~10",
        cmd_label="portal",
        description="a ruined nether portal",
        note="Vanilla picks the variant matching the surrounding biome.",
    )


def _give_enchanted_item(
    chat_id: int,
    arg: str,
    admin_name: str,
    *,
    icon: str,
    base_item: str,
    component: str,
    summary: str,
    cmd_label: str,
) -> None:
    """Shared body for hidden admin commands that give one fully-kitted
    item to a target player. Centralises argument validation, the
    `give` RCON call, and Telegram formatting so each command becomes a
    one-line wrapper. Failure signals are surfaced verbatim so a future
    Minecraft rename of an item or enchant id is visibly loud."""
    toks = arg.strip().split()
    if not toks:
        send(chat_id, f"Usage: /{cmd_label} `<player>`")
        return
    player = toks[0]
    if not MC_PROFILE_NAME.match(player):
        send(chat_id, "Invalid player name (1–16 letters, digits, underscore).")
        return
    give_cmd = f"give {player} {base_item}{component} 1"
    out = rcon(give_cmd)
    log.info("%s to %s by %s: %s", cmd_label, player, admin_name, out)
    pe, ae = md_escape(player), md_escape(admin_name)
    if any(sig in out for sig in GIVE_FAILURE_SIGNALS):
        send(chat_id, f"❌ /{cmd_label} for *{pe}* failed:\n`{md_escape(out[:400])}`")
        return
    tail = md_escape(out) if out else "(no output)"
    pretty_item = base_item.replace("_", " ")
    send(
        chat_id,
        f"{icon} Gave *{pe}* a {pretty_item} with {summary} (by {ae}).\n`{tail}`",
    )


def cmd_sword(chat_id: int, arg: str, admin_name: str):
    """Diamond sword with the melee god-roll. Hidden from /help."""
    _give_enchanted_item(
        chat_id, arg, admin_name,
        icon="⚔️",
        base_item=SWORD_BASE_ITEM,
        component=SWORD_ENCHANT_COMPONENT,
        summary=SWORD_SUMMARY,
        cmd_label="sword",
    )


def cmd_pickaxe(chat_id: int, arg: str, admin_name: str):
    """Diamond pickaxe with the mining god-roll. Hidden from /help."""
    _give_enchanted_item(
        chat_id, arg, admin_name,
        icon="⛏️",
        base_item=PICKAXE_BASE_ITEM,
        component=PICKAXE_ENCHANT_COMPONENT,
        summary=PICKAXE_SUMMARY,
        cmd_label="pickaxe",
    )


def cmd_trident(chat_id: int, arg: str, admin_name: str):
    """Trident with the thrown/lightning build. Hidden from /help."""
    _give_enchanted_item(
        chat_id, arg, admin_name,
        icon="🔱",
        base_item=TRIDENT_BASE_ITEM,
        component=TRIDENT_ENCHANT_COMPONENT,
        summary=TRIDENT_SUMMARY,
        cmd_label="trident",
    )


def cmd_turtle_shell(chat_id: int, arg: str, admin_name: str):
    """Turtle-shell helmet for underwater work plus a damage shield.
    Hidden from /help."""
    _give_enchanted_item(
        chat_id, arg, admin_name,
        icon="🐢",
        base_item=TURTLE_SHELL_BASE_ITEM,
        component=TURTLE_SHELL_COMPONENT,
        summary=TURTLE_SHELL_SUMMARY,
        cmd_label="ts",
    )


def cmd_villager(chat_id: int, arg: str, admin_name: str):
    """Spawn N (1–5) villagers next to a target player. Hidden from /help.

    Intentionally not advertised in HELP_TEXT — admins only learn it from
    out-of-band channels. Still admin-gated by the dispatcher, so the
    only thing "hidden" buys us is keeping the command list short for
    everyone else.

    Implementation note: vanilla Java edition Minecraft. The
    `execute at <player> run summon villager ~ ~ ~` form anchors at the
    target's position so the spawned mob appears in their face.
    """
    toks = arg.strip().split(None, 1)
    if not toks or not toks[0]:
        send(chat_id, "Usage: /villager `<player>` `[count]` (count 1–5)")
        return
    player = toks[0]
    if not MC_PROFILE_NAME.match(player):
        send(chat_id, "Invalid player name (1–16 letters, digits, underscore).")
        return
    count = 1
    if len(toks) > 1 and toks[1].strip():
        try:
            count = int(toks[1].strip())
        except ValueError:
            send(chat_id, f"Count must be a whole number 1–{VILLAGER_MAX_COUNT}.")
            return
        if count < 1 or count > VILLAGER_MAX_COUNT:
            send(chat_id, f"Count must be between 1 and {VILLAGER_MAX_COUNT}.")
            return

    # Each `summon` is a separate RCON call. We surface the LAST RCON
    # response (they're all near-identical for success cases) and bail
    # on the first failure so the operator doesn't see "Spawned 5" when
    # only 2 actually landed.
    last_out = ""
    for i in range(count):
        out = rcon(f"execute at {player} run summon villager ~ ~ ~")
        last_out = out
        # rcon() returns the message body on failure too, so detect the
        # "Entity not found" / "No entity was found" cases by content.
        if "Entity not found" in out or "No entity was found" in out or "RCON error" in out:
            log.warning("villager spawn aborted at %d/%d near %s by %s: %s",
                        i + 1, count, player, admin_name, out)
            pe, ae = md_escape(player), md_escape(admin_name)
            send(
                chat_id,
                f"❌ Spawn aborted at {i}/{count} for *{pe}* (by {ae}).\n"
                f"`{md_escape(out[:300])}`",
            )
            return
    log.info("villager spawn %d near %s by %s: %s", count, player, admin_name, last_out)
    pe, ae = md_escape(player), md_escape(admin_name)
    tail = md_escape(last_out) if last_out else "(no output)"
    send(chat_id, f"🧙 Spawned {count} villager(s) next to *{pe}* (by {ae}).\n`{tail}`")


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


def players_online() -> bool:
    return parse_rcon_list_player_count(rcon("list")) > 0


def maintenance_pending() -> bool:
    with _maintenance_lock:
        return _pending_maintenance is not None


def build_maintenance_scheduled_tellraw(kind: str) -> str:
    msg = (
        "A full world backup will run as soon as nobody is online."
        if kind == "backup"
        else "The world will be restored from a backup when nobody is online (downtime and rollback)."
    )
    payload = [
        {"text": "[Server] ", "color": "red", "bold": True},
        {"text": msg, "color": "yellow"},
    ]
    return "tellraw @a " + json.dumps(payload, ensure_ascii=False)


def schedule_maintenance(kind: str, chat_id: int, admin_name: str, restore_spec: str | None) -> bool:
    """Return False if another maintenance request is already queued."""
    global _pending_maintenance
    with _maintenance_lock:
        if _pending_maintenance is not None:
            return False
        _pending_maintenance = {
            "kind": kind,
            "chat_id": chat_id,
            "admin": admin_name,
            "restore": restore_spec,
        }
    out = rcon(build_maintenance_scheduled_tellraw(kind))
    if out and "RCON error" in out:
        log.warning("tellraw maintenance schedule failed: %s", out)
    ae = md_escape(admin_name)
    label = "backup" if kind == "backup" else "restore"
    notify_event("restarts", f"📌 *Maintenance scheduled* ({label}) by {ae} — runs when the server is empty.")
    return True


def discover_backup_host_dir() -> str | None:
    """Inspect mc-guard's own container to find the host source of /backups.

    Required because `docker run -v <src>:<dst>` in backup.sh / restore.sh
    sends `<src>` to the host daemon, so it must be a host path even when
    the script runs inside mc-guard. This avoids forcing every operator to
    set CREEPWATCH_BACKUP_DIR_HOST or CREEPWATCH_PROJECT_DIR by hand: a
    fresh deploy of mc-guard can self-discover from the bind mount the
    operator already declared in docker-compose.yml.
    """
    real = os.environ.get("DOCKER_REAL", "/usr/bin/docker.real")
    if not pathlib.Path(real).is_file():
        return None
    target = str(BACKUP_DIR)
    fmt = (
        '{{ range .Mounts }}{{ if eq .Destination "' + target + '" }}'
        '{{ .Source }}{{ end }}{{ end }}'
    )
    try:
        out = subprocess.run(
            [real, "inspect", "mc-guard", "--format", fmt],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        log.warning("discover_backup_host_dir failed: %s", e)
        return None
    if out.returncode != 0:
        return None
    v = out.stdout.strip()
    return v or None


def backup_dir_host_for_docker_bind() -> str | None:
    """Host directory for docker run -v when backup/restore run inside mc-guard.

    The Docker socket is the host daemon; bind mount sources must be host paths.
    Resolution order, first non-empty wins:
      1. CREEPWATCH_BACKUP_DIR_HOST env (explicit operator override)
      2. CREEPWATCH_PROJECT_DIR/backups (legacy contract)
      3. docker inspect mc-guard .Mounts[/backups].Source (self-discovery)
    """
    raw = os.environ.get("CREEPWATCH_BACKUP_DIR_HOST", "").strip()
    if raw:
        return raw
    p = compose_project_dir()
    if p:
        return str(pathlib.Path(p) / "backups")
    return discover_backup_host_dir()


def _subprocess_env_for_backup_restore() -> dict:
    env = os.environ.copy()
    env["BACKUP_DIR"] = str(BACKUP_DIR)
    host_b = backup_dir_host_for_docker_bind()
    if host_b:
        env["BACKUP_DOCKER_HOST_DIR"] = host_b
    project = compose_project_dir()
    if project:
        env["CREEPWATCH_PROJECT_DIR"] = project
    return env


def run_backup_subprocess(progress_file: str | None = None) -> subprocess.CompletedProcess:
    env = _subprocess_env_for_backup_restore()
    if progress_file:
        env["BACKUP_PROGRESS_FILE"] = progress_file
    return subprocess.run(
        ["/bin/sh", str(BACKUP_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=7200,
    )


# Slot 2/3 must be at least this far back from the previous slot. Backups
# taken in the same 24h window only ever populate slot 1, so frequent
# same-day /backup runs cannot evict day-1 / day-2 restore points from the
# slot ladder.
SLOT_GAP_SECONDS = 24 * 3600
SLOT_COUNT = 3


def _archive_basename_to_utc(name: str) -> datetime.datetime | None:
    m = re.fullmatch(r"minecraft-(\d{8})T(\d{6})Z\.tar\.gz", name)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(
            m.group(1) + m.group(2), "%Y%m%d%H%M%S"
        ).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def pick_slot_archives(
    names_newest_first: list[str],
    gap_seconds: int = SLOT_GAP_SECONDS,
    max_slots: int = SLOT_COUNT,
) -> list[str]:
    """Return up to `max_slots` basenames forming a 24h-gap restore ladder.

    Slot 1 = newest archive overall.
    Slot 2 = newest archive at least `gap_seconds` older than slot 1.
    Slot 3 = newest archive at least `gap_seconds` older than slot 2.

    Same-day repeats land on slot 1 only; older days stay anchored to
    slots 2/3. Both backup retention (when slot mode is active) and
    /restore selection share this function so the displayed slot list
    matches the kept-on-disk set.
    """
    picks: list[str] = []
    last_ts: datetime.datetime | None = None
    for n in names_newest_first:
        ts = _archive_basename_to_utc(n)
        if ts is None:
            continue
        if last_ts is None:
            picks.append(n)
            last_ts = ts
            if len(picks) >= max_slots:
                break
            continue
        if (last_ts - ts).total_seconds() >= gap_seconds:
            picks.append(n)
            last_ts = ts
            if len(picks) >= max_slots:
                break
    return picks


# ── R2 mirror listing for /restore status indicator ──────────────────────────
# Listing is much more frequent than uploading (every /restore list or
# /restore slots calls it), so we use boto3 for sub-second S3 calls and
# cache for a short window — back-to-back commands reuse the listing.
# Upload/restore themselves still go through aws-cli-in-docker (in the
# shell scripts) to keep them self-contained for host/cron use.
try:
    import boto3 as _boto3
    from botocore.config import Config as _BotoConfig
except ImportError:  # pragma: no cover — boto3 missing only matters in prod.
    _boto3 = None
    _BotoConfig = None

R2_LIST_CACHE_TTL = 30.0
_r2_list_cache: tuple[float, frozenset[str] | None] = (0.0, None)
_r2_list_cache_lock = threading.Lock()


def _r2_s3_client():
    """Return a boto3 S3 client for R2, or None if R2 isn't configured."""
    if _boto3 is None:
        return None
    akid = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
    sec = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
    endpoint = os.environ.get("R2_S3_ENDPOINT", "").strip()
    if not (akid and sec and endpoint):
        return None
    return _boto3.client(
        "s3",
        aws_access_key_id=akid,
        aws_secret_access_key=sec,
        endpoint_url=endpoint,
        region_name="auto",
        config=_BotoConfig(
            retries={"max_attempts": 2, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
        ),
    )


def r2_list_basenames(force: bool = False) -> frozenset[str] | None:
    """Return the set of `minecraft-*.tar.gz` basenames in the R2 bucket under R2_PREFIX.

    Returns None when R2 is not configured or the listing fails (network,
    auth, missing bucket). Callers should treat None as "status unknown"
    rather than "definitely not on R2" — we don't want to mark every
    archive as local-only just because R2 had a hiccup.

    `force=True` bypasses and refreshes the cache; mc-guard uses it after
    a successful /backup so the next /restore list reflects the upload.
    """
    global _r2_list_cache
    now = time.monotonic()
    if not force:
        with _r2_list_cache_lock:
            ts, cached = _r2_list_cache
            if cached is not None and (now - ts) < R2_LIST_CACHE_TTL:
                return cached

    cli = _r2_s3_client()
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if cli is None or not bucket:
        with _r2_list_cache_lock:
            _r2_list_cache = (now, None)
        return None
    prefix = os.environ.get("R2_PREFIX", "minecraft/").strip() or "minecraft/"

    try:
        names: set[str] = set()
        paginator = cli.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                # Key is the full prefix path, e.g. "minecraft/minecraft-…tar.gz".
                bn = obj["Key"].rsplit("/", 1)[-1]
                if BACKUP_ARCHIVE_RE.fullmatch(bn):
                    names.add(bn)
        frozen = frozenset(names)
        with _r2_list_cache_lock:
            _r2_list_cache = (now, frozen)
        return frozen
    except Exception as e:
        log.warning("r2_list_basenames failed (%s); next /restore list will retry", e)
        # Don't poison the cache with a stale-good value, but also don't
        # spam R2 — short TTL on the None entry covers brief outages.
        with _r2_list_cache_lock:
            _r2_list_cache = (now, None)
        return None


def r2_indicator_for(basename: str, remote: frozenset[str] | None) -> str:
    """Return the per-archive status emoji.

    - ✅ when the archive is also on R2 (mirrored)
    - 📍 when it exists locally but is not on R2 yet (or was pruned remotely)
    - empty when R2 is not configured or unreachable (we don't pretend to know)
    """
    if remote is None:
        return ""
    return "✅" if basename in remote else "📍"


def sorted_backup_basenames() -> list[str]:
    """Newest-first minecraft-*.tar.gz basenames under BACKUP_DIR."""
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(
        (p.name for p in BACKUP_DIR.iterdir() if p.is_file() and BACKUP_ARCHIVE_RE.fullmatch(p.name)),
        reverse=True,
    )


def restore_spec_error(spec: str) -> str | None:
    """None if spec is usable by restore.sh; else a short error for Telegram."""
    s = spec.strip()
    sl = s.lower()
    names = sorted_backup_basenames()
    slots = pick_slot_archives(names)
    if sl == "last":
        return "No backups in the backup directory yet." if not slots else None
    if sl in ("1", "2", "3"):
        i = int(sl)
        if len(slots) < i:
            return (
                f"Slot {sl} is not available — only {len(slots)} slot(s) populated "
                f"(slots 2/3 require ≥24h gap from the previous slot)."
            )
        return None
    if BACKUP_ARCHIVE_RE.fullmatch(s):
        return None if (BACKUP_DIR / s).is_file() else f"File not found: `{md_escape(s)}`"
    return "Invalid restore target. Use `/restore slots`, `last`, `1`–`3`, or a full backup filename."


def resolve_restore_slot(spec: str) -> str | None:
    """Map `last`/`1`/`2`/`3` to the actual basename via the 24h-gap ladder.

    Pass full filenames through unchanged. Returns None if the spec cannot
    be resolved (caller has already validated via `restore_spec_error`, but
    the timing window between validation and resolution can race a prune).
    """
    s = spec.strip()
    sl = s.lower()
    if BACKUP_ARCHIVE_RE.fullmatch(s):
        return s
    slots = pick_slot_archives(sorted_backup_basenames())
    if not slots:
        return None
    if sl == "last":
        return slots[0]
    if sl in ("1", "2", "3"):
        i = int(sl)
        if i <= len(slots):
            return slots[i - 1]
    return None


def run_restore_subprocess(restore_arg: str, progress_file: str | None = None) -> subprocess.CompletedProcess:
    env = _subprocess_env_for_backup_restore()
    if progress_file:
        env["RESTORE_PROGRESS_FILE"] = progress_file
    return subprocess.run(
        ["/bin/sh", str(RESTORE_SCRIPT), restore_arg],
        env=env,
        capture_output=True,
        text=True,
        timeout=7200,
    )


# ── Progress board runner ─────────────────────────────────────────────────────

def _run_task_with_progress_board(
    kind: str,
    chat_id: int | None,
    runner,
) -> tuple[subprocess.CompletedProcess, ProgressBoard | None]:
    """Spawn a backup/restore subprocess while streaming a step board to chat_id.

    `runner(progress_file: str | None) -> CompletedProcess` is the actual
    subprocess invoker (run_backup_subprocess or run_restore_subprocess
    wrapped to capture the restore spec). The split keeps script-specific
    env wiring out of this function.

    When chat_id is None (host/cron path), no board is rendered, no
    progress file is created, and the subprocess output is unchanged.
    """
    if chat_id is None:
        r = runner(None)
        return r, None

    board = ProgressBoard(kind)
    initial_text = board.render()
    msg_id = send(chat_id, initial_text)
    if msg_id is None:
        # Telegram unreachable / API error — fall back to silent run rather
        # than spinning up a poller that can never publish updates.
        log.warning("progress board: sendMessage failed; running %s without live board", kind)
        r = runner(None)
        return r, board

    progress_path_obj = pathlib.Path(tempfile.mkdtemp(prefix=f"creepwatch-{kind}-")) / "progress.log"
    # Touch the file so the tail poller's first read sees an empty file
    # rather than FileNotFoundError until the script's first append.
    progress_path_obj.write_text("", encoding="utf-8")
    tail = ProgressFileTail(progress_path_obj, board)

    stop = threading.Event()

    def edit_board() -> None:
        edit(chat_id, msg_id, board.render())

    def poll_loop() -> None:
        last_edit = 0.0
        last_text = initial_text
        while not stop.is_set():
            tail.poll()
            now = time.monotonic()
            text = board.render()
            if text != last_text and (now - last_edit) >= MIN_EDIT_INTERVAL_SEC:
                edit(chat_id, msg_id, text)
                last_text = text
                last_edit = now
            if stop.wait(0.6):
                break

    th = threading.Thread(target=poll_loop, name=f"progress-{kind}", daemon=True)
    th.start()

    try:
        r = runner(str(progress_path_obj))
    finally:
        stop.set()
        th.join(timeout=5)

    # Drain any events the script wrote after our last poll, then render the
    # final state with the actual exit code.
    tail.poll()
    board.mark_done(r.returncode == 0)
    try:
        edit_board()
    except Exception:
        log.exception("final board edit")

    try:
        progress_path_obj.unlink(missing_ok=True)
        progress_path_obj.parent.rmdir()
    except Exception:
        pass

    return r, board


def _format_failure_tail(r: subprocess.CompletedProcess) -> str:
    """Render the last ~1.8KB of subprocess output as a Markdown code block."""
    blob = ((r.stdout or "") + (r.stderr or "")).strip()
    if not blob:
        return "(no script output)"
    return f"```\n{blob[-1800:]}\n```"


def _cmd_backup_worker(chat_id: int, admin_name: str):
    """Runs backup.sh with a live step board in chat_id; broadcasts the final outcome."""
    try:
        send_chat_action(chat_id, BACKUP_TELEGRAM_CHAT_ACTION)
        with backup_typing_indicator(chat_id):
            r, _board = _run_task_with_progress_board(
                kind="backup",
                chat_id=chat_id,
                runner=lambda pf: run_backup_subprocess(progress_file=pf),
            )
        if r.returncode != 0:
            log.warning(
                "backup.sh exit=%s admin=%s stderr_head=%r stdout_head=%r",
                r.returncode,
                admin_name,
                (r.stderr or "")[:800],
                (r.stdout or "")[:400],
            )
            broadcast(f"❌ *Backup failed* (exit {r.returncode}) — see /logs backup")
            send(chat_id, f"❌ *Backup failed*, exit {r.returncode}.\n{_format_failure_tail(r)}")
            return
        # Refresh the cached R2 listing so the next /restore list shows the
        # archive we just uploaded — and any old archives we just pruned —
        # without waiting out the 30s TTL.
        r2_list_basenames(force=True)
        broadcast("✅ *World backup* finished.")
    except Exception as e:
        log.exception("backup worker")
        err = md_escape(str(e)[:800])
        broadcast(f"❌ *Backup crashed*: {err}")
        send(chat_id, f"❌ Backup crashed: {err}")


def _cmd_restore_worker(chat_id: int, admin_name: str, restore_spec: str):
    """Runs restore.sh with a live step board in chat_id; broadcasts the final outcome."""
    try:
        send_chat_action(chat_id, BACKUP_TELEGRAM_CHAT_ACTION)
        with backup_typing_indicator(chat_id):
            r, _board = _run_task_with_progress_board(
                kind="restore",
                chat_id=chat_id,
                runner=lambda pf: run_restore_subprocess(restore_spec, progress_file=pf),
            )
        if r.returncode != 0:
            log.warning(
                "restore.sh exit=%s admin=%s spec=%r stderr_head=%r stdout_head=%r",
                r.returncode,
                admin_name,
                restore_spec,
                (r.stderr or "")[:800],
                (r.stdout or "")[:400],
            )
            broadcast(f"❌ *Restore failed* (exit {r.returncode}) — see /logs restore")
            send(chat_id, f"❌ *Restore failed*.\n{_format_failure_tail(r)}")
            return
        broadcast("✅ *World restore* finished — Minecraft was started again.")
    except Exception as e:
        log.exception("restore worker")
        err = md_escape(str(e)[:800])
        broadcast(f"❌ *Restore crashed*: {err}")
        send(chat_id, f"❌ Restore crashed: {err}")


# Persistent backup/restore log files written by the shell scripts. The
# /logs command tails these so admins can post-mortem a failed run without
# SSHing to the host.
BACKUP_LOG_FILE = pathlib.Path(os.environ.get("BACKUP_LOG_FILE", "/data/logs/backup.log"))
RESTORE_LOG_FILE = pathlib.Path(os.environ.get("RESTORE_LOG_FILE", "/data/logs/restore.log"))
LOGS_MAX_LINES = 50
LOGS_MAX_BYTES = 16_000


def _tail_log_file(path: pathlib.Path, max_lines: int, max_bytes: int) -> list[str]:
    """Return the last <=max_lines lines of a log file, never reading >max_bytes.

    The byte cap matters because a single misbehaving run could leave a
    multi-MB log and we don't want to OOM mc-guard just to print a tail.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            start = max(0, size - max_bytes)
            fh.seek(start)
            raw = fh.read()
    except FileNotFoundError:
        return []
    except OSError as e:
        log.warning("tail log %s: %s", path, e)
        return []
    text = raw.decode(errors="replace")
    # If we sliced mid-line, drop the first partial line.
    if start > 0 and "\n" in text:
        text = text.split("\n", 1)[1]
    return text.splitlines()[-max_lines:]


def cmd_logs(chat_id: int, arg: str):
    """`/logs backup [N]` or `/logs restore [N]` — tail the script log file."""
    parts = arg.split()
    task = parts[0].lower() if parts else "backup"
    if task in ("bu", "backup"):
        path = BACKUP_LOG_FILE
        label = "backup"
    elif task in ("rs", "restore"):
        path = RESTORE_LOG_FILE
        label = "restore"
    else:
        send(chat_id, "Usage: `/logs backup [N]` · `/logs restore [N]` (N = last N lines, max 50)")
        return
    n_lines = LOGS_MAX_LINES
    if len(parts) >= 2:
        try:
            n_lines = max(1, min(LOGS_MAX_LINES, int(parts[1])))
        except ValueError:
            send(chat_id, f"`{md_escape(parts[1])}` is not a number. Usage: `/logs {label} [N]`.")
            return
    if not path.is_file():
        send(chat_id, f"📄 No `{md_escape(label)}.log` yet at `{md_escape(str(path))}`.")
        return
    lines = _tail_log_file(path, n_lines, LOGS_MAX_BYTES)
    if not lines:
        send(chat_id, f"📄 `{md_escape(label)}.log` is empty.")
        return
    # Telegram message limit is 4096 chars; leave headroom for the code fence.
    body = "\n".join(lines)
    if len(body) > 3800:
        body = body[-3800:]
        body = body.split("\n", 1)[1] if "\n" in body else body
    send(chat_id, f"📄 *Last {len(lines)} lines* of `{md_escape(label)}.log`\n```\n{body}\n```")


def cmd_backup(chat_id: int, admin_name: str):
    if not BACKUP_SCRIPT.is_file():
        send(
            chat_id,
            "⚠️ Backup script is not mounted. Add `./scripts:/scripts:ro` on mc-guard and `./backups` (see README).",
        )
        return
    if players_online():
        if not schedule_maintenance("backup", chat_id, admin_name, None):
            send(chat_id, "⛔ Another backup or restore is already scheduled. Wait for the lobby to clear.")
            return
        send(chat_id, "📌 *Backup scheduled* — players were messaged in-game; it runs when nobody is online.")
        return
    broadcast(f"📦 *World backup* started by {md_escape(admin_name)}…")
    send_chat_action(chat_id, BACKUP_TELEGRAM_CHAT_ACTION)
    threading.Thread(target=_cmd_backup_worker, args=(chat_id, admin_name), daemon=True).start()


def cmd_restore(chat_id: int, arg: str, admin_name: str):
    parts = arg.strip().split()
    if not parts:
        send(
            chat_id,
            "Usage: `/restore slots` · `/restore list` · `/restore last` · `/restore 1`–`3` · `/restore <filename>`",
        )
        return
    if not RESTORE_SCRIPT.is_file():
        send(
            chat_id,
            "⚠️ Restore script is not mounted. Add `./scripts:/scripts:ro` on mc-guard (see README).",
        )
        return
    if not BACKUP_DIR.is_dir():
        send(chat_id, f"⚠️ Backup directory missing: `{md_escape(str(BACKUP_DIR))}`")
        return

    sub = parts[0]
    if sub.lower() == "slots":
        slots = pick_slot_archives(sorted_backup_basenames())
        remote = r2_list_basenames()
        lines = []
        for i in range(1, SLOT_COUNT + 1):
            tag = " (newest)" if i == 1 else ""
            if i <= len(slots):
                bn = slots[i - 1]
                ts = _archive_basename_to_utc(bn)
                ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "(unparseable timestamp)"
                ind = r2_indicator_for(bn, remote)
                ind_prefix = f"{ind} " if ind else ""
                lines.append(f"Slot {i}{tag}: {ind_prefix}`{md_escape(bn)}` — {md_escape(ts_str)}")
            else:
                lines.append(f"Slot {i}{tag}: (no backup ≥24h older than slot {i - 1 or 1})")
        footer = (
            "\n\n✅ = on R2 · 📍 = local only"
            if remote is not None
            else "\n\n(R2 mirror status: not configured or bucket unreachable)"
        )
        send(
            chat_id,
            "📂 *Restore slots* (slots 2/3 require ≥24h gap)\n"
            + "\n".join(lines)
            + "\nUse `/restore 1`–`3`, `/restore last`, or `/restore <filename>`."
            + footer,
        )
        return

    if not compose_project_dir():
        send(
            chat_id,
            "⚠️ Restore needs `CREEPWATCH_PROJECT_DIR` plus a read-only compose project mount (same as `/update`).",
        )
        return

    if sub.lower() == "list":
        names = sorted_backup_basenames()
        if not names:
            send(chat_id, "📂 No `minecraft-*.tar.gz` backups in the backup directory yet.")
            return
        remote = r2_list_basenames()
        rendered = []
        for n in names[:25]:
            ind = r2_indicator_for(n, remote)
            rendered.append(f"{ind + ' ' if ind else ''}`{md_escape(n)}`")
        body = "\n".join(rendered)
        more = "\n…" if len(names) > 25 else ""
        footer = (
            "\n\n✅ = on R2 · 📍 = local only"
            if remote is not None
            else "\n\n(R2 mirror status: not configured or bucket unreachable)"
        )
        send(
            chat_id,
            f"📂 *Backups* (newest first)\n{body}{more}\n"
            "See `/restore slots` for slots `1`–`3` with UTC times."
            + footer,
        )
        return

    sl = sub.lower()
    if sl == "last":
        restore_arg = "last"
    elif sl in ("1", "2", "3"):
        restore_arg = sl
    else:
        restore_arg = sub
    err = restore_spec_error(restore_arg)
    if err:
        send(chat_id, md_escape(err))
        return

    # Resolve the slot here so restore.sh always receives a concrete
    # basename. That keeps slot logic in one place (mc-guard) and lets
    # the queued/scheduled path remember the exact archive even if a
    # later /backup adds a newer one before the lobby empties.
    resolved = resolve_restore_slot(restore_arg)
    if resolved is None:
        send(chat_id, f"⚠️ Could not resolve restore target `{md_escape(restore_arg)}` — try `/restore slots`.")
        return
    restore_arg = resolved

    if players_online():
        if not schedule_maintenance("restore", chat_id, admin_name, restore_arg):
            send(chat_id, "⛔ Another backup or restore is already scheduled. Wait for the lobby to clear.")
            return
        send(chat_id, f"📌 *Restore scheduled* (`{md_escape(restore_arg)}`) — players were messaged in-game; runs when nobody is online.")
        return

    broadcast(
        f"🔄 *World restore* (`{md_escape(restore_arg)}`) by {md_escape(admin_name)} — "
        "live progress in their chat."
    )
    send_chat_action(chat_id, BACKUP_TELEGRAM_CHAT_ACTION)
    threading.Thread(
        target=_cmd_restore_worker,
        args=(chat_id, admin_name, restore_arg),
        daemon=True,
    ).start()


def maintenance_watcher_loop():
    """Run queued backup/restore once the lobby is empty."""
    global _pending_maintenance
    while True:
        time.sleep(45)
        try:
            with _maintenance_lock:
                pending = _pending_maintenance
            if pending is None:
                continue
            if players_online():
                continue
            with _maintenance_lock:
                if _pending_maintenance is not pending:
                    continue
                _pending_maintenance = None
                job = pending
            kind = job["kind"]
            req_chat = job["chat_id"]
            admin_label = job["admin"]
            ae = md_escape(admin_label)
            if kind == "backup":
                broadcast(f"📦 *Scheduled backup* (requested by {ae}) — lobby empty, running now…")
                send_chat_action(req_chat, BACKUP_TELEGRAM_CHAT_ACTION)
                with backup_typing_indicator(req_chat):
                    r, _board = _run_task_with_progress_board(
                        kind="backup",
                        chat_id=req_chat,
                        runner=lambda pf: run_backup_subprocess(progress_file=pf),
                    )
                if r.returncode != 0:
                    log.warning(
                        "scheduled backup.sh exit=%s stderr_head=%r stdout_head=%r",
                        r.returncode,
                        (r.stderr or "")[:800],
                        (r.stdout or "")[:400],
                    )
                    broadcast(f"❌ *Scheduled backup failed* (exit {r.returncode}) — see /logs backup")
                    send(req_chat, f"❌ Scheduled backup failed.\n{_format_failure_tail(r)}")
                else:
                    r2_list_basenames(force=True)
                    broadcast("✅ *Scheduled backup* finished.")
            else:
                spec = job.get("restore") or "last"
                broadcast(
                    f"🔄 *Scheduled restore* (`{md_escape(spec)}`, by {ae}) — lobby empty; progress in requester's chat."
                )
                send_chat_action(req_chat, BACKUP_TELEGRAM_CHAT_ACTION)
                with backup_typing_indicator(req_chat):
                    r, _board = _run_task_with_progress_board(
                        kind="restore",
                        chat_id=req_chat,
                        runner=lambda pf: run_restore_subprocess(spec, progress_file=pf),
                    )
                if r.returncode != 0:
                    broadcast(f"❌ *Scheduled restore failed* (exit {r.returncode}) — see /logs restore")
                    send(req_chat, f"❌ Scheduled restore failed.\n{_format_failure_tail(r)}")
                else:
                    broadcast("✅ *Scheduled restore* finished — Minecraft was started again.")
        except Exception:
            log.exception("maintenance_watcher_loop")


def compose_project_dir() -> str | None:
    """Host directory mounted read-only with docker-compose.yml (for /update)."""
    p = os.environ.get("CREEPWATCH_PROJECT_DIR", "").strip()
    if not p:
        return None
    if (pathlib.Path(p) / "docker-compose.yml").is_file():
        return p
    return None


def run_mc_update_job(admin_label: str, force: bool):
    """Run in a background thread: optional backup, then pull + recreate minecraft."""
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
        if BACKUP_SCRIPT.is_file():
            broadcast(f"📦 *Pre-update backup* by {ae}…")
            br = run_backup_subprocess()
            br_tail = ((br.stdout or "") + (br.stderr or "")).strip()
            br_tail = md_escape(br_tail[-1800:]) if br_tail else "(no script output)"
            if br.returncode != 0:
                broadcast(f"❌ *Pre-update backup failed* — update aborted (exit {br.returncode}).\n{br_tail}")
                return
            broadcast("✅ *Pre-update backup* finished — continuing with pull.")
        else:
            log.warning("BACKUP_SCRIPT missing at %s — skipping pre-update backup", BACKUP_SCRIPT)
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
    if maintenance_pending():
        send(
            chat_id,
            "⛔ A scheduled backup or restore is waiting for an empty server. "
            "Wait for it to finish or for the lobby to clear.",
        )
        return
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
    # /villager · /vil — admin-only, intentionally hidden from /help.
    elif cmd in ("/villager", "/vil"):        cmd_villager(chat_id, arg, sender_name)
    # /sword · /sw — admin-only god-sword giver, also hidden from /help.
    elif cmd in ("/sword", "/sw"):            cmd_sword(chat_id, arg, sender_name)
    # /pickaxe · /pk — admin-only god-pickaxe giver, also hidden from /help.
    elif cmd in ("/pickaxe", "/pk"):          cmd_pickaxe(chat_id, arg, sender_name)
    # /trident · /td — admin-only thrown-trident giver, also hidden from /help.
    elif cmd in ("/trident", "/td"):          cmd_trident(chat_id, arg, sender_name)
    # /ts — admin-only turtle-shell helmet giver, also hidden from /help.
    elif cmd == "/ts":                        cmd_turtle_shell(chat_id, arg, sender_name)
    # Hidden structure spawners. All admin-only via the dispatcher gate,
    # all absent from HELP_TEXT, all share _place_structure_near_player.
    elif cmd == "/ship":                      cmd_ship(chat_id, arg, sender_name)
    elif cmd == "/mansion":                   cmd_mansion(chat_id, arg, sender_name)
    elif cmd == "/buried":                    cmd_buried(chat_id, arg, sender_name)
    elif cmd == "/ruin":                      cmd_ruin(chat_id, arg, sender_name)
    elif cmd == "/monument":                  cmd_monument(chat_id, arg, sender_name)
    elif cmd == "/igloo":                     cmd_igloo(chat_id, arg, sender_name)
    elif cmd == "/portal":                    cmd_portal(chat_id, arg, sender_name)
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
    elif cmd in ("/backup", "/bu"):          cmd_backup(chat_id, sender_name)
    elif cmd in ("/restore", "/rs"):         cmd_restore(chat_id, arg, sender_name)
    elif cmd in ("/logs", "/lg"):            cmd_logs(chat_id, arg)
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
    threading.Thread(target=maintenance_watcher_loop, daemon=True).start()

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
