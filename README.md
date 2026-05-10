# creepwatch

A Telegram bot that guards a Minecraft server's whitelist. When a stranger
tries to join, every admin gets a push with **Allow / Deny** buttons. One tap
adds them to the whitelist (or blocks them) over RCON. Each admin tunes their
own alerts via `/settings`.

Pairs with [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server)
in the same Compose stack.

> **Note:** [GitHub `jjziets/creepwatch`](https://github.com/jjziets/creepwatch) has always shipped this README for the public repo. The **live server tree** (`/home/vast/minecraft` on the host) historically had no `README.md` file next to the code; this file is the merged copy so server and repo stay aligned when you push.

## Features

- **Whitelist guard** — non-whitelisted joins fire an Allow / Deny prompt to all admins.
- **Join / leave alerts** — broadcast on player join and leave.
- **Per-admin notification prefs** — `/settings` toggles joins, leaves, approvals, rejects, restarts, errors, and in-game **chat** mirrors independently for each admin.
- **Chat bridge** — admins can send plain text in Telegram and it appears in-game as `[Admin] …` via `tellraw`. In-game `<player> chat` can be mirrored back to Telegram when **Chats** is on.
- **Server admin commands** — manage whitelist, blocklist, online players, recent activity, server status.
- **Update notifications** — broadcasts a message only when the Minecraft server version actually changes (silent on routine restarts).
- **Log error paging** — `ERROR` lines are classified; known noisy patterns are suppressed; real issues go to Telegram with a cooldown per signature.
- **Block list** — denied players land in a persistent blocklist so subsequent join attempts are silently ignored.

## Commands

Long form and short alias both work.

| Long | Short | What it does |
|------|-------|---------------|
| `/help` | `/h` | Show command list |
| `/whitelist` | `/wl` | List whitelisted players |
| `/approve <player>` | `/a` | Add player to whitelist |
| `/remove <player>` | `/rm` | Remove player from whitelist |
| `/blocked` | `/bl` | List denied players |
| `/unblock <player>` | `/ub` | Remove player from blocklist |
| `/online` | `/ol` | Who is online right now |
| `/activity` | `/ac` | Last 20 join / leave / disconnect events |
| `/status` | `/st` | Server version and player count |
| `/settings` | `/se` | Toggle your own notification categories |

## Quick start

1. Create a bot via [@BotFather](https://t.me/BotFather) and grab the token.
2. Get your Telegram chat ID by messaging [@userinfobot](https://t.me/userinfobot).
3. Clone and configure:
   ```sh
   git clone https://github.com/jjziets/creepwatch.git
   cd creepwatch
   cp .env.example .env
   $EDITOR .env   # paste your bot token and chat IDs
   ```
4. Start the stack:
   ```sh
   docker compose up -d
   ```
5. Message your bot `/help` from each admin account.

The bot needs the Docker socket and `docker` binary mounted because it tails the
`minecraft` container's logs and calls `rcon-cli` inside it. That's how it
detects whitelist rejections, joins, leaves, chat, and `ERROR` lines, and how it
manages the whitelist and the admin chat bridge.

## How it works

- **Log tailing** — `docker logs -f minecraft` is parsed for whitelist rejections, joins, leaves, in-game chat, server ready/stopping lines, and `ERROR` lines.
- **RCON** — whitelist add / remove, `list`, `version`, and `tellraw` for the admin chat bridge go through `docker exec minecraft rcon-cli`.
- **State** — denied players in `data/blocked_players.txt`, per-admin prefs in `data/notify_prefs.json`, last reported MC version in `data/mc_last_version`. These survive container recreates because `./data` is a host bind mount into `mc-guard`.

## Notification model

Every notification type is a per-admin preference. By default everything is on.

| Pref | Fires on |
|------|----------|
| `joins` | Player joined the game |
| `leaves` | Player left the game |
| `approvals` | Allow / approve / unblock results from another admin |
| `rejects` | Deny / remove results from another admin |
| `restarts` | Server ready / stopping messages from logs |
| `errors` | Classified `ERROR` lines that are not suppressed noise |
| `chats` | In-game chat lines forwarded to Telegram |

The Allow / Deny prompt for **new** join requests always goes to every admin —
it requires action, so it isn't gated. Whoever runs `/approve`, `/remove`, or
`/unblock` always gets a confirmation in their own chat regardless of their own
prefs.

## Chat bridge

- **Telegram → Minecraft:** From an authorised admin chat, send any message that does **not** start with `/`. It is broadcast to all online players as `[Admin] FirstName: text` using `tellraw`.
- **Minecraft → Telegram:** Lines matching in-game chat are formatted and sent to admins who have **Chats** enabled in `/settings`.

## Server log errors

`mc_guard.classify_error()` decides whether an `ERROR` line should page admins.
Known noisy patterns (for example certain worldgen far-chunk warnings and
specific disconnect packet errors) are logged locally only. Everything else is
subject to a per-signature cooldown before Telegram sees it.

Unit tests live in `test_mc_guard_classifier.py`:

```sh
python3 -m unittest discover -v -s . -p 'test_*.py'
```

## Auto-update protection

The Watchtower service in the bundled Compose stack pulls a fresh
`itzg/minecraft-server:latest` on a cron schedule (by default hourly from **01:00 to 08:00** —
several chances per night to land on an empty lobby). Set `TZ` in `.env` to make
that window match your wall clock; defaults to UTC.

Each attempt runs a Watchtower **pre-update lifecycle hook**
(`scripts/pre-update-check.sh`) that calls `rcon-cli list` inside the
minecraft container. If anyone is online the script exits non-zero and
Watchtower skips this cycle — the running server is never restarted out
from under players. Admins can get one Telegram heads-up when a skip is
notified; subsequent skip attempts within **12 hours** can stay silent thanks to
a marker file **`.creepwatch_last_skip_notify`** on the Minecraft data volume.

The first idle window wins: as soon as the lobby is empty the update
proceeds, creepwatch broadcasts the routine stopping / ready messages when those
log lines appear, and the version-change detector adds a **Minecraft updated** line if
a new MC version landed.

## Backups

`scripts/backup.sh` produces a nightly tarball of the Minecraft world without
kicking players. The flow is the standard quiesce-and-snapshot pattern:

1. `rcon save-all flush` — flush pending writes.
2. `rcon save-off` — pause autosave.
3. Tar the `minecraft_data` volume from an ephemeral `alpine:latest` container.
4. `rcon save-on` — resume autosave (always runs, even if the snapshot fails).

By default tarballs land under `backups/` relative to the compose project (the
production host uses `/home/vast/minecraft/backups/`). Old archives are pruned after **7 days**
(override via `BACKUP_RETENTION_DAYS`). Failures are reported to Telegram via the same
`TELEGRAM_BOT_TOKEN` / `ADMIN_CHAT_IDS` used by mc-guard; successful backups stay silent.

Optional systemd units ship under `systemd/` — see that directory for
`minecraft-backup.service` and `minecraft-backup.timer`. Install once:

```sh
sudo cp systemd/minecraft-backup.service systemd/minecraft-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now minecraft-backup.timer
```

Push backups off-host (S3, Backblaze, rsync to another box, …) by extending the
script after the successful snapshot step.

## Repository layout

```
docker-compose.yml
mc_guard.py
scripts/
  pre-update-check.sh
  backup.sh
data/                      # bind-mounted into mc-guard (gitignored contents)
systemd/
test_mc_guard_classifier.py
geyser/                    # optional Bedrock bridge
```

Keep **`.env`**, world data, `backups/`, and runtime files out of git.

## License

MIT
