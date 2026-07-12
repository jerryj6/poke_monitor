import json
import os
import sys
import datetime
import time
import threading
import urllib.parse
from io import BytesIO
import asyncio
from flask import Flask, jsonify
import requests
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------
# CONSTANTS & DEFAULTS
# ---------------------------------------------------------
STATE_MARKER_DEFAULT = "POKE_MONITOR_STATE_V2"

# ---------------------------------------------------------
# GLOBAL RUNTIME STATE
# ---------------------------------------------------------
state = {}
state_lock = threading.Lock()
canonical_msg_id = None
remote_state_dirty = False
config_instance = None

health_status = {
    "service": "poke-monitor-combined",
    "webServer": "online",
    "monitorRunning": False,
    "stateSource": "unknown",
    "remoteStateAvailable": False,
    "remoteStateDirty": False,
    "flagsCheckIntervalSeconds": 300,
    "usageSampleIntervalSeconds": 60,
    "usageNotificationIntervalSeconds": 900,
    "lastFlagsCheckStartedAt": None,
    "lastFlagsCheckCompletedAt": None,
    "lastUsageSampleAt": None,
    "lastRoutineUsageNotificationAt": None,
    "lastUsageWarningAt": None,
    "currentWeeklyUsagePercent": None,
    "recentUsageVelocityPpPerMinute": None,
    "usageWarningActive": False,
    "usageSampleCount": 0,
    "tokenExpired": False,
    "lastError": None
}
health_lock = threading.Lock()

# Flask App
app = Flask(__name__)

# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------
def get_iso_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def iso_to_unix(iso_str):
    try:
        s = iso_str.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0

def fake_center(text, pad_count):
    blank = "\u2800"
    return blank * pad_count + text

def format_duration(ms_timestamp):
    try:
        if not ms_timestamp:
            return "Unknown"
        dt_reset = datetime.datetime.fromtimestamp(float(ms_timestamp) / 1000, datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = dt_reset - now
        if delta.total_seconds() <= 0:
            return "Resets now"

        days = delta.days
        hours, _ = divmod(delta.seconds, 3600)

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")

        return " ".join(parts) or "<1h"
    except Exception:
        return "Unknown"

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------
class Config:
    def __init__(self):
        # Secrets
        self.discord_bot_token = self._get_required("DISCORD_BOT_TOKEN")
        self.discord_state_channel_id = self._get_required("DISCORD_STATE_CHANNEL_ID")
        self.poke_session_token = self._get_required("POKE_SESSION_TOKEN")
        self.poke_flags_payload = self._get_required("POKE_FLAGS_PAYLOAD")
        self.flags_webhook_url = self._get_required("FLAGS_WEBHOOK_URL")
        self.credit_webhook_url = self._get_required("CREDIT_WEBHOOK_URL")

        # Optional Forwarding Variables
        self.source_channel_id = os.environ.get("SOURCE_CHANNEL_ID")
        self.destination_channel_id = os.environ.get("DESTINATION_CHANNEL_ID")

        # Basic validations
        if not self.discord_state_channel_id.isdigit():
            raise ValueError("DISCORD_STATE_CHANNEL_ID must be a numeric string")
        if not self.flags_webhook_url.startswith("https://"):
            raise ValueError("FLAGS_WEBHOOK_URL must start with https://")
        if not self.credit_webhook_url.startswith("https://"):
            raise ValueError("CREDIT_WEBHOOK_URL must start with https://")
        if self.source_channel_id:
            self.source_channel_id = self.source_channel_id.strip()
            if not self.source_channel_id.isdigit():
                raise ValueError("SOURCE_CHANNEL_ID must be a numeric string")
        if self.destination_channel_id:
            self.destination_channel_id = self.destination_channel_id.strip()
            if not self.destination_channel_id.isdigit():
                raise ValueError("DESTINATION_CHANNEL_ID must be a numeric string")

        # Intervals & Windows
        self.flags_check_interval = self._get_int("FLAGS_CHECK_INTERVAL_SECONDS", 300)
        self.usage_sample_interval = self._get_int("USAGE_SAMPLE_INTERVAL_SECONDS", 60)
        self.usage_notification_interval = self._get_int("USAGE_NOTIFICATION_INTERVAL_SECONDS", 900)
        self.usage_history_checkpoint = self._get_int("USAGE_HISTORY_CHECKPOINT_SECONDS", 900)
        self.suppress_routine_after_warning = self._get_int("SUPPRESS_ROUTINE_AFTER_WARNING_SECONDS", 60)

        # Warning & Velocity Settings
        self.warning_cooldown = self._get_int("USAGE_WARNING_COOLDOWN_SECONDS", 900)
        self.recent_window_seconds = self._get_int("USAGE_RECENT_WINDOW_SECONDS", 600)
        self.min_velocity_window_seconds = self._get_int("USAGE_MIN_VELOCITY_WINDOW_SECONDS", 480)
        self.history_max_samples = self._get_int("USAGE_HISTORY_MAX_SAMPLES", 120)
        self.history_max_age_seconds = self._get_int("USAGE_HISTORY_MAX_AGE_SECONDS", 7200)

        self.warning_velocity_pp_per_min = self._get_float("USAGE_WARNING_VELOCITY_PP_PER_MINUTE", 0.125)
        self.warning_min_delta_pp = self._get_float("USAGE_WARNING_MIN_DELTA_PP", 1.25)
        self.warning_realert_delta_pp = self._get_float("USAGE_WARNING_REALERT_DELTA_PP", 2.00)
        self.warning_realert_velocity_ratio = self._get_float("USAGE_WARNING_REALERT_VELOCITY_RATIO", 1.50)

        self.reset_drop_tolerance_pp = self._get_float("USAGE_RESET_DROP_TOLERANCE_PP", 0.25)

        self.port = self._get_int("PORT", 10000)
        self.state_marker = os.environ.get("STATE_MARKER", STATE_MARKER_DEFAULT).strip()
        self.local_state_path = os.environ.get("LOCAL_STATE_PATH", "state.json").strip()
        self.poke_client_version = os.environ.get("POKE_CLIENT_VERSION", "1.360.2").strip()
        self.usage_velocity_debug = os.environ.get("USAGE_VELOCITY_DEBUG", "false").lower() == "true"

        print("[Config] Configuration validated successfully.")
        sys.stdout.flush()

    def _get_required(self, name):
        val = os.environ.get(name)
        if not val:
            raise ValueError(f"Missing required environment variable: {name}")
        return val.strip()

    def _get_int(self, name, default):
        val = os.environ.get(name)
        if not val:
            return default
        try:
            parsed = int(val)
            if parsed <= 0:
                raise ValueError()
            return parsed
        except ValueError:
            raise ValueError(f"Environment variable {name} must be a positive integer")

    def _get_float(self, name, default):
        val = os.environ.get(name)
        if not val:
            return default
        try:
            parsed = float(val)
            if parsed < 0:
                raise ValueError()
            return parsed
        except ValueError:
            raise ValueError(f"Environment variable {name} must be a non-negative float")

# ---------------------------------------------------------
# HTTP WRAPPER WITH RETRIES
# ---------------------------------------------------------
def make_request(url, data=None, headers=None, method="GET", retries=3, delay=3, auth_header=None):
    if headers is None:
        headers = {}
    else:
        headers = headers.copy()

    parsed_url = urllib.parse.urlparse(url)
    if auth_header and "discord.com" in parsed_url.netloc:
        headers["Authorization"] = auth_header

    for attempt in range(retries):
        try:
            if method == "GET":
                res = requests.get(url, headers=headers, timeout=15)
            elif method == "POST":
                if isinstance(data, dict):
                    res = requests.post(url, json=data, headers=headers, timeout=15)
                else:
                    res = requests.post(url, data=data, headers=headers, timeout=15)
            elif method == "DELETE":
                res = requests.delete(url, headers=headers, timeout=15)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if res.status_code == 429:
                retry_after = 5
                try:
                    retry_after = float(res.json().get("retry_after", 5))
                except Exception:
                    if "Retry-After" in res.headers:
                        try:
                            retry_after = float(res.headers["Retry-After"])
                        except Exception:
                            pass
                print(f"[HTTP] Rate limited (429). Retrying in {retry_after}s...", file=sys.stderr)
                time.sleep(retry_after)
                continue

            if res.status_code in [500, 502, 503, 504]:
                if attempt == retries - 1:
                    return res.status_code, res.text
                print(f"[HTTP] Attempt {attempt+1} failed with status {res.status_code}. Retrying...", file=sys.stderr)
                time.sleep(delay)
                continue

            return res.status_code, res.text

        except Exception as e:
            if attempt == retries - 1:
                raise e
            print(f"[HTTP] Attempt {attempt+1} failed with error: {e}. Retrying...", file=sys.stderr)
            time.sleep(delay)

    return 500, "Max retries reached"

# ---------------------------------------------------------
# DISCORD STATE STORAGE
# ---------------------------------------------------------
def get_state_from_discord(config):
    url = f"https://discord.com/api/v10/channels/{config.discord_state_channel_id}/messages?limit=50"
    auth = f"Bot {config.discord_bot_token}"
    
    print("[State] Searching Discord state channel")
    status, body = make_request(url, method="GET", auth_header=auth)
    if status != 200:
        raise Exception(f"Failed to fetch channel history: HTTP {status} - {body}")

    messages = json.loads(body)
    for msg in messages:
        content = msg.get("content", "").strip()
        if content == config.state_marker:
            attachments = msg.get("attachments", [])
            if not attachments:
                continue

            att = attachments[0]
            att_url = att.get("url")
            if not att_url:
                continue

            print(f"[State] Downloading state attachment from {att_url}")
            att_status, att_body = make_request(att_url, method="GET")
            if att_status != 200:
                print(f"[State] Failed to download attachment: HTTP {att_status}", file=sys.stderr)
                continue

            try:
                state_dict = json.loads(att_body)
                if not isinstance(state_dict, dict):
                    raise ValueError("Root element is not a JSON object")
                if "last_flags" not in state_dict or not isinstance(state_dict["last_flags"], dict):
                    raise ValueError("last_flags missing or not an object")
                if "usage_history" not in state_dict or not isinstance(state_dict["usage_history"], list):
                    raise ValueError("usage_history missing or not a list")

                print(f"[State] Loaded Discord state updated at {state_dict.get('updated_at')}")
                return state_dict, msg["id"]
            except Exception as parse_e:
                print(f"[State] Newest candidate malformed: {parse_e}; checking older state", file=sys.stderr)
                continue

    return None, None

def save_state_to_discord(config, state_dict, old_msg_id=None):
    global canonical_msg_id, remote_state_dirty
    url = f"https://discord.com/api/v10/channels/{config.discord_state_channel_id}/messages"
    auth = f"Bot {config.discord_bot_token}"

    state_dict["updated_at"] = get_iso_now()
    serialized_state = json.dumps(state_dict, indent=2)

    headers = {
        "Authorization": auth,
        "User-Agent": "PokeMonitor/2.0"
    }
    
    files = {
        "files[0]": ("state.json", serialized_state, "application/json")
    }
    data = {
        "payload_json": json.dumps({"content": config.state_marker})
    }

    try:
        res = requests.post(url, headers=headers, data=data, files=files, timeout=20)
        if res.status_code not in [200, 201]:
            raise Exception(f"Discord state upload returned HTTP {res.status_code} - {res.text}")

        new_msg = res.json()
        new_msg_id = new_msg["id"]
        canonical_msg_id = new_msg_id
        remote_state_dirty = False
        print("[State] Uploaded canonical state to Discord")

        if old_msg_id:
            print(f"[State] Deleting old state message: {old_msg_id}")
            del_url = f"https://discord.com/api/v10/channels/{config.discord_state_channel_id}/messages/{old_msg_id}"
            del_status, del_body = make_request(del_url, method="DELETE", auth_header=auth)
            if del_status not in [200, 204]:
                print(f"[State] Deletion of old state message {old_msg_id} failed: HTTP {del_status} - {del_body}", file=sys.stderr)

        return new_msg_id
    except Exception as e:
        remote_state_dirty = True
        print(f"[State] Error uploading state to Discord: {e}. Remote marked dirty.", file=sys.stderr)
        return None

# ---------------------------------------------------------
# LOCAL CACHE
# ---------------------------------------------------------
def save_state_locally(config, state_dict):
    tmp_path = config.local_state_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state_dict, f, indent=2)
        os.replace(tmp_path, config.local_state_path)
    except Exception as e:
        print(f"[State] Error saving local cache: {e}", file=sys.stderr)

def load_state_locally(config):
    if not os.path.exists(config.local_state_path):
        return None
    try:
        with open(config.local_state_path, "r") as f:
            state_dict = json.load(f)
            if isinstance(state_dict, dict) and "last_flags" in state_dict and "usage_history" in state_dict:
                return state_dict
    except Exception as e:
        print(f"[State] Error loading local cache: {e}", file=sys.stderr)
    return None

# ---------------------------------------------------------
# STATE MIGRATION
# ---------------------------------------------------------
def migrate_state(state_dict):
    v = state_dict.get("schema_version", 0)
    if v < 2:
        state_dict["schema_version"] = 2
        state_dict.setdefault("updated_at", None)
        state_dict.setdefault("last_flags", {})
        state_dict.setdefault("token_expiry_notified", False)
        state_dict.setdefault("last_successful_flags_check_at", None)
        state_dict.setdefault("last_successful_balance_check_at", None)
        state_dict.setdefault("last_error", None)
        state_dict.setdefault("usage_history", [])
        state_dict.setdefault("usage_period_resets_at", None)
        state_dict.setdefault("last_usage_notification_at", None)
        state_dict.setdefault("last_usage_notification_percent", None)
        state_dict.setdefault("last_routine_usage_notification_at", None)
        state_dict.setdefault("last_usage_warning_at", None)
        state_dict.setdefault("last_usage_warning_percent", None)
        state_dict.setdefault("last_usage_warning_velocity", None)
        state_dict.setdefault("usage_warning_active", False)
        state_dict.setdefault("usage_warning_below_threshold_count", 0)
        state_dict.setdefault("last_usage_history_checkpoint_at", None)
    
    # Combined config settings
    state_dict.setdefault("changelog_forwarding_enabled", True)
    return state_dict

def init_fresh_state():
    return {
        "schema_version": 2,
        "updated_at": get_iso_now(),
        "last_flags": {},
        "token_expiry_notified": False,
        "last_successful_flags_check_at": None,
        "last_successful_balance_check_at": None,
        "last_error": None,
        "usage_history": [],
        "usage_period_resets_at": None,
        "last_usage_notification_at": None,
        "last_usage_notification_percent": None,
        "last_routine_usage_notification_at": None,
        "last_usage_warning_at": None,
        "last_usage_warning_percent": None,
        "last_usage_warning_velocity": None,
        "usage_warning_active": False,
        "usage_warning_below_threshold_count": 0,
        "last_usage_history_checkpoint_at": None,
        "changelog_forwarding_enabled": True
    }

# ---------------------------------------------------------
# DISCORD WEBHOOK LAYER
# ---------------------------------------------------------
def send_webhook_safely(webhook_url, title, description, color, fields):
    split_fields = []
    for f in fields:
        name = f.get("name", "")
        value = f.get("value", "")
        inline = f.get("inline", False)

        if len(value) > 1000:
            chunks = [value[i:i+1000] for i in range(0, len(value), 1000)]
            for idx, chunk in enumerate(chunks):
                suffix = f" (Part {idx+1})" if len(chunks) > 1 else ""
                split_fields.append({
                    "name": f"{name}{suffix}",
                    "value": chunk,
                    "inline": inline
                })
        else:
            split_fields.append({
                "name": name,
                "value": value,
                "inline": inline
            })

    embeds = []
    current_fields = []
    current_char_count = len(title or "") + len(description or "")

    for f in split_fields:
        field_chars = len(f["name"]) + len(f["value"])
        if len(current_fields) >= 20 or current_char_count + field_chars > 5000:
            embeds.append({
                "title": title if len(embeds) == 0 else f"{title} (Continued)",
                "description": description if len(embeds) == 0 else None,
                "color": color,
                "fields": current_fields
            })
            current_fields = [f]
            current_char_count = field_chars
        else:
            current_fields.append(f)
            current_char_count += field_chars

    if current_fields or not embeds:
        embeds.append({
            "title": title if len(embeds) == 0 else f"{title} (Continued)",
            "description": description if len(embeds) == 0 else None,
            "color": color,
            "fields": current_fields
        })

    success = True
    for i in range(0, len(embeds), 10):
        batch = embeds[i:i+10]
        payload = {
            "embeds": batch,
            "allowed_mentions": {"parse": []}
        }
        try:
            res = requests.post(webhook_url, json=payload, headers={"User-Agent": "PokeMonitor/2.0"}, timeout=15)
            if res.status_code not in [200, 204]:
                print(f"[Webhook] Failed: HTTP {res.status_code} - {res.text}", file=sys.stderr)
                success = False
        except Exception as e:
            print(f"[Webhook] Exception sending payload: {e}", file=sys.stderr)
            success = False

    return success

def send_initial_alert(webhook_url, flags):
    enabled_count = sum(1 for f in flags.values() if f.get("enabled", False))
    total_count = len(flags)
    embed = {
        "title": "⚙️ Poke.com Monitor Initialized",
        "description": f"Successfully connected. Currently tracking **{total_count}** flags (**{enabled_count}** enabled).",
        "color": 3447003,
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed], "allowed_mentions": {"parse": []}}, timeout=15)
    except Exception as e:
        print(f"[Webhook] Initial alert failed: {e}", file=sys.stderr)

# ---------------------------------------------------------
# FETCH & FORMAT DATA MODULES (SHARED WITH SLASH COMMANDS)
# ---------------------------------------------------------
def fetch_usage_data(config):
    url = "https://poke.com/api/v1/subscription/status"
    headers = {
        "Accept": "application/json",
        "Cookie": f"__Secure-poke_production.session_token={config.poke_session_token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    }
    status, body = make_request(url, headers=headers, method="GET", retries=2, delay=2)
    return status, body

def format_balance_embed(status, body):
    if status in [401, 403]:
        embed = discord.Embed(
            title="⚠️ Poke Session Token Expired",
            description="Your `POKE_SESSION_TOKEN` is invalid or expired. Please update it in Render environment variables.",
            color=15158332
        )
        return embed
    if status != 200:
        embed = discord.Embed(
            title="❌ Error Checking Balance",
            description=f"Poke API returned HTTP {status}.",
            color=15158332
        )
        return embed

    res_json = json.loads(body)
    base_usage = res_json.get("baseUsage", {})
    usage_info = None
    if isinstance(base_usage, dict) and base_usage.get("present", False):
        usage_info = base_usage
    else:
        buckets = res_json.get("buckets", [])
        usage_info = next((b for b in buckets if b.get("interval") == "weekly"), None)

    if not usage_info:
        embed = discord.Embed(
            title="❌ Error Checking Balance",
            description="No weekly usage info present in response.",
            color=15158332
        )
        return embed

    pct = float(usage_info.get("usedPercent", 0.0))
    resets_at = usage_info.get("resetsAt")

    bar_len = 22
    filled = int(round((pct / 100.0) * bar_len))
    bar = "█" * min(filled, bar_len) + "░" * max(0, bar_len - filled)
    reset_time_str = format_duration(resets_at)

    weekly_line = f"Weekly: **{pct:.2f}%**"
    reset_line = f"Resets in *{reset_time_str}*"
    description = (
        f"{fake_center(weekly_line, 4)}\n"
        f"`{bar}`\n"
        f"{fake_center(reset_line, 4)}"
    )

    if pct >= 80.0:
        color = 15158332
    elif pct >= 50.0:
        color = 16776960
    else:
        color = 3066993

    embed = discord.Embed(
        title="🌴 Poke Balance",
        description=description,
        color=color
    )
    return embed

def fetch_feature_flags_data(config):
    ts = int(time.time() * 1000)
    url = f"https://poke.com/palm-trees/flags?v=2&ip=0&_={ts}&ver={config.poke_client_version}&compression=base64"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    }
    post_data = f"data={urllib.parse.quote(config.poke_flags_payload)}"
    status, body = make_request(url, data=post_data, headers=headers, method="POST")
    return status, body

def compare_flags(current_flags, previous_flags):
    added = []
    removed = []
    changed = []

    for key, current_val in current_flags.items():
        prev_val = previous_flags.get(key)
        if prev_val is None:
            added.append(current_val)
        else:
            curr_enabled = current_val.get("enabled", False)
            prev_enabled = prev_val.get("enabled", False)
            curr_variant = current_val.get("variant")
            prev_variant = prev_val.get("variant")

            curr_metadata = current_val.get("metadata", {}) or {}
            prev_metadata = prev_val.get("metadata", {}) or {}
            curr_payload = curr_metadata.get("payload")
            prev_payload = prev_metadata.get("payload")
            curr_version = curr_metadata.get("version")
            prev_version = prev_metadata.get("version")

            if (curr_enabled != prev_enabled) or (curr_variant != prev_variant) or (curr_payload != prev_payload) or (curr_version != prev_version):
                changed.append((prev_val, current_val))

    for key, prev_val in previous_flags.items():
        if key not in current_flags:
            removed.append(prev_val)

    return added, removed, changed

def format_flag_fields(added, removed, changed):
    fields = []
    if added:
        added_desc = []
        for f in added:
            enabled_str = "🟢 Enabled" if f.get('enabled', False) else "🔴 Disabled"
            item_desc = [f"• `{f['key']}`: {enabled_str}"]
            meta = f.get('metadata', {}) or {}
            fid = meta.get('id')
            fver = meta.get('version')
            if fid is not None:
                item_desc.append(f"  ID: `{fid}`" + (f" (v{fver})" if fver is not None else ""))
            reason = f.get('reason', {}) or {}
            rdesc = reason.get('description') or reason.get('code')
            if rdesc:
                item_desc.append(f"  Reason: *{rdesc}*")
            payload = meta.get('payload')
            if payload is not None:
                item_desc.append(f"  Payload: `{payload}`")
            var = f.get('variant')
            if var:
                item_desc.append(f"  Variant: `{var}`")
            added_desc.append("\n".join(item_desc))

        fields.append({
            "name": "➕ Added Flags",
            "value": "\n".join(added_desc),
            "inline": False
        })

    if removed:
        fields.append({
            "name": "➖ Removed Flags",
            "value": "\n".join([f"• `{f['key']}`" for f in removed]),
            "inline": False
        })

    if changed:
        changed_desc = []
        for prev, curr in changed:
            p_enabled = prev.get('enabled', False)
            c_enabled = curr.get('enabled', False)
            p_status = "Enabled" if p_enabled else "Disabled"
            c_status = "Enabled" if c_enabled else "Disabled"
            p_icon = "🟢" if p_enabled else "🔴"
            c_icon = "🟢" if c_enabled else "🔴"

            p_var = prev.get('variant')
            c_var = curr.get('variant')

            p_meta = prev.get('metadata', {}) or {}
            c_meta = curr.get('metadata', {}) or {}
            p_pay = p_meta.get('payload')
            c_pay = c_meta.get('payload')

            if p_enabled and not c_enabled:
                changed_desc.append(f"• `{curr['key']}`: {p_icon} {p_status} -> {c_icon} {c_status}")
                continue

            diffs = []
            if p_status != c_status:
                diffs.append(f"{p_icon} {p_status} -> {c_icon} {c_status}")
            p_ver = p_meta.get('version')
            c_ver = c_meta.get('version')
            if p_ver != c_ver:
                diffs.append(f"Version: `v{p_ver}` -> `v{c_ver}`")
            if p_var != c_var:
                diffs.append(f"Variant: `{p_var}` -> `{c_var}`")
            if p_pay != c_pay:
                diffs.append(f"Payload: `{p_pay}` -> `{c_pay}`")

            if not p_enabled and c_enabled:
                fid = c_meta.get('id')
                fver = c_meta.get('version')
                if fid is not None:
                    diffs.append(f"ID: `{fid}`" + (f" (v{fver})" if fver is not None else ""))
                reason = curr.get('reason', {}) or {}
                rdesc = reason.get('description') or reason.get('code')
                if rdesc:
                    diffs.append(f"Reason: *{rdesc}*")
                payload = c_meta.get('payload')
                if payload is not None:
                    diffs.append(f"Payload: `{payload}`")
                var = curr.get('variant')
                if var:
                    diffs.append(f"Variant: `{var}`")

            if diffs:
                changed_desc.append(f"• `{curr['key']}`:\n  " + "\n  ".join(diffs))

        if changed_desc:
            fields.append({
                "name": "🔄 Modified Flags",
                "value": "\n".join(changed_desc),
                "inline": False
            })
    return fields

# ---------------------------------------------------------
# FLAG CHECK BACKGROUND ENGINE
# ---------------------------------------------------------
def run_feature_flags_sync(config):
    global state
    print("Checking feature flags...")

    with health_lock:
        health_status["lastFlagsCheckStartedAt"] = get_iso_now()

    status, body = fetch_feature_flags_data(config)
    if status != 200:
        raise Exception(f"Failed to fetch feature flags: HTTP {status} - {body}")

    res_json = json.loads(body)
    current_flags = res_json.get("flags", {})

    with state_lock:
        previous_flags = state.get("last_flags", None)

        if not previous_flags:
            print("[Flags] Initial run: Saving current flags state.")
            state["last_flags"] = current_flags
            state["last_successful_flags_check_at"] = get_iso_now()
            save_state_locally(config, state)
            save_state_to_discord(config, state, canonical_msg_id)
            send_initial_alert(config.flags_webhook_url, current_flags)
            return

        added, removed, changed = compare_flags(current_flags, previous_flags)

        alert_sent = True
        if added or removed or changed:
            print("Changes detected in feature flags. Sending Discord alert.")
            fields = format_flag_fields(added, removed, changed)
            alert_sent = send_webhook_safely(config.flags_webhook_url, "Poke Flag Change", None, 15105570, fields)

        if alert_sent:
            state["last_flags"] = current_flags
            state["last_successful_flags_check_at"] = get_iso_now()
            save_state_locally(config, state)
            save_state_to_discord(config, state, canonical_msg_id)
        else:
            print("[Flags] Webhook sending failed, not advancing flags state", file=sys.stderr)

    with health_lock:
        health_status["lastFlagsCheckCompletedAt"] = get_iso_now()
        health_status["lastError"] = None

# ---------------------------------------------------------
# USAGE CHECK BACKGROUND ENGINE
# ---------------------------------------------------------
def run_usage_and_balance_sync(config):
    global state
    print("Checking API limits / balance...")
    now_ts = time.time()
    
    with health_lock:
        health_status["lastUsageSampleAt"] = get_iso_now()

    status, body = fetch_usage_data(config)

    # 1. Handle Expiry Responses
    if status in [401, 403]:
        with state_lock:
            if not state.get("token_expiry_notified", False):
                state["token_expiry_notified"] = True
                save_state_locally(config, state)
                save_state_to_discord(config, state, canonical_msg_id)
                
                expire_payload = {
                    "embeds": [{
                        "title": "⚠️ Poke Session Token Expired",
                        "description": "Your `POKE_SESSION_TOKEN` is invalid or expired. Please update it in Render environment variables.",
                        "color": 15158332
                    }]
                }
                requests.post(config.credit_webhook_url, json=expire_payload, timeout=15)
                
        with health_lock:
            health_status["tokenExpired"] = True
        return

    if status != 200:
        raise Exception(f"Failed to fetch usage: HTTP {status} - {body}")

    res_json = json.loads(body)
    base_usage = res_json.get("baseUsage", {})
    usage_info = None
    if isinstance(base_usage, dict) and base_usage.get("present", False):
        usage_info = base_usage
    else:
        buckets = res_json.get("buckets", [])
        usage_info = next((b for b in buckets if b.get("interval") == "weekly"), None)

    if not usage_info:
        print("[Usage] No weekly usage info present in response", file=sys.stderr)
        return

    pct = float(usage_info.get("usedPercent", 0.0))
    resets_at = usage_info.get("resetsAt")

    with state_lock:
        if state.get("token_expiry_notified", False):
            state["token_expiry_notified"] = False
            rec_payload = {
                "embeds": [{
                    "title": "✅ Poke Session Token Recovered",
                    "description": "The session token is valid again. Usage monitoring has resumed.",
                    "color": 3066993
                }]
            }
            requests.post(config.credit_webhook_url, json=rec_payload, timeout=15)
            save_state_locally(config, state)
            save_state_to_discord(config, state, canonical_msg_id)

        state["last_successful_balance_check_at"] = get_iso_now()
        history = state.setdefault("usage_history", [])

        last_sample = history[-1] if history else None
        reset_detected = False

        if last_sample:
            prev_pct = float(last_sample["used_percent"])
            if pct < prev_pct - config.reset_drop_tolerance_pp:
                print(f"[Usage] Weekly usage decreased from {prev_pct}% to {pct}%; resetting velocity history")
                reset_detected = True
            elif resets_at and state.get("usage_period_resets_at") and resets_at != state["usage_period_resets_at"]:
                print(f"[Usage] Reset timestamp changed from {state['usage_period_resets_at']} to {resets_at}; resetting velocity history")
                reset_detected = True
            elif state.get("usage_period_resets_at") and (now_ts * 1000) > float(state["usage_period_resets_at"]):
                print("[Usage] Billing cycle reset threshold passed; resetting velocity history")
                reset_detected = True

        if reset_detected:
            history.clear()
            state["usage_warning_active"] = False
            state["usage_warning_below_threshold_count"] = 0
            state["usage_period_resets_at"] = resets_at
            save_state_locally(config, state)
            save_state_to_discord(config, state, canonical_msg_id)

        history.append({
            "timestamp": get_iso_now(),
            "used_percent": pct,
            "resets_at": resets_at
        })

        max_age_limit = now_ts - config.history_max_age_seconds
        history = [s for s in history if iso_to_unix(s["timestamp"]) >= max_age_limit]
        if len(history) > config.history_max_samples:
            history = history[-config.history_max_samples:]
        state["usage_history"] = history
        save_state_locally(config, state)

        # Velocity Warning Calculations
        velocity = None
        delta_pp = 0.0
        elapsed_seconds = 0.0

        boundary_ts = now_ts - config.recent_window_seconds
        older_sample = None
        for sample in reversed(history):
            sample_ts = iso_to_unix(sample["timestamp"])
            if sample_ts <= boundary_ts:
                older_sample = sample
                break

        if older_sample:
            elapsed_seconds = now_ts - iso_to_unix(older_sample["timestamp"])
            if elapsed_seconds >= config.min_velocity_window_seconds:
                older_pct = float(older_sample["used_percent"])
                delta_pp = pct - older_pct
                elapsed_minutes = elapsed_seconds / 60.0
                velocity = delta_pp / elapsed_minutes

                slice_samples = [s for s in history if iso_to_unix(older_sample["timestamp"]) <= iso_to_unix(s["timestamp"]) <= now_ts]
                has_drop_in_slice = False
                for idx in range(1, len(slice_samples)):
                    pred = float(slice_samples[idx-1]["used_percent"])
                    curr = float(slice_samples[idx]["used_percent"])
                    if curr < pred - config.reset_drop_tolerance_pp:
                        has_drop_in_slice = True
                        break

                if not has_drop_in_slice:
                    is_above_threshold = (
                        pct > older_pct and
                        delta_pp >= config.warning_min_delta_pp and
                        velocity >= config.warning_velocity_pp_per_min
                    )

                    if config.usage_velocity_debug:
                        print(f"[Debug] Velocity: {velocity:.4f} pp/min. Delta: {delta_pp:.2f}%. Above threshold: {is_above_threshold}")

                    if is_above_threshold:
                        state["usage_warning_below_threshold_count"] = 0
                        last_warning_at = state.get("last_usage_warning_at")
                        is_cooldown_passed = (last_warning_at is None or (now_ts - float(last_warning_at)) >= config.warning_cooldown)

                        is_realert_allowed = False
                        if not state.get("usage_warning_active", False):
                            is_realert_allowed = True
                        else:
                            last_warn_pct = state.get("last_usage_warning_percent", 0.0)
                            last_warn_vel = state.get("last_usage_warning_velocity", 0.0)
                            
                            delta_since_warn = pct - float(last_warn_pct)
                            ratio_since_warn = velocity / float(last_warn_vel) if last_warn_vel > 0 else 999.0

                            if delta_since_warn >= config.warning_realert_delta_pp:
                                is_realert_allowed = True
                            elif ratio_since_warn >= config.warning_realert_velocity_ratio:
                                is_realert_allowed = True

                        if is_cooldown_passed and is_realert_allowed:
                            reset_time_str = format_duration(resets_at)
                            hourly_equiv = velocity * 60.0
                            warn_description = (
                                f"Current weekly usage: **{pct:.2f}%**\n"
                                f"Recent increase: **+{delta_pp:.2f} points** in **{elapsed_minutes:.1f} minutes**\n"
                                f"Recent rate: **+{velocity:.3f} points/min**\n"
                                f"Hourly equivalent: **+{hourly_equiv:.2f} points/hour**\n"
                                f"Resets in: *{reset_time_str}*"
                            )
                            alert_ok = send_webhook_safely(config.credit_webhook_url, "⚠️ Poke Usage Rising Quickly", warn_description, 16750848, [])
                            if alert_ok:
                                state["last_usage_warning_at"] = now_ts
                                state["last_usage_warning_percent"] = pct
                                state["last_usage_warning_velocity"] = velocity
                                state["usage_warning_active"] = True
                                save_state_locally(config, state)
                                save_state_to_discord(config, state, canonical_msg_id)
                                print("[Usage] Velocity alert successfully delivered")
                            else:
                                print("[Usage] Velocity alert delivery failed. Cooldown not updated.", file=sys.stderr)

                    else:
                        if state.get("usage_warning_active", False):
                            count = state.get("usage_warning_below_threshold_count", 0) + 1
                            state["usage_warning_below_threshold_count"] = count
                            if count >= 3:
                                state["usage_warning_active"] = False
                                state["usage_warning_below_threshold_count"] = 0
                                save_state_locally(config, state)
                                save_state_to_discord(config, state, canonical_msg_id)
                                print("[Usage] Warning active flag cleared (below threshold threshold count met)")

        # 15-minute Routine Notifications
        last_routine_notification = state.get("last_routine_usage_notification_at")
        is_routine_due = (last_routine_notification is None or (now_ts - float(last_routine_notification)) >= config.usage_notification_interval)

        if is_routine_due:
            last_warn_at = state.get("last_usage_warning_at")
            is_suppressed = (last_warn_at is not None and (now_ts - float(last_warn_at)) <= config.suppress_routine_after_warning)

            if is_suppressed:
                print("[Usage] Suppressed routine notification within warning window.")
                state["last_routine_usage_notification_at"] = now_ts
                save_state_locally(config, state)
            else:
                filled = int(round((pct / 100.0) * 22))
                bar = "█" * min(filled, 22) + "░" * max(0, 22 - filled)
                reset_time_str = format_duration(resets_at)
                weekly_line = f"Weekly: **{pct:.2f}%**"
                reset_line = f"Resets in *{reset_time_str}*"
                description = (
                    f"{fake_center(weekly_line, 4)}\n"
                    f"`{bar}`\n"
                    f"{fake_center(reset_line, 4)}"
                )

                if pct >= 80.0:
                    color = 15158332
                elif pct >= 50.0:
                    color = 16776960
                else:
                    color = 3066993

                routine_ok = send_webhook_safely(config.credit_webhook_url, "🌴 Poke Balance", description, color, [])
                if routine_ok:
                    state["last_routine_usage_notification_at"] = now_ts
                    save_state_locally(config, state)
                    save_state_to_discord(config, state, canonical_msg_id)
                else:
                    print("[Usage] Routine balance delivery failed. Not advancing timestamp.", file=sys.stderr)

    with health_lock:
        health_status["lastRoutineUsageNotificationAt"] = state.get("last_routine_usage_notification_at")
        health_status["lastUsageWarningAt"] = state.get("last_usage_warning_at")
        health_status["currentWeeklyUsagePercent"] = pct
        health_status["recentUsageVelocityPpPerMinute"] = velocity
        health_status["usageWarningActive"] = state.get("usage_warning_active", False)
        health_status["usageSampleCount"] = len(history)
        health_status["tokenExpired"] = False
        health_status["lastError"] = None

# ---------------------------------------------------------
# DISCORD CLIENT & BOT SETUP
# ---------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guild_messages = True

class PokeMonitorBot(commands.Bot):
    async def close(self):
        print("[Monitor] Shutting down Discord Bot...")
        check_usage_loop_task.cancel()
        check_flags_loop_task.cancel()
        
        with state_lock:
            if remote_state_dirty:
                print("[State] Saving dirty state during shutdown...")
                try:
                    await asyncio.to_thread(save_state_to_discord, config_instance, state, canonical_msg_id)
                except Exception as e:
                    print(f"[State] Defer save failed on close: {e}", file=sys.stderr)
                    
        await super().close()
        print("[Monitor] Discord Bot closed.")

bot = PokeMonitorBot(command_prefix="!", intents=intents)

# ---------------------------------------------------------
# DISCORD BOT EVENTS
# ---------------------------------------------------------
@bot.event
async def on_ready():
    print(f"[Discord] Logged in as {bot.user} ({bot.user.id})")
    
    # Sync command tree
    try:
        # Clear guild-specific command copies to remove duplicates
        for guild in bot.guilds:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"[Discord] Cleared guild-specific commands for: {guild.name} ({guild.id})")
            
        # Sync globally
        synced = await bot.tree.sync()
        print(f"[Discord] Synced {len(synced)} slash commands globally.")
    except Exception as e:
        print(f"[Discord] Slash command sync failed: {e}", file=sys.stderr)

    # Start loop tasks
    if not check_usage_loop_task.is_running():
        check_usage_loop_task.start()
    if not check_flags_loop_task.is_running():
        check_flags_loop_task.start()

    with health_lock:
        health_status["monitorRunning"] = True

@bot.event
async def on_message(message):
    if not bot.user or message.author.id == bot.user.id:
        return

    # Check if Changelog forwarding is configured
    if not config_instance or not config_instance.source_channel_id:
        return

    if str(message.channel.id) != config_instance.source_channel_id:
        return

    # Verify changelog forwarding is enabled in state
    if not state.get("changelog_forwarding_enabled", True):
        return

    # Check if webhook announcement
    if not message.webhook_id:
        print(f"[Ignored] Non-webhook message {message.id} in source channel.")
        return

    print(f"[Changelog] Intercepting message {message.id}")
    try:
        dest_id = config_instance.destination_channel_id or config_instance.source_channel_id
        dest_channel = bot.get_channel(int(dest_id))
        if not dest_channel:
            dest_channel = await bot.fetch_channel(int(dest_id))

        if dest_channel:
            # Files
            files = []
            for att in message.attachments:
                try:
                    att_bytes = await att.read()
                    files.append(discord.File(BytesIO(att_bytes), filename=att.filename))
                except Exception as e:
                    print(f"[Changelog] Attachment read failed for {att.filename}: {e}", file=sys.stderr)

            # Forward
            await dest_channel.send(
                content=message.content,
                embeds=[discord.Embed.from_dict(emb.to_dict()) for emb in message.embeds],
                files=files,
                allowed_mentions=discord.AllowedMentions.none()
            )
            print(f"[Changelog] Forwarded announcement {message.id}")

            # Delete original message
            try:
                await message.delete()
                print(f"[Changelog] Deleted original message {message.id}")
            except Exception as e:
                print(f"[Changelog] Failed to delete original message: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[Changelog] Message forward failed: {e}", file=sys.stderr)

# ---------------------------------------------------------
# DISCORD SLASH COMMANDS
# ---------------------------------------------------------
@bot.tree.command(name="balance", description="Check weekly usage status and remaining balance")
async def slash_balance(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        status, body = await asyncio.to_thread(fetch_usage_data, config_instance)
        embed = format_balance_embed(status, body)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"[Slash] Balance command error: {e}", file=sys.stderr)
        await interaction.followup.send(content=f"❌ Error checking balance: {e}")

@bot.tree.command(name="flags", description="Check current Poke feature flags for updates")
async def slash_flags(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        status, body = await asyncio.to_thread(fetch_feature_flags_data, config_instance)
        if status != 200:
            await interaction.followup.send(content=f"❌ Error checking flags: Poke API returned HTTP {status}.")
            return
            
        res_json = json.loads(body)
        current_flags = res_json.get("flags", {})
        
        with state_lock:
            previous_flags = state.get("last_flags", None)
            
            if previous_flags is None:
                state["last_flags"] = current_flags
                state["last_successful_flags_check_at"] = get_iso_now()
                save_state_locally(config_instance, state)
                save_state_to_discord(config_instance, state, canonical_msg_id)
                await interaction.followup.send(content="⚙️ Flags initialized. Saved current flags to state.")
                return

            added, removed, changed = compare_flags(current_flags, previous_flags)

            if not added and not removed and not changed:
                await interaction.followup.send(content="✅ No changes detected.")
                return

            fields = format_flag_fields(added, removed, changed)
            alert_sent = send_webhook_safely(config_instance.flags_webhook_url, "Poke Flag Change", None, 15105570, fields)
            
            if alert_sent:
                state["last_flags"] = current_flags
                state["last_successful_flags_check_at"] = get_iso_now()
                save_state_locally(config_instance, state)
                save_state_to_discord(config_instance, state, canonical_msg_id)
                
                embed = discord.Embed(
                    title="Poke Flag Change (Changes Detected)",
                    description="Flag changes have been detected and sent to the flags webhook channel.",
                    color=15105570
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(content="❌ Error sending flag updates to the webhook.")

    except Exception as e:
        print(f"[Slash] Flags command error: {e}", file=sys.stderr)
        await interaction.followup.send(content=f"❌ Error checking flags: {e}")

@bot.tree.command(name="changelog", description="Toggle changelog forwarding on or off")
@app_commands.describe(action="Choose to enable or disable changelog forwarding")
@app_commands.choices(action=[
    app_commands.Choice(name="enable", value="enable"),
    app_commands.Choice(name="disable", value="disable")
])
async def slash_changelog(interaction: discord.Interaction, action: app_commands.Choice[str]):
    enabled = (action.value == "enable")
    with state_lock:
        state["changelog_forwarding_enabled"] = enabled
        save_state_locally(config_instance, state)
        save_state_to_discord(config_instance, state, canonical_msg_id)
        
    status_str = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"✅ Changelog forwarding has been **{status_str}**.")

# ---------------------------------------------------------
# BOT BACKGROUND TASK LOOPS
# ---------------------------------------------------------
@tasks.loop(seconds=60)
async def check_usage_loop_task():
    try:
        await asyncio.to_thread(run_usage_and_balance_sync, config_instance)
    except Exception as e:
        print(f"[Task] Error in check usage loop: {e}", file=sys.stderr)
        with health_lock:
            health_status["lastError"] = str(e)

@tasks.loop(seconds=300)
async def check_flags_loop_task():
    try:
        await asyncio.to_thread(run_feature_flags_sync, config_instance)
    except Exception as e:
        print(f"[Task] Error in check flags loop: {e}", file=sys.stderr)
        with health_lock:
            health_status["lastError"] = str(e)



# ---------------------------------------------------------
# FLASK WEB ENDPOINTS
# ---------------------------------------------------------
@app.route("/")
def index():
    return jsonify({
        "service": "poke-monitor-combined",
        "webServer": "online"
    })

@app.route("/health")
def health():
    with health_lock:
        # Check websocket status if bot is ready
        ws_status = "disconnected"
        bot_user = None
        bot_id = None
        guild_count = 0
        
        if bot.is_ready():
            ws_status = "connected"
            bot_user = str(bot.user)
            bot_id = bot.user.id
            guild_count = len(bot.guilds)

        hs = health_status.copy()
        hs.update({
            "discord": ws_status,
            "botUser": bot_user,
            "botId": bot_id,
            "guildCount": guild_count,
            "changelogForwardingEnabled": state.get("changelog_forwarding_enabled", True)
        })
        return jsonify(hs)

# ---------------------------------------------------------
# STARTUP ENTRY POINT
# ---------------------------------------------------------
def main():
    global state, canonical_msg_id, config_instance
    
    # 1. Config Ingestion & Validation
    try:
        config_instance = Config()
    except Exception as e:
        print(f"[Config] Initialization failure: {e}", file=sys.stderr)
        sys.exit(1)

    # Update health status intervals
    with health_lock:
        health_status["flagsCheckIntervalSeconds"] = config_instance.flags_check_interval
        health_status["usageSampleIntervalSeconds"] = config_instance.usage_sample_interval
        health_status["usageNotificationIntervalSeconds"] = config_instance.usage_notification_interval

    # 2. Authoritative Discord State Recovery
    discord_available = False
    try:
        remote_state, msg_id = get_state_from_discord(config_instance)
        discord_available = True
    except Exception as discord_err:
        print(f"[State] Discord state channel unreachable on startup: {discord_err}", file=sys.stderr)
        remote_state, msg_id = None, None

    if discord_available:
        if remote_state:
            state = migrate_state(remote_state)
            canonical_msg_id = msg_id
            save_state_locally(config_instance, state)
            with health_lock:
                health_status["stateSource"] = "discord"
                health_status["remoteStateAvailable"] = True
        else:
            local_state = load_state_locally(config_instance)
            if local_state:
                print("[State] Discord confirmed empty, uploading local state cache as canonical")
                state = migrate_state(local_state)
                canonical_msg_id = save_state_to_discord(config_instance, state)
                with health_lock:
                    health_status["stateSource"] = "local_migrated"
                    health_status["remoteStateAvailable"] = True
            else:
                print("[State] Discord confirmed empty and no local state cache exists. Initializing fresh state.")
                state = init_fresh_state()
                
                # Fetch initial flags to avoid duplicate state posts at startup
                try:
                    print("[State] Fetching initial flags for fresh state setup...")
                    status, body = fetch_feature_flags_data(config_instance)
                    if status == 200:
                        res_json = json.loads(body)
                        state["last_flags"] = res_json.get("flags", {})
                except Exception as flag_err:
                    print(f"[State] Warning: Failed to fetch initial flags during setup: {flag_err}", file=sys.stderr)

                canonical_msg_id = save_state_to_discord(config_instance, state)
                with health_lock:
                    health_status["stateSource"] = "initialized"
                    health_status["remoteStateAvailable"] = True
    else:
        local_state = load_state_locally(config_instance)
        if local_state:
            print("[State] Using local cache as degraded fallback")
            state = migrate_state(local_state)
            with health_lock:
                health_status["stateSource"] = "local_fallback"
                health_status["remoteStateAvailable"] = False
                health_status["remoteStateDirty"] = True
        else:
            print("[State] Discord unavailable and state initialization deferred. Blocked.", file=sys.stderr)
            with health_lock:
                health_status["stateSource"] = "blocked"
                health_status["remoteStateAvailable"] = False
                
            while not discord_available:
                print("[State] Retrying Discord recovery in 15 seconds...", file=sys.stderr)
                time.sleep(15)
                try:
                    remote_state, msg_id = get_state_from_discord(config_instance)
                    discord_available = True
                    if remote_state:
                        state = migrate_state(remote_state)
                        canonical_msg_id = msg_id
                        save_state_locally(config_instance, state)
                        with health_lock:
                            health_status["stateSource"] = "discord"
                            health_status["remoteStateAvailable"] = True
                    else:
                        state = init_fresh_state()
                        
                        # Fetch initial flags to avoid duplicate state posts
                        try:
                            print("[State] Fetching initial flags for fresh state setup...")
                            status, body = fetch_feature_flags_data(config_instance)
                            if status == 200:
                                res_json = json.loads(body)
                                state["last_flags"] = res_json.get("flags", {})
                        except Exception as flag_err:
                            print(f"[State] Warning: Failed to fetch initial flags during setup: {flag_err}", file=sys.stderr)

                        canonical_msg_id = save_state_to_discord(config_instance, state)
                        with health_lock:
                            health_status["stateSource"] = "initialized"
                            health_status["remoteStateAvailable"] = True
                except Exception as retry_err:
                    print(f"[State] Discord retry failed: {retry_err}", file=sys.stderr)

    # 3. Start Flask Web Server in a concurrent thread
    print(f"[Web] Starting Flask Health server on 0.0.0.0:{config_instance.port}...")
    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config_instance.port, debug=False, use_reloader=False),
        daemon=True
    )
    flask_thread.start()

    # 4. Start Discord Bot (Blocking main thread)
    print("[Discord] Launching Discord Gateway connection...")
    sys.stdout.flush()
    bot.run(config_instance.discord_bot_token)

if __name__ == "__main__":
    main()