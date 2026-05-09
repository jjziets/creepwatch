# creepwatch

A Telegram bot that guards a Minecraft server's whitelist. When a stranger
tries to join, every admin gets a push with **Allow / Deny** buttons. One tap
adds them to the whitelist (or blocks them) over RCON. Each admin tunes their
own join, leave, approval, and rejection alerts via `/settings`.

Pairs with [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server)
in the same Compose stack.

## Features

- 🛡️ **Whitelist guard** — non-whitelisted joins fire an Allow / Deny prompt to all admins.
- 🟢 **Join / leave alerts** — broadcast on player join and leave.
- 🔕 **Per-admin notification prefs** — `/settings` toggles joins, leaves, approvals, rejects independently for each admin.
- 🎮 **Server admin commands** — manage whitelist, blocklist, online players, recent activity, server status.
- 🆙 **Update notifications** — broadcasts a message only when the Minecraft server version actually changes (silent on routine restarts).
- ✋ **Block list** — denied players land in a persistent blocklist so subsequent join attempts are silently ignored.

## Commands

Long form and short alias both work.

| Long           | Short  | What it does                                  |
|----------------|--------|-----------------------------------------------|
| `/help`        | `/h`   | Show command list                             |
| `/whitelist`   | `/wl`  | List whitelisted players                      |
| `/approve <p>` | `/a`   | Add player to whitelist                       |
| `/remove <p>`  | `/rm`  | Remove player from whitelist                  |
| `/blocked`     | `/bl`  | List denied players                           |
| `/unblock <p>` | `/ub`  | Remove player from blocklist                  |
| `/online`      | `/ol`  | Who is online right now                       |
| `/activity`    | `/ac`  | Last 20 join / leave / disconnect events      |
| `/status`      | `/st`  | Server version and player count               |
| `/settings`    | `/se`  | Toggle your own joins / leaves / approvals / rejects |

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
detects whitelist rejections, joins, and leaves and how it manages the
whitelist.

## How it works

- **Log tailing** — `docker logs -f minecraft` is parsed for whitelist
  rejections, joins, and leaves.
- **RCON** — whitelist add / remove, `list`, `version` go through
  `docker exec minecraft rcon-cli`.
- **State** — denied players in `data/blocked_players.txt`, per-admin prefs in
  `data/notify_prefs.json`. Both survive container recreates because `./data`
  is a host bind mount.

## Notification model

Every notification type is a per-admin preference. By default everything is on.

| Pref       | Fires on                                                    |
|------------|-------------------------------------------------------------|
| `joins`    | `🟢 <player> joined the game.`                              |
| `leaves`   | `⚪ <player> left the game.`                                |
| `approvals`| `✅` allow / approve / unblock results from the other admin |
| `rejects`  | `❌` deny / remove results from the other admin             |

The Allow / Deny prompt for **new** join requests always goes to every admin —
it requires action, so it isn't gated. Whoever runs `/approve`, `/remove`, or
`/unblock` always gets a confirmation in their own chat regardless of their own
prefs.

## Auto-update protection

The Watchtower service in the bundled Compose stack pulls a fresh
`itzg/minecraft-server:latest` every night at 02:00 UTC. Updates are gated by
a Watchtower **pre-update lifecycle hook**
(`scripts/pre-update-check.sh`) that runs `rcon-cli list` inside the minecraft
container — if any player is online, the script exits non-zero and Watchtower
skips the cycle, then notifies both admins via Telegram:

```
🟡 Minecraft auto-update postponed: 2 player(s) online (Steve, Alex). Will retry tomorrow at 2am.
```

When the lobby is empty, the update proceeds normally and creepwatch's own
version-change detection broadcasts `🆙 Minecraft updated! X → Y`.

## License

MIT
