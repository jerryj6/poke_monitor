# Poke.com Monitor 🌴

A production-grade, Render-compatible backend application that monitors feature flags and API credit consumption for Poke.com. It is designed to run persistently in the cloud, handle macOS-free environments, and maintain authoritative, permanent state storage inside a private Discord channel.

---

## Deployment Architecture

The service runs as a concurrent Python application:
1. **Flask Web Server**: Binds to `0.0.0.0` at the port specified in the `PORT` environment variable (default `10000`). It exposes health checks (`/` and `/health`) for uptime monitoring (e.g., UptimeRobot).
2. **Scheduler Thread**: An internal loop running in a background daemon thread that controls periodic execution tasks using independent monotonic deadlines.

---

## State Persistence Strategy

This application does not rely on local persistent disks, which are transient on free-tier cloud platforms like Render. It uses three layers of state:

1. **Discord State Message (Authoritative, Permanent)**:
   - Stored as a message in a private Discord channel matching the `STATE_MARKER` (default: `POKE_MONITOR_STATE_V2`).
   - The message contains a file attachment `state.json`.
   - On startup, the application queries Discord channel history, downloads the attachment, validates its structure, and populates the in-memory state.
   - Updates are crash-safe: a new message with the attachment is uploaded, and upon successful upload confirmation, the old message is deleted.
2. **In-Memory State (Active)**:
   - Held in memory during runtime to prevent excessive Discord API calls.
3. **Local State Cache (Temporary)**:
   - Written atomically using `tempfile + os.replace` to `state.json`.
   - Used only as a fallback if Discord is temporarily unreachable at startup.

---

## Periodic Schedules

- **Poke Feature Flags**: Sampled every 5 minutes (`FLAGS_CHECK_INTERVAL_SECONDS=300`).
- **Weekly Credit Usage**: Sampled every 1 minute (`USAGE_SAMPLE_INTERVAL_SECONDS=60`).
- **Routine Balance Embed**: Sent to Discord credit webhook every 15 minutes (`USAGE_NOTIFICATION_INTERVAL_SECONDS=900`). To prevent duplicate HTTP requests, it is processed during the 1-minute usage sampling task.

---

## Velocity Warnings & Cooldown

The monitor calculates usage velocity (in percentage points per minute) over a rolling 10-minute window (`USAGE_RECENT_WINDOW_SECONDS=600`).

### Warning Thresholds
Exactly **one warning type** is sent to the credit webhook when weekly usage increases rapidly:
- **Title**: `⚠️ Poke Usage Rising Quickly`
- **Rate Threshold**: `0.125 pp/min` (equivalent to 7.5 percentage points per hour).
- **Minimum Delta**: `1.25` percentage points increase over the window.
- **Window Limit**: A minimum of 8 minutes (`USAGE_MIN_VELOCITY_WINDOW_SECONDS=480`) of same-period history is required.

### Deduplication and Cooldown
- **Cooldown**: A warning has a cooldown of 15 minutes (`USAGE_WARNING_COOLDOWN_SECONDS=900`).
- **Re-alert Rules**: Inside the active warning state, a new alert is allowed after the cooldown only if:
  1. The warning condition became inactive (detected after 3 consecutive below-threshold samples) and later crossed the threshold again.
  2. Weekly usage increased by at least `2.00` percentage points (`USAGE_WARNING_REALERT_DELTA_PP`) since the last warned value.
  3. Current velocity is at least `1.5` times (`USAGE_WARNING_REALERT_VELOCITY_RATIO`) the last warned velocity.

### Daily Replenishment
Poke usage drops by approximately 14–15 percentage points daily during normal replenishment.
- **Reset Tolerance**: Any drop greater than `0.25` percentage points (`USAGE_RESET_DROP_TOLERANCE_PP`) is treated as normal replenishment.
- **Action**: When detected, the application clears the velocity history slice, resets the active warning flag, and updates the billing cycle reset timestamp without triggering warnings.

---

## Token Expiration Handling

If the Poke API responds with `401` or `403` status codes:
1. The session token is treated as expired.
2. A single alert is sent to the credit webhook telling the user to update `POKE_SESSION_TOKEN`.
3. The app continues feature-flag checks normally.
4. Once a request succeeds again (HTTP `200`), the alert state is cleared and a recovery message is sent.

---

## Render Deployment Settings

Configure the service on Render with the following settings:
- **Runtime**: `Python`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `python poke_monitor.py`

### Environment Variables
| Name | Description | Default Value |
| :--- | :--- | :--- |
| `DISCORD_BOT_TOKEN` | Discord Bot Token | *Required* |
| `DISCORD_STATE_CHANNEL_ID` | Private channel ID for state storage | *Required* |
| `POKE_SESSION_TOKEN` | Poke session token Cookie value | *Required* |
| `POKE_FLAGS_PAYLOAD` | Raw request body string for flag checks | *Required* |
| `FLAGS_WEBHOOK_URL` | Discord webhook URL for flag updates | *Required* |
| `CREDIT_WEBHOOK_URL` | Discord webhook URL for balance warnings | *Required* |
| `PORT` | Flask web server port | `10000` |
| `POKE_CLIENT_VERSION` | Poke frontend client version string | `1.360.2` |
| `FLAGS_CHECK_INTERVAL_SECONDS` | Interval for checking feature flags | `300` |
| `USAGE_SAMPLE_INTERVAL_SECONDS` | Interval for sampling weekly usage | `60` |
| `USAGE_NOTIFICATION_INTERVAL_SECONDS` | Interval for sending routine balance updates | `900` |

---

## Discord Bot Configuration

Create a private channel for state persistence and invite the bot with these permissions:
- **View Channel**
- **Read Message History**
- **Send Messages**
- **Attach Files**
- **Manage Messages** *(needed to delete stale state JSON files)*

---

## Uptime Monitoring

Configure **UptimeRobot** (or a similar tool) to query the root endpoint (`GET /`) of your Render Web Service every 5 minutes to prevent the free instance from sleeping. Note that UptimeRobot does not make the local filesystem permanent; Discord remains the authoritative store.

---

## Web API Status Endpoints

- `GET /`: Returns service status.
- `GET /health`: Returns a JSON object with execution metrics, usage percentage, velocity rate, and warning statuses without leaking credentials or secrets.
