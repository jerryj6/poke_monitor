# Poke Monitor

A self-hosted Discord bot that watches Poke feature flags and account usage, sends usage alerts, and optionally forwards webhook announcements. It runs a Discord client, scheduled checks, and a small HTTP health server in one Python process.

This is an unofficial project, unaffiliated with Poke or Discord. It uses Poke web endpoints that may change without notice. A working Poke account session and your own Discord bot and webhooks are required.

## Features

- Feature flag comparisons every five minutes, with changes sent to a Discord webhook.
- Usage samples every minute and routine balance notifications every 15 minutes.
- Warnings for sustained increases in usage, with cooldowns and replenishment detection.
- `/balance` to display usage, `/flags` to check for changes, and `/changelog` to toggle forwarding.
- Optional forwarding of webhook announcements, including embeds and attachments. After a successful forward, the original message is deleted.
- State stored in a private Discord channel, with a local JSON fallback.

## Quick start

Use Python 3.12 or 3.13.

```sh
git clone https://github.com/jerryj6/poke_monitor.git
cd poke_monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with your own values. On macOS or Linux, load it into the process environment and start the service:

```sh
set -a
source .env
set +a
python poke_monitor.py
```

The app reads environment variables; it does not load `.env` or `config.json` itself. Keep values shell-quoted when using the commands above. Hosted deployments should use the host's secret environment settings.

### Discord setup

1. Create a Discord application and bot. Enable **Message Content Intent** in the Developer Portal.
2. Invite the bot with the `bot` and `applications.commands` scopes.
3. Grant **View Channel**, **Read Message History**, **Send Messages**, **Embed Links**, and **Attach Files** in the channels it uses. Forwarding also needs **Manage Messages** in the source channel to delete originals.
4. Create a private state channel and two webhooks for flag and usage notifications. Restrict access to the channels and bot commands to trusted users.

Slash command replies are visible in the channel. Anyone allowed to invoke the commands can query the configured account or toggle forwarding; the app does not implement its own per-user authorization. Use a dedicated bot and Discord's command permissions.

### Required configuration

| Variable | Value |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Your bot token. |
| `DISCORD_STATE_CHANNEL_ID` | Numeric ID of the private state channel. |
| `POKE_SESSION_TOKEN` | Value of your own `__Secure-poke_production.session_token` cookie. |
| `POKE_FLAGS_PAYLOAD` | Decoded value of the `data` form field from your own Poke flags request. The app adds `data=` and URL-encodes the value itself. |
| `FLAGS_WEBHOOK_URL` | Webhook URL for feature flag updates. |
| `CREDIT_WEBHOOK_URL` | Webhook URL for usage notifications. |

Obtain the cookie and flags payload from your own authenticated browser session. Treat both as sensitive: payloads can contain account or device identifiers. Do not commit browser exports, cookies, payloads, state files, or logs. Update the session token in the environment and restart when it expires.

### Optional configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SOURCE_CHANNEL_ID` | Unset | Enables listening for webhook announcements in this channel. |
| `DESTINATION_CHANNEL_ID` | Source channel | Destination for forwarded announcements. |
| `PORT` | `10000` | HTTP server port. |
| `LOCAL_STATE_PATH` | `state.json` | Local fallback state file; keep it outside tracked files. |
| `STATE_MARKER` | `POKE_MONITOR_STATE_V2` | Marker used to locate Discord state messages. |
| `POKE_CLIENT_VERSION` | `1.360.2` | Version parameter sent to the flags endpoint; may need updating. |
| `FLAGS_CHECK_INTERVAL_SECONDS` | `300` | Feature flag check interval. |
| `USAGE_SAMPLE_INTERVAL_SECONDS` | `60` | Usage sampling interval. |
| `USAGE_NOTIFICATION_INTERVAL_SECONDS` | `900` | Routine usage notification interval. |
| `USAGE_WARNING_COOLDOWN_SECONDS` | `900` | Minimum time between usage warnings. |
| `USAGE_RECENT_WINDOW_SECONDS` | `600` | Rolling velocity window. |
| `USAGE_MIN_VELOCITY_WINDOW_SECONDS` | `480` | Minimum observation span before a velocity warning. |
| `USAGE_WARNING_VELOCITY_PP_PER_MINUTE` | `0.125` | Warning threshold in percentage points per minute. |
| `USAGE_WARNING_MIN_DELTA_PP` | `1.25` | Minimum usage increase for a warning. |
| `USAGE_WARNING_REALERT_DELTA_PP` | `2.00` | Additional usage needed to re-alert after cooldown. |
| `USAGE_WARNING_REALERT_VELOCITY_RATIO` | `1.50` | Alternative re-alert trigger relative to the last warned rate. |
| `USAGE_RESET_DROP_TOLERANCE_PP` | `0.25` | Usage drop treated as replenishment. |
| `USAGE_HISTORY_MAX_SAMPLES` | `120` | Maximum stored usage samples. |
| `USAGE_HISTORY_MAX_AGE_SECONDS` | `7200` | Maximum sample age. |
| `USAGE_HISTORY_CHECKPOINT_SECONDS` | `900` | Reserved setting; currently no separate checkpoint task uses it. |
| `SUPPRESS_ROUTINE_AFTER_WARNING_SECONDS` | `60` | Delay routine notifications after a warning. |
| `USAGE_VELOCITY_DEBUG` | `false` | Log velocity diagnostics; keep logs private. |

## Hosting

For a Render Python web service, use:

- Build command: `pip install -r requirements.txt`
- Start command: `python poke_monitor.py`
- Health check path: `/health`
- Python version: a supported 3.12 or 3.13 release

Configure the required environment values through the hosting dashboard. Run one instance: multiple instances can duplicate alerts and compete over state. Start the Python entry point directly; importing the Flask app through a WSGI server does not start the Discord bot or monitoring loops.

`/` returns a service identifier. `/health` returns only service availability, monitor status, and Discord connectivity; it omits account usage, bot identity, and raw errors. These endpoints return HTTP 200 when the web server responds, so they are liveness checks, not proof of successful API checks. The web server starts after state recovery; if Discord is unavailable and there is no local state, startup waits for recovery.

The HTTP endpoint uses Flask's built-in server and is intended for lightweight health checks behind a hosting proxy. No live deployment or current Poke endpoint compatibility is guaranteed by the unit tests.

## State and privacy

The private Discord state attachment contains flag snapshots and usage history. A local copy is written to `LOCAL_STATE_PATH`. The bot reads recent state messages at startup and replaces its prior canonical state message after saving. Keep the state channel and any backups private, and use a separate state channel per instance.

Notifications intentionally share usage and flag data with the configured Discord channels. Logs may contain API errors, channel identifiers, or attachment URLs; do not publish logs without reviewing them. `.gitignore` excludes common local secrets, state, logs, and virtual environments, but review your staged diff before committing.

## Development

```sh
python -m unittest discover -v
```

Tests use mock HTTP calls and temporary state files; no live credentials are needed. GitHub Actions runs the suite on Python 3.12 and 3.13. Coverage includes state migration/recovery, notification splitting, usage warning behavior, expired-token handling, and health endpoint privacy.

When contributing, include a focused description and relevant tests. Use synthetic data in fixtures and issue reports.
