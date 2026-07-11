import unittest
from unittest.mock import patch, MagicMock
import json
import time
import os
import sys
import datetime

# Set up dummy environment variables before importing poke_monitor
os.environ["DISCORD_BOT_TOKEN"] = "mock_bot_token"
os.environ["DISCORD_STATE_CHANNEL_ID"] = "1234567890"
os.environ["POKE_SESSION_TOKEN"] = "mock_session_token"
os.environ["POKE_FLAGS_PAYLOAD"] = "mock_flags_payload"
os.environ["FLAGS_WEBHOOK_URL"] = "https://discord.com/api/webhooks/mock1"
os.environ["CREDIT_WEBHOOK_URL"] = "https://discord.com/api/webhooks/mock2"

import poke_monitor

class TestPokeMonitor(unittest.TestCase):

    def setUp(self):
        self.config = poke_monitor.Config()
        poke_monitor.state = poke_monitor.init_fresh_state()
        poke_monitor.canonical_msg_id = None
        poke_monitor.remote_state_dirty = False

    def test_schema_migration(self):
        # Version 0 state
        legacy_state = {
            "last_flags": {"test-flag": {"key": "test-flag", "enabled": True}},
            "token_expiry_notified": True
        }
        migrated = poke_monitor.migrate_state(legacy_state)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["last_flags"]["test-flag"]["enabled"], True)
        self.assertTrue(migrated["token_expiry_notified"])
        self.assertIn("usage_history", migrated)
        self.assertEqual(migrated["usage_history"], [])

    @patch("poke_monitor.requests.post")
    def test_send_webhook_safely_splitting(self, mock_post):
        # Setup mock response
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_post.return_value = mock_response

        # Test splitting of fields when they are too long or too many
        fields = [
            {"name": f"Field {i}", "value": "A" * 1200, "inline": False}
            for i in range(25)
        ]
        
        success = poke_monitor.send_webhook_safely(
            self.config.flags_webhook_url,
            "Test Splitting",
            "Description text",
            12345,
            fields
        )
        self.assertTrue(success)
        # It should split into multiple embeds and post them. Let's verify requests were made.
        self.assertTrue(mock_post.called)

    @patch("poke_monitor.make_request")
    def test_get_state_from_discord_success(self, mock_make_request):
        # 1. Mock channel history response
        mock_history = [
            {
                "id": "msg_999",
                "content": "POKE_MONITOR_STATE_V2",
                "attachments": [
                    {
                        "url": "https://cdn.discordapp.com/attachments/123/state.json"
                    }
                ]
            }
        ]
        # 2. Mock state file response
        mock_state_json = {
            "schema_version": 2,
            "last_flags": {"some-flag": {"enabled": True}},
            "usage_history": []
        }

        def mock_req(url, *args, **kwargs):
            if "messages" in url:
                return 200, json.dumps(mock_history)
            elif "attachments" in url:
                return 200, json.dumps(mock_state_json)
            return 404, "Not Found"

        mock_make_request.side_effect = mock_req

        state, msg_id = poke_monitor.get_state_from_discord(self.config)
        self.assertIsNotNone(state)
        self.assertEqual(msg_id, "msg_999")
        self.assertTrue(state["last_flags"]["some-flag"]["enabled"])

    @patch("poke_monitor.make_request")
    def test_get_state_from_discord_malformed_fallback(self, mock_make_request):
        # First message is malformed, second is valid
        mock_history = [
            {
                "id": "msg_1",
                "content": "POKE_MONITOR_STATE_V2",
                "attachments": [{"url": "https://cdn.discordapp.com/attachments/123/state_malformed.json"}]
            },
            {
                "id": "msg_2",
                "content": "POKE_MONITOR_STATE_V2",
                "attachments": [{"url": "https://cdn.discordapp.com/attachments/123/state_valid.json"}]
            }
        ]
        mock_state_valid = {
            "schema_version": 2,
            "last_flags": {"some-flag": {"enabled": False}},
            "usage_history": []
        }

        def mock_req(url, *args, **kwargs):
            if "messages" in url:
                return 200, json.dumps(mock_history)
            elif "state_malformed.json" in url:
                return 200, "{malformed json..."
            elif "state_valid.json" in url:
                return 200, json.dumps(mock_state_valid)
            return 404, "Not Found"

        mock_make_request.side_effect = mock_req

        state, msg_id = poke_monitor.get_state_from_discord(self.config)
        self.assertIsNotNone(state)
        self.assertEqual(msg_id, "msg_2")
        self.assertFalse(state["last_flags"]["some-flag"]["enabled"])

    @patch("poke_monitor.make_request")
    def test_usage_replenishment_detection(self, mock_make_request):
        # Setup mock usage status call
        mock_response = {
            "baseUsage": {
                "present": True,
                "usedPercent": 57.50, # Dropped from 71.78
                "resetsAt": 1786260352000
            }
        }
        mock_make_request.return_value = (200, json.dumps(mock_response))

        # Seed usage history with high usage
        now = time.time()
        poke_monitor.state["usage_history"] = [
            {"timestamp": poke_monitor.get_iso_now(), "used_percent": 71.78, "resets_at": 1786260352000}
        ]
        poke_monitor.state["usage_warning_active"] = True
        poke_monitor.state["usage_warning_below_threshold_count"] = 2

        # Check balance/usage
        poke_monitor.sample_usage_and_balance(self.config)

        # Confirm history was reset (only the newest sample remains)
        history = poke_monitor.state["usage_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["used_percent"], 57.50)
        # Warning states should be reset
        self.assertFalse(poke_monitor.state["usage_warning_active"])
        self.assertEqual(poke_monitor.state["usage_warning_below_threshold_count"], 0)

    @patch("poke_monitor.send_webhook_safely")
    @patch("poke_monitor.make_request")
    def test_usage_velocity_warning(self, mock_make_request, mock_send_webhook):
        mock_send_webhook.return_value = True

        # Setup standard usage warning condition
        # Rises from 40.0% to 41.5% in 10 minutes (velocity = 0.15 pp/min)
        now = time.time()
        ten_mins_ago = now - 600
        
        # Populate history
        history = []
        for i in range(11):
            ts = ten_mins_ago + (i * 60)
            # Linearly increase from 40.0 to 41.5
            pct = 40.0 + (i * 0.15)
            dt_str = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
            history.append({"timestamp": dt_str, "used_percent": pct, "resets_at": 1786260352000})

        poke_monitor.state["usage_history"] = history
        
        # Mock final request (current sample matches the last element of history)
        mock_response = {
            "baseUsage": {
                "present": True,
                "usedPercent": 41.50,
                "resetsAt": 1786260352000
            }
        }
        mock_make_request.return_value = (200, json.dumps(mock_response))

        # Sample usage and check warning triggers
        poke_monitor.sample_usage_and_balance(self.config)

        # Confirm warning became active and webhook was called
        self.assertTrue(poke_monitor.state["usage_warning_active"])
        self.assertEqual(poke_monitor.state["last_usage_warning_percent"], 41.50)
        self.assertAlmostEqual(poke_monitor.state["last_usage_warning_velocity"], 0.15, places=3)
        self.assertTrue(mock_send_webhook.called)

    @patch("poke_monitor.send_webhook_safely")
    @patch("poke_monitor.make_request")
    def test_usage_warning_cooldown_and_realert(self, mock_make_request, mock_send_webhook):
        mock_send_webhook.return_value = True

        now = time.time()
        # Seed warning state as active
        poke_monitor.state["usage_warning_active"] = True
        poke_monitor.state["last_usage_warning_at"] = now - 500  # within cooldown (900s)
        poke_monitor.state["last_usage_warning_percent"] = 40.0
        poke_monitor.state["last_usage_warning_velocity"] = 0.15

        # Try to trigger another warning inside cooldown - should be blocked
        history = [
            {"timestamp": datetime.datetime.fromtimestamp(now - 600, datetime.timezone.utc).isoformat(), "used_percent": 38.0},
            {"timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(), "used_percent": 41.0}
        ]
        poke_monitor.state["usage_history"] = history
        mock_make_request.return_value = (200, json.dumps({
            "baseUsage": {"present": True, "usedPercent": 41.0, "resetsAt": 1786260352000}
        }))

        poke_monitor.sample_usage_and_balance(self.config)
        self.assertEqual(poke_monitor.state["last_usage_warning_at"], now - 500) # unchanged

        # Fast forward time to pass cooldown (900s)
        poke_monitor.state["last_usage_warning_at"] = now - 1000 # outside cooldown

        # Check realert condition 1: usage increase >= USAGE_WARNING_REALERT_DELTA_PP (2.0)
        # Warned at 40.0%, now at 42.1% (+2.1% delta since last warning)
        history = [
            {"timestamp": datetime.datetime.fromtimestamp(now - 600, datetime.timezone.utc).isoformat(), "used_percent": 40.0},
            {"timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(), "used_percent": 42.1}
        ]
        poke_monitor.state["usage_history"] = history
        mock_make_request.return_value = (200, json.dumps({
            "baseUsage": {"present": True, "usedPercent": 42.1, "resetsAt": 1786260352000}
        }))

        poke_monitor.sample_usage_and_balance(self.config)
        self.assertAlmostEqual(poke_monitor.state["last_usage_warning_percent"], 42.1)

    @patch("poke_monitor.send_webhook_safely")
    @patch("poke_monitor.make_request")
    def test_usage_warning_below_threshold_clears_active(self, mock_make_request, mock_send_webhook):
        mock_send_webhook.return_value = True

        poke_monitor.state["usage_warning_active"] = True
        poke_monitor.state["usage_warning_below_threshold_count"] = 2

        # 3rd consecutive sample below threshold: should set active=False
        now = time.time()
        history = [
            {"timestamp": datetime.datetime.fromtimestamp(now - 600, datetime.timezone.utc).isoformat(), "used_percent": 40.0},
            {"timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(), "used_percent": 40.1} # very slow rise
        ]
        poke_monitor.state["usage_history"] = history
        mock_make_request.return_value = (200, json.dumps({
            "baseUsage": {"present": True, "usedPercent": 40.1, "resetsAt": 1786260352000}
        }))

        poke_monitor.sample_usage_and_balance(self.config)
        self.assertFalse(poke_monitor.state["usage_warning_active"])
        self.assertEqual(poke_monitor.state["usage_warning_below_threshold_count"], 0)

    @patch("poke_monitor.requests.post")
    @patch("poke_monitor.make_request")
    def test_token_expiration_warning_flow(self, mock_make_request, mock_post):
        mock_make_request.return_value = (401, "Unauthorized")

        # Initial expiry check
        poke_monitor.state["token_expiry_notified"] = False
        poke_monitor.sample_usage_and_balance(self.config)

        self.assertTrue(poke_monitor.state["token_expiry_notified"])
        self.assertTrue(mock_post.called) # Alert sent

        # Second expiry check - should not repeat alert
        mock_post.reset_mock()
        poke_monitor.sample_usage_and_balance(self.config)
        self.assertFalse(mock_post.called)

        # Recovery check
        mock_make_request.return_value = (200, json.dumps({
            "baseUsage": {"present": True, "usedPercent": 40.0, "resetsAt": 1786260352000}
        }))
        poke_monitor.sample_usage_and_balance(self.config)
        self.assertFalse(poke_monitor.state["token_expiry_notified"])
        self.assertTrue(mock_post.called) # Recovery alert sent

if __name__ == "__main__":
    unittest.main()
