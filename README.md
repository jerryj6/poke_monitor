# Poke.com Combined Monitor & Changelog Bot 🌴

A production-grade, Render-compatible unified service that combines feature flag monitoring, credit usage warnings, and a Discord changelog announcement forwarder into a single Python application using `discord.py`.

---

## Deployment Architecture

The service runs as a concurrent application:
1. **Discord Bot (`discord.py`)**: Runs on the main thread, maintaining a persistent WebSocket connection to the Discord Gateway to receive events (like webhook announcements) and register/respond to Slash Commands.
2. **Flask Web Server**: Binds to `0.0.0.0` at the port specified in `PORT` (default `10000`). Exposes endpoints `/` and `/health` for uptime monitors (e.g. UptimeRobot) to query.
3. **Background Tasks**: Managed via `discord.ext.tasks` running non-blocking intervals on the main event loop, utilizing `asyncio.to_thread` for synchronous network fetches.

---

## Combined Features

### 1. Changelog Webhook Forwarding (Optional)
If `SOURCE_CHANNEL_ID` is configured:
- The bot listens for incoming webhook messages in the source channel.
- It automatically forwards the content, embeds, and file attachments to `DESTINATION_CHANNEL_ID` (which defaults to the source channel if omitted).
- Upon successful forwarding, it deletes the original webhook message.
- Can be toggled on/off interactively using `/changelog <enable|disable>` (persisted in state).

### 2. Interactive Slash Commands
- `/balance`: Instantly queries the Poke API and replies with the current weekly usage percentage and progress bar.
- `/flags`: Instantly checks for Poke feature flag updates, publishes changes to your flag webhook channel, and replies with a status summary.
- `/changelog <enable|disable>`: Toggles the forwarding feature.

### 3. Background Monitoring
- Checks feature flags every 5 minutes (`FLAGS_CHECK_INTERVAL_SECONDS=300`).
- Samples weekly credit usage every 1 minute (`USAGE_SAMPLE_INTERVAL_SECONDS=60`).
- Publishes routine balance embeds every 15 minutes (`USAGE_NOTIFICATION_INTERVAL_SECONDS=900`).

---

## Velocity Warnings & Cooldowns

The monitor calculates weekly usedPercent rate changes (velocity in percentage points per minute) over a rolling 10-minute window.
- **Warning Title**: `⚠️ Poke Usage Rising Quickly`
- **Rate Threshold**: `0.125 pp/min` (approx +7.5 points/hour).
- **Minimum Delta**: `1.25` percentage points.
- **Cooldown**: 15 minutes (`USAGE_WARNING_COOLDOWN_SECONDS=900`).
- **Re-alert Rules**: Inside an active warning state, a new alert is sent after cooldown only if usage has increased by at least `2.00` points or velocity is `1.5` times the previous warned rate.
- **Replenishment Reset**: Drops > `0.25` points are treated as daily replenishment, resetting the velocity slice and clearing the warning state.

---

## Environment Variables

| Name | Description | Default |
| :--- | :--- | :--- |
| `DISCORD_BOT_TOKEN` | Discord Bot Token | *Required* |
| `DISCORD_STATE_CHANNEL_ID` | Private channel ID for storing state.json | *Required* |
| `POKE_SESSION_TOKEN` | Poke session token Cookie value | *Required* |
| `POKE_FLAGS_PAYLOAD` | Raw request body string for flag checks | *Required* |
| `FLAGS_WEBHOOK_URL` | Discord webhook URL for flag updates | *Required* |
| `CREDIT_WEBHOOK_URL` | Discord webhook URL for balance alerts | *Required* |
| `SOURCE_CHANNEL_ID` | Optional. Channel ID to intercept webhook messages | *None* |
| `DESTINATION_CHANNEL_ID` | Optional. Channel ID to forward messages to | *None* |
| `PORT` | Flask web server port | `10000` |

---

## Discord Bot Permissions

Configure your Bot in the Discord Developer Portal with the **Bot** tab permissions:
- **View Channel**
- **Read Message History**
- **Send Messages**
- **Attach Files**
- **Manage Messages** *(required to delete original webhook announcements)*
- **Message Content Intent** *(Must be enabled under "Privileged Gateway Intents" on the developer portal)*
