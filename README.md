# creepwatch

A Telegram bot that guards a Minecraft server's whitelist. When a stranger
tries to join, every admin gets a push with **Allow / Deny** buttons. One tap
adds them to the whitelist (or blocks them) over RCON. Each admin tunes their
own alerts via `/settings`.

**Two-way chat:** authorised admins can talk to everyone in-game from Telegram
(plain messages are relayed as gold `[Admin]` lines via `tellraw @a`). Player
chat from the server logs is mirrored back to Telegram as `💬` lines for admins
who enable **Chats** in `/settings`.

Pairs with [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server)
in the same Compose stack.

> **Note:** [GitHub `jjziets/creepwatch`](https://github.com/jjziets/creepwatch) has always shipped this README for the public repo. The **live server tree** (`/home/vast/minecraft` on the host) historically had no `README.md` file next to the code; this file is the merged copy so server and repo stay aligned when you push.

## Features

- **Whitelist guard** — non-whitelisted joins fire an Allow / Deny prompt to all admins.
- **Join / leave alerts** — broadcast on player join and leave.
- **Per-admin notification prefs** — `/settings` toggles joins, leaves, approvals, rejects, restarts, errors, and in-game **chat** mirrors independently for each admin.
- **Chat bridge** — admins can send plain text in Telegram and it appears in-game as `[Admin] …` via `tellraw`. In-game `<player> chat` can be mirrored back to Telegram when **Chats** is on.
- **Server admin commands** — whitelist, blocklist, kick/ban/pardon, direct `msg` to one player, `whitelist reload`, time/weather/difficulty/gamerule, online/activity/status, optional `/update`.
- **Update notifications** — broadcasts a message only when the Minecraft server version actually changes (silent on routine restarts).
- **Log error paging** — `ERROR` lines are classified; known noisy patterns are suppressed; real issues go to Telegram with a cooldown per signature.
- **Block list** — denied players land in a persistent blocklist so subsequent join attempts are silently ignored.
- **Manual `/update`** — optional pull + recreate of the `minecraft` service from Telegram when the compose project is mounted into mc-guard (see below).
- **Log-tail resilience** — mc-guard reconnects to `docker logs` after a Minecraft container restart instead of exiting.
- **Skip escalation** — Watchtower skip streak is persisted; long runs of “players always online” escalate the wording of the heads-up message.
- **Heartbeat file** — mc-guard refreshes `/data/.creepwatch_heartbeat` for host-side stale detection.
- **Restricted Docker CLI** — inside `mc-guard`, `docker` is a wrapper that only allows `exec minecraft rcon-cli`, `logs … minecraft`, and `compose … pull|up` for the `minecraft` service (see `bin/docker-mc-guard.sh`).

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
| `/kick <player>` | `/k` | Disconnect a player (optional kick message) |
| `/msg <player> <msg>` | `/tell` | Whisper one player from the server (RCON `msg`) |
| `/wlreload` | `/wlr` | `whitelist reload` after editing whitelist on disk |
| `/ban <player>` | `/bn` | Ban (optional reason) |
| `/banip <target>` | `/bi` | Ban IP / pattern (validated) |
| `/pardon <player>` | `/pd` | Unban name |
| `/pardonip <target>` | `/pdi` | Unban IP pattern |
| `/time …` | — | `query daytime|gametime|day` or `set day|night|noon|midnight|<ticks>` |
| `/weather …` | — | `clear` / `rain` / `thunder` [seconds] |
| `/difficulty …` | `/diff` | `peaceful` / `easy` / `normal` / `hard` |
| `/gamerule …` | `/gr` | Query one rule, or set to `true` / `false` / digits |
| `/settings` | `/se` | Toggle your own notification categories |
| `/update` | `/up` | Pull latest MC image and recreate `minecraft` (optional; see README) |

## Security

- **Admins only in private chat** — The bot ignores every update except private DMs where the sender’s Telegram **user id** is listed in `ADMIN_CHAT_IDS`. Commands, the Minecraft chat bridge, and Allow / Deny buttons are not accepted from groups, channels, or strangers (including no reply to random `/start` spam). In BotFather, leave **Allow groups** off so the bot cannot be added to chats you do not control.
- **Configure user ids, not groups** — Use the positive id from [@userinfobot](https://t.me/userinfobot) in a **private** chat with yourself. Negative ids are groups/supergroups; the bot refuses them at startup.
- **Docker from mc-guard** — Compose mounts `bin/docker-mc-guard.sh` as `/usr/local/bin/docker` ahead of the real binary (`docker.real`). Only **`docker exec minecraft rcon-cli`**, **`docker logs` … `minecraft` (name last)**, and **`docker compose` … `pull minecraft` / `up -d --no-deps minecraft`** reach the host Docker CLI. RCON stays local to the host via that single `exec` path. The **Docker socket** is still high-trust: malicious code inside the container could talk to it directly without the `docker` binary—keep the image and `mc_guard.py` supply chain trusted.
- **Secrets** — Treat `TELEGRAM_BOT_TOKEN` and host `.env` like production credentials; rotate the bot token if it leaks.
- **Attack surface** — Anyone who can Telegram as an admin or SSH the host can trigger the same RCON/compose paths the bot uses.

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

The bot needs the Docker socket and a **restricted** `docker` entrypoint (see
`bin/docker-mc-guard.sh`) so it can tail the `minecraft` container's logs and run
`rcon-cli` inside that container only.

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

The bot watches `docker logs` for vanilla-style chat lines (`]: <player> message`)
and sends Telegram updates through the same notification pipeline as joins and
errors. RCON runs `tellraw @a …` so bridged admin text appears for every player
who can see action bar / chat output.

### Telegram → Minecraft (admin → server)

- Only chats listed in `ADMIN_CHAT_IDS` can send messages that reach the server.
- **Text messages only** (Telegram updates with `text`). Stickers, photos, and
  other payload types are ignored for bridging.
- Anything that **starts with `/`** is treated as a bot command, not in-game
  chat — use `/help`, `/online`, etc. as usual.
- Plain text is normalised to a single line, trimmed to about **800 characters**,
  then sent with `tellraw`: gold bold `[Admin]`, yellow admin first name (up to
  ~40 chars of label), white message body.
- If `rcon-cli` fails, you get a short **⚠️** reply in your Telegram chat with
  the error text (Markdown-escaped where needed).

### Minecraft → Telegram (server → admins)

- Player chat is detected from log lines matching `]: <player> remainder`.
- Admins who turned **Chats** *off* in `/settings` do not receive these mirrors;
  everyone still gets mandatory prompts (e.g. whitelist Allow / Deny) as before.
- Telegram messages use legacy Markdown. Player names and message bodies are
  escaped so Markdown metacharacters in chat (underscore, asterisk, backtick,
  square brackets) cannot break the message. Long lines are compacted and
  capped for readability (~800 chars of content).

### Quick checks

1. From an admin account, run `/settings` and ensure **Chats** is on (green).
2. Say something in-game — you should see `💬 *Player*: …` in Telegram.
3. Reply in Telegram with a line that does **not** start with `/` — players
   should see `[Admin] YourName: …` in Minecraft.

## Server log errors

`mc_guard.classify_error()` decides whether an `ERROR` line should page admins.
Known noisy patterns (for example certain worldgen far-chunk warnings and
specific disconnect packet errors) are logged locally only. Everything else is
subject to a per-signature cooldown before Telegram sees it.

Unit tests live in `test_mc_guard_classifier.py`:

```sh
pip install requests   # once, for local runs
python3 -m unittest discover -v -s . -p 'test_*.py'
```

GitHub Actions runs the same tests plus `ruff` and `shellcheck` on every push
to `main` / `dev` (see `.github/workflows/lint.yml`).

### Deploy to your server on merge to `main`

After **lint passes** on a **push to `main`**, the same workflow can SSH to your
host, `git pull` the repo, and run **`docker compose up -d --no-deps mc-guard`**
only — the **minecraft** container is not recreated.

1. **On the server** (once): clone this repo to the directory where you run
   Compose (e.g. `/home/vast/minecraft`), install Docker Compose v2, and ensure
   `git pull` works (deploy key or HTTPS credentials for GitHub).
2. **On the server**: add the **public** half of a dedicated SSH key to
   `~/.ssh/authorized_keys` for the account that will run deploy (often `root`).
3. **In GitHub** → repository **Settings → Secrets and variables → Actions**,
   add:

   | Secret | Example |
   |--------|---------|
   | `DEPLOY_HOST` | `41.193.204.66` |
   | `DEPLOY_USER` | `root` |
   | `DEPLOY_SSH_KEY` | Full private key PEM (the pair from step 2) |
   | `DEPLOY_PATH` | Absolute path to the compose project on the server |
   | `DEPLOY_PORT` | Optional; default **22**. Set if SSH listens on another port. |

If `DEPLOY_HOST` is **not** set, the workflow still passes: the SSH deploy steps
are skipped (so forks and local clones do not fail CI).

Manual equivalent on the host:

```sh
./scripts/deploy-mc-guard.sh /path/to/compose-project
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

If skips happen on **different calendar days** in a row, a counter in
**`.creepwatch_skip_streak`** (on the same volume) increments once per day. The
Telegram text escalates after **3** and **7** consecutive skip days so a stale
server version is not silent forever. A successful Minecraft version change
(resetting that streak) is detected by mc-guard when it writes `mc_last_version`.

The first idle window wins: as soon as the lobby is empty the update
proceeds, creepwatch broadcasts the routine stopping / ready messages when those
log lines appear, and the version-change detector adds a **Minecraft updated** line if
a new MC version landed.

## Safe compose deploys

`docker compose up -d <service>` can still recreate **other** services when their
declared config drifted, because Compose re-evaluates the whole project. That can
restart `minecraft` when you only meant to roll `mc-guard`.

Use **`bin/safe-deploy.sh`** from the compose project directory:

```sh
chmod +x bin/safe-deploy.sh
./bin/safe-deploy.sh mc-guard
```

It checks `rcon-cli list` for online players (unless `--force`) and runs
`docker compose up -d --no-deps <service>` so dependency drift does not cascade.

## Manual `/update` from Telegram

Admins can run `/update` or `/up` (and `/update force` to override the online
player check). The bot runs `docker compose pull minecraft` then
`docker compose up -d --no-deps minecraft` using **`CREEPWATCH_PROJECT_DIR`**
as the host directory that contains `docker-compose.yml`.

1. Set `CREEPWATCH_PROJECT_DIR` in `.env` to that absolute path (same directory
   you run Compose from on the host).
2. Add a **read-only** bind mount on `mc-guard` so the same files exist inside
   the container, for example `- /home/you/minecraft:/project:ro` and set
   `CREEPWATCH_PROJECT_DIR=/project`.

Without the mount + env var, `/update` replies that it is not configured.

## Liveness heartbeat

Every **600** seconds (override with `CREEPWATCH_HEARTBEAT_SEC`) mc-guard writes a
Unix timestamp to **`/data/.creepwatch_heartbeat`** on the mc-guard bind mount.
A host cron or external monitor can alert if the file is older than ~30 minutes
(useful when Telegram itself is the broken channel).

**Host check script:** `scripts/check-creepwatch-heartbeat.sh` exits **1** and prints a
clear line to stderr when the timestamp file is missing or older than **1800** seconds
(override with a second argument). Point it at the host path of `.creepwatch_heartbeat`
next to your compose `data/` directory, then wire it into cron or systemd.

**External ping (optional):** set **`CREEPWATCH_HEALTHCHECK_URL`** (e.g. a
[Healthchecks.io](https://healthchecks.io/) ping URL) so each heartbeat also issues an
HTTP GET. If Telegram is down but the process is alive, the file still updates; if the
bot is wedged, neither the file nor the external ping advances — use the URL as a
backup notification path (email/SMS from the provider) independent of Telegram.

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
bin/
  safe-deploy.sh
  docker-mc-guard.sh
scripts/
  pre-update-check.sh
  backup.sh
  check-creepwatch-heartbeat.sh
  deploy-mc-guard.sh
data/                      # bind-mounted into mc-guard (gitignored contents)
systemd/
test_mc_guard_classifier.py
geyser/                    # optional Bedrock bridge
.github/workflows/
  lint.yml
```

Keep **`.env`**, world data, `backups/`, and runtime files out of git.

## License

MIT
