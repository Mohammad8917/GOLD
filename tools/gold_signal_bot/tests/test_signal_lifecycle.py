"""Regression tests for Telegram signal lifecycle notifications (no trade execution)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gold_signal.config import Config
from gold_signal.lifecycle import detect_lifecycle_events
from gold_signal.service import run_once
from gold_signal.state import read_outbox


class SignalLifecycleTests(unittest.TestCase):
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

    def seed_signal(self, config: Config, candles: list[dict]) -> None:
        with (
            patch("gold_signal.service.fetch_closed_candles", return_value=candles),
            patch("gold_signal.service.detect_signals", return_value=[self.signal(candles[-1]["time"])]),
            patch("gold_signal.service._signal_is_fresh", return_value=True),
            patch("gold_signal.service.format_signal", return_value="initial signal"),
            patch("gold_signal.service.send_telegram", return_value=100),
            patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
        ):
            self.assertEqual(run_once(config), "sent")

    def run_next(self, config: Config, candles: list[dict], sender):
        with (
            patch("gold_signal.service.fetch_closed_candles", return_value=candles),
            patch("gold_signal.service.detect_signals", return_value=[]),
            patch("gold_signal.service.send_telegram", new=sender),
            patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
        ):
            return run_once(config)

    def only_record(self, config: Config) -> dict:
        return next(iter(read_outbox(config.state_file).values()))

    def test_tp1_notifies_half_position_and_keeps_signal_active(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            self.seed_signal(config, self.candles(201))
            next_bars = self.candles(202)
            next_bars[201].update(high=2611.0, low=2595.0)
            sender = Mock(return_value=101)
            self.run_next(config, next_bars, sender)
            self.assertEqual(sender.call_count, 1)
            self.assertIn("TP1", sender.call_args.args[1])
            self.assertIn("۵۰٪", sender.call_args.args[1])
            record = self.only_record(config)
            self.assertTrue(record["tp1_hit"])
            self.assertEqual(record["lifecycle_status"], "TP1_HIT")
            self.assertEqual([x["event"] for x in record["lifecycle_event_history"]], ["TP1"])

    def test_tp2_on_one_candle_sends_tp1_then_tp2_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            self.seed_signal(config, self.candles(201))
            next_bars = self.candles(202)
            next_bars[201].update(high=2621.0, low=2595.0)
            sender = Mock(side_effect=[101, 102])
            self.run_next(config, next_bars, sender)
            self.assertEqual(sender.call_count, 2)
            messages = [call.args[1] for call in sender.call_args_list]
            self.assertIn("TP1", messages[0])
            self.assertIn("TP2", messages[1])
            record = self.only_record(config)
            self.assertEqual(record["lifecycle_status"], "TP2_COMPLETE")
            self.assertEqual([x["event"] for x in record["lifecycle_event_history"]], ["TP1", "TP2"])

    def test_stop_first_when_stop_and_target_touch_same_candle(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            self.seed_signal(config, self.candles(201))
            next_bars = self.candles(202)
            next_bars[201].update(high=2611.0, low=2589.0)
            sender = Mock(return_value=101)
            self.run_next(config, next_bars, sender)
            self.assertEqual(sender.call_count, 1)
            self.assertIn("SL", sender.call_args.args[1])
            record = self.only_record(config)
            self.assertEqual(record["lifecycle_status"], "SL_CLOSED")
            self.assertFalse(record["tp1_hit"])
            self.assertEqual([x["event"] for x in record["lifecycle_event_history"]], ["SL"])

    def test_stop_after_tp1_does_not_claim_the_total_result_is_a_loss(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            self.seed_signal(config, self.candles(201))
            after_tp1 = self.candles(202)
            after_tp1[201].update(high=2611.0, low=2595.0)
            self.run_next(config, after_tp1, Mock(return_value=101))
            after_stop = self.candles(203)
            after_stop[201].update(high=2611.0, low=2595.0)
            after_stop[202].update(high=2605.0, low=2589.0)
            sender = Mock(return_value=102)
            self.run_next(config, after_stop, sender)
            self.assertIn("لزوماً زیان خالص نیست", sender.call_args.args[1])
            record = self.only_record(config)
            self.assertEqual(record["lifecycle_status"], "SL_CLOSED")
            self.assertEqual([x["event"] for x in record["lifecycle_event_history"]], ["TP1", "SL"])

    def test_ambiguous_lifecycle_send_retries_with_visible_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            self.seed_signal(config, self.candles(201))
            next_bars = self.candles(202)
            next_bars[201].update(high=2611.0, low=2595.0)
            with self.assertRaises(TimeoutError):
                self.run_next(config, next_bars, Mock(side_effect=TimeoutError("timeout")))
            record = self.only_record(config)
            self.assertEqual(record["lifecycle_notice"]["status"], "UNKNOWN")
            sender = Mock(return_value=103)
            self.run_next(config, next_bars, sender)
            self.assertIn("[RECOVERY RETRY]", sender.call_args.args[1])
            self.assertEqual(self.only_record(config)["lifecycle_status"], "TP1_HIT")

    def test_sent_signal_catchup_replays_multiple_candles_and_keeps_stop_first(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            initial = self.candles(201)
            self.seed_signal(config, initial)
            replay = self.candles(204)
            # First missed candle touches both SL and TP1: conservative SL-first
            # ordering must close the already-SENT signal exactly once.
            replay[201].update(high=2611.0, low=2589.0)
            # A later missed candle reaches TP2, but a signal already closed by
            # the earlier SL must not emit any additional lifecycle event.
            replay[202].update(high=2625.0, low=2600.0)
            replay[203].update(high=2628.0, low=2600.0)
            sender = Mock(return_value=104)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=replay),
                patch("gold_signal.service.detect_signals", return_value=[]),
                patch("gold_signal.service.send_telegram", new=sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
            ):
                result = run_once(config)

            self.assertEqual(result, "lifecycle_updated")
            self.assertEqual(sender.call_count, 1)
            self.assertIn("SL", sender.call_args.args[1])
            record = self.only_record(config)
            self.assertEqual(record["status"], "SENT")
            self.assertEqual(record["lifecycle_status"], "SL_CLOSED")
            self.assertEqual(
                [event["event"] for event in record["lifecycle_event_history"]],
                ["SL"],
            )

    def test_lifecycle_rejects_impossible_ohlc_range(self):
        record = {
            "side": "BUY", "stop_loss": 2590.0,
            "take_profit_1": 2610.0, "take_profit_2": 2620.0,
            "tp1_hit": False,
        }
        with self.assertRaisesRegex(RuntimeError, "candle high must be >= low"):
            detect_lifecycle_events(record, {"high": 2580.0, "low": 2600.0})

    def test_sell_side_uses_mirrored_stop_and_target_levels(self):
        record = {
            "side": "SELL", "stop_loss": 2610.0,
            "take_profit_1": 2590.0, "take_profit_2": 2580.0,
            "tp1_hit": False,
        }
        self.assertEqual(
            detect_lifecycle_events(record, {"high": 2605.0, "low": 2589.0}),
            ["TP1"],
        )
        self.assertEqual(
            detect_lifecycle_events(record, {"high": 2611.0, "low": 2579.0}),
            ["SL"],
        )


if __name__ == "__main__":
    unittest.main()
