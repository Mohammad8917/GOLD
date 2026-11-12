"""Offline integration tests for Telegram signal-expiry notifications."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_historical import simulate
from gold_signal.config import Config
from gold_signal.service import run_once
from gold_signal.state import read_outbox, read_state


class TelegramSignalExpiryTests(unittest.TestCase):
    def test_one_day_default_scales_to_timeframe(self) -> None:
        from unittest.mock import patch
        from gold_signal.config import default_signal_ttl_candles
        self.assertEqual(default_signal_ttl_candles("1h"), 24)
        self.assertEqual(default_signal_ttl_candles("15min"), 96)
        self.assertEqual(default_signal_ttl_candles("4h"), 6)

    def make_config(self, folder: str) -> Config:
        return Config(
            "api-key", "bot-token", "chat-id", "1h", 60, 1.5, 1.0, 2.0,
            Path(folder) / "state.json", 5, 2, 0.01, 3.0, 1.5, 3,
        )

    def candles(self, count: int) -> list[dict]:
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        return [{
            "time": start + timedelta(hours=index),
            "open": 2600.0, "high": 2602.0, "low": 2598.0, "close": 2601.0,
        } for index in range(count)]

    def signal(self, stamp: datetime) -> dict:
        return {
            "symbol": "XAU/USD", "side": "BUY", "entry": 2600.0,
            "stop_loss": 2590.0, "take_profit_1": 2610.0,
            "take_profit_2": 2620.0, "atr": 6.0, "rsi": 60.0,
            "ema_fast": 2598.0, "ema_slow": 2590.0, "candle_time": stamp,
        }

    def test_expiry_message_is_sent_on_the_ttlth_closed_candle(self) -> None:
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            first = self.candles(201)
            signal = self.signal(first[-1]["time"])
            with patch("gold_signal.service.fetch_closed_candles", return_value=first), \
                 patch("gold_signal.service.detect_signals", return_value=[signal]), \
                 patch("gold_signal.service.format_signal", return_value="initial signal"), \
                 patch("gold_signal.service.send_telegram", return_value=101):
                self.assertEqual(run_once(config), "sent")

            through_ttl_minus_one = self.candles(203)
            with patch("gold_signal.service.fetch_closed_candles", return_value=through_ttl_minus_one), \
                 patch("gold_signal.service.detect_signals", return_value=[]), \
                 patch("gold_signal.service.send_telegram", return_value=102) as sender:
                run_once(config)
                sender.assert_not_called()

            ttl_candle = self.candles(204)
            with patch("gold_signal.service.fetch_closed_candles", return_value=ttl_candle), \
                 patch("gold_signal.service.detect_signals", return_value=[]), \
                 patch("gold_signal.service.send_telegram", return_value=103) as sender:
                run_once(config)
                sender.assert_called_once()
                message = sender.call_args.args[1]
                self.assertIn("منقضی", message)
                self.assertIn("2601.00", message)
                self.assertIn((ttl_candle[203]["time"] + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"), message)

            # Same candle sequence and TTL must produce the exact same expiry
            # close timestamp in the historical simulator and Telegram notice.
            backtest_signal = {
                "signal_index": 200, "signal_time": first[-1]["time"],
                "side": "BUY", "entry": 2600.0, "stop": 2590.0,
                "tp1": 2610.0, "tp2": 2620.0, "risk": 10.0,
            }
            with patch.dict(os.environ, {"GOLD_TIMEFRAME": "1h"}, clear=False):
                trades, _ = simulate(
                    ttl_candle, [backtest_signal], cost_per_side=0.0,
                    signal_ttl_candles=config.signal_ttl_candles,
                )
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["exit_reason"], "EXPIRED")
            backtest_expiry_display = datetime.fromisoformat(
                trades[0]["exit_time"]
            ).strftime("%Y-%m-%d %H:%M")
            self.assertIn(backtest_expiry_display, message)

            records = read_outbox(config.state_file)
            signal_record = next(
                record for record in records.values()
                if record.get("signal_timestamp") == first[-1]["time"].isoformat()
                and record.get("status") == "SENT"
            )
            self.assertEqual(signal_record["expiry_status"], "SENT")
            self.assertEqual(signal_record["lifecycle_status"], "EXPIRED")
            self.assertEqual(signal_record["expiry_message_id"], 103)
            self.assertEqual(read_state(config.state_file), ttl_candle[-1]["time"].isoformat())


if __name__ == "__main__":
    unittest.main()
