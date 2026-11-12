"""Offline tests for the explicit Telegram connectivity command."""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gold_signal.cli import main


class TelegramCliTests(unittest.TestCase):
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

    def test_connectivity_test_requires_positive_telegram_message_id(self):
        self.assertEqual(self.run_test_command(12345), 0)

    def test_connectivity_test_fails_if_telegram_response_has_no_message_id(self):
        self.assertEqual(self.run_test_command(None), 1)

    def test_connectivity_test_fails_if_message_id_is_not_positive_integer(self):
        self.assertEqual(self.run_test_command(0), 1)


if __name__ == "__main__":
    unittest.main()
