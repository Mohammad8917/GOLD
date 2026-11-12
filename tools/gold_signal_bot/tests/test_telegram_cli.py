"""Offline tests for the explicit Telegram connectivity command."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gold_signal.cli import main, _run_current_candle_with_retry
from gold_signal.config import Config


class TelegramCliTests(unittest.TestCase):
    class NoWaitEvent:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return False

    def run_test_command(self, message_id):
        config = SimpleNamespace(api_key="", bot_token="test-token")
        with (
            patch("gold_signal.cli.load_dotenv"),
            patch("gold_signal.cli.Config.from_env", return_value=config) as from_env,
            patch("gold_signal.cli.send_telegram", return_value=message_id),
            patch.object(sys, "argv", ["gold_signal_bot.py", "--test-telegram"]),
        ):
            result = main()
        from_env.assert_called_once_with(require_market_data=False)
        return result

    def make_retry_config(self):
        return Config(
            "api", "bot", "chat", "15min", 45, 1.5, 1.0, 2.0,
            Path("state.json"), 5, 3, 1.0, 3.0, 1.5, 3, 4,
        )

    def test_current_candle_retry_stops_when_new_closed_candle_is_processed(self):
        config = self.make_retry_config()
        import time
        retry_clock = int(time.time() // 900) * 900 + 4
        with patch("time.time", return_value=retry_clock), patch(
            "gold_signal.cli.run_once",
            side_effect=["no_signal", "processed_no_signal"],
        ) as run:
            result = _run_current_candle_with_retry(config, self.NoWaitEvent())
        self.assertEqual(result, "processed_no_signal")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0].retry_attempts == 1 for call in run.call_args_list))

    def test_current_candle_retry_has_a_hard_five_request_budget(self):
        config = self.make_retry_config()
        import time
        retry_clock = int(time.time() // 900) * 900 + 4
        with patch("time.time", return_value=retry_clock), patch(
            "gold_signal.cli.run_once", return_value="no_signal"
        ) as run:
            result = _run_current_candle_with_retry(config, self.NoWaitEvent())
        self.assertEqual(result, "no_signal")
        self.assertEqual(run.call_count, 5)
        self.assertTrue(all(call.args[0].retry_attempts == 1 for call in run.call_args_list))

    def test_connectivity_test_requires_positive_telegram_message_id(self):
        self.assertEqual(self.run_test_command(12345), 0)

    def test_connectivity_test_fails_if_telegram_response_has_no_message_id(self):
        self.assertEqual(self.run_test_command(None), 1)

    def test_connectivity_test_fails_if_message_id_is_not_positive_integer(self):
        self.assertEqual(self.run_test_command(0), 1)


if __name__ == "__main__":
    unittest.main()
