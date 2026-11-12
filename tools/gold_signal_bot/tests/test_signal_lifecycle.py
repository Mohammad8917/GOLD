"""Regression tests for Telegram signal lifecycle notifications (no trade execution)."""
from __future__ import annotations

import os
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
from gold_signal.state import read_outbox, write_outbox, write_state


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

            self.assertEqual(result, "sent_catchup")
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


    def test_twenty_stale_signals_create_one_silent_durable_summary_and_no_signal_posts(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Config(
                "api-key", "bot-token", "chat-id", "1h", 60, 1.5, 1.0, 2.0,
                Path(folder) / "state.json", 5, 2, 0.01, 3.0, 1.5, 1000,
            )
            initial = self.candles(201)
            self.seed_signal(config, initial)
            replay = self.candles(221)
            stale_signals = [self.signal(replay[index]["time"]) for index in range(201, 221)]
            sender = Mock(return_value=501)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=replay),
                patch("gold_signal.service.detect_signals", return_value=stale_signals),
                patch("gold_signal.service.send_telegram", new=sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
                patch("gold_signal.service._signal_is_fresh", return_value=False),
            ):
                result = run_once(config)

            self.assertEqual(result, "stale_notice_sent")
            self.assertEqual(sender.call_count, 1)
            self.assertTrue(sender.call_args.kwargs["disable_notification"])
            summary = sender.call_args.args[1]
            self.assertIn("20 سیگنال", summary)
            self.assertIn("2026-11-09 09:00", summary)
            self.assertIn("2026-11-10 04:00", summary)
            records = read_outbox(config.state_file)
            stale = [record for record in records.values() if record.get("status") == "SKIPPED_STALE"]
            self.assertEqual(len(stale), 20)
            self.assertTrue(all(record["stale_notice_status"] == "SENT" for record in stale))
            self.assertTrue(all(record["lifecycle_status"] == "CLOSED_STALE" for record in stale))

            # A second process invocation after the durable SENT write must not resend.
            restart_sender = Mock(return_value=502)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=replay),
                patch("gold_signal.service.detect_signals", return_value=stale_signals),
                patch("gold_signal.service.send_telegram", new=restart_sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
            ):
                run_once(config)
            self.assertEqual(restart_sender.call_count, 0)

    def test_stale_notice_unknown_recovers_visibly_without_trading_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            candles = self.candles(201)
            boundary = candles[-1]["time"]
            write_state(config.state_file, boundary)
            signal_id = "a" * 64
            write_outbox(config.state_file, {
                signal_id: {
                    "signal_id": signal_id,
                    "signal_timestamp": candles[-2]["time"].isoformat(),
                    "timeframe": config.timeframe,
                    "status": "SKIPPED_STALE",
                    "lifecycle_status": "CLOSED_STALE",
                    "stale_notice_group": boundary.isoformat(),
                    "stale_notice_status": "SENDING",
                    "stale_notice_attempt_count": 1,
                    "stale_notice_message_id": None,
                    "stale_notice_error": None,
                }
            })

            sender = Mock(return_value=700)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=candles),
                patch("gold_signal.service.detect_signals", return_value=[]),
                patch("gold_signal.service.send_telegram", new=sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
            ):
                result = run_once(config)

            self.assertEqual(result, "stale_notice_sent")
            self.assertEqual(sender.call_count, 1)
            message = sender.call_args.args[1]
            self.assertIn("[RECOVERY RETRY]", message)
            self.assertIn("STALE SIGNALS SKIPPED", message)
            self.assertTrue(sender.call_args.kwargs["disable_notification"])
            record = self.only_record(config)
            self.assertEqual(record["status"], "SKIPPED_STALE")
            self.assertEqual(record["lifecycle_status"], "CLOSED_STALE")
            self.assertEqual(record["stale_notice_status"], "SENT")
            self.assertEqual(record["stale_notice_message_id"], 700)

            # Once recovered to SENT, another restart must not resend anything.
            second_sender = Mock(return_value=701)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=candles),
                patch("gold_signal.service.detect_signals", return_value=[]),
                patch("gold_signal.service.send_telegram", new=second_sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
            ):
                run_once(config)
            self.assertEqual(second_sender.call_count, 0)

    def test_failed_stale_summary_retries_at_next_run_without_duplicate_group(self):
        with tempfile.TemporaryDirectory() as folder:
            config = self.make_config(folder)
            initial = self.candles(201)
            self.seed_signal(config, initial)
            replay = self.candles(202)
            stale_signal = [self.signal(replay[201]["time"])]

            failed_sender = Mock(side_effect=RuntimeError("Telegram rejected stale summary (HTTP 400)"))
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=replay),
                patch("gold_signal.service.detect_signals", return_value=stale_signal),
                patch("gold_signal.service.send_telegram", new=failed_sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
                patch("gold_signal.service._signal_is_fresh", return_value=False),
            ):
                result = run_once(config)

            self.assertEqual(result, "processed_no_signal")
            self.assertEqual(failed_sender.call_count, 1)
            first_message = failed_sender.call_args.args[1]
            self.assertTrue(failed_sender.call_args.kwargs["disable_notification"])
            self.assertIn("1 سیگنال", first_message)
            record = self.only_record(config)
            self.assertEqual(record["status"], "SKIPPED_STALE")
            self.assertEqual(record["lifecycle_status"], "CLOSED_STALE")
            self.assertEqual(record["stale_notice_status"], "FAILED")

            retry_sender = Mock(return_value=601)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=replay),
                patch("gold_signal.service.detect_signals", return_value=[]),
                patch("gold_signal.service.send_telegram", new=retry_sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
            ):
                result = run_once(config)

            self.assertEqual(result, "stale_notice_sent")
            self.assertEqual(retry_sender.call_count, 1)
            self.assertEqual(retry_sender.call_args.args[1], first_message)
            self.assertTrue(retry_sender.call_args.kwargs["disable_notification"])
            self.assertNotIn("[RECOVERY RETRY]", retry_sender.call_args.args[1])
            self.assertEqual(self.only_record(config)["stale_notice_status"], "SENT")

            # A later restart/run must not create a second notice for this range.
            final_sender = Mock(return_value=602)
            with (
                patch("gold_signal.service.fetch_closed_candles", return_value=replay),
                patch("gold_signal.service.detect_signals", return_value=[]),
                patch("gold_signal.service.send_telegram", new=final_sender),
                patch("gold_signal.service.maybe_send_daily_quota_warning", return_value=False),
            ):
                run_once(config)
            self.assertEqual(final_sender.call_count, 0)

    def test_freshness_grace_is_configurable_and_validated(self):
        env = {
            "GOLD_TWELVE_DATA_API_KEY": "api",
            "GOLD_TELEGRAM_BOT_TOKEN": "token",
            "GOLD_TELEGRAM_CHAT_ID": "chat",
            "GOLD_SIGNAL_FRESHNESS_GRACE_SECONDS": "123",
            "GOLD_SIGNAL_STATE_FILE": str(Path(tempfile.gettempdir()) / "gold-config-test-state.json"),
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Config.from_env().freshness_grace_seconds, 123)
        env["GOLD_SIGNAL_FRESHNESS_GRACE_SECONDS"] = "-1"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "FRESHNESS_GRACE_SECONDS"):
                Config.from_env()



if __name__ == "__main__":
    unittest.main()
