"""Crash/restart tests for the Telegram delivery write-ahead log."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gold_signal.service import run_once
from gold_signal.state import read_outbox, read_state, outbox_path, write_outbox, write_state


STRATEGY_VERSION = "ema20-ema50-rsi14-atr14-v1"


def make_bars(count: int = 202) -> list[dict]:
    start = datetime(2026, 11, 10, tzinfo=timezone.utc)
    return [
        {
            "time": start + timedelta(minutes=15 * index),
            "open": 2600.0,
            "high": 2602.0,
            "low": 2598.0,
            "close": 2601.0,
        }
        for index in range(count)
    ]


def make_config(path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        state_file=path,
        timeframe="15min",
        atr_multiplier=1.5,
        tp1_r=1.0,
        tp2_r=2.0,
        api_key="test-api-key",
        bot_token="test-bot-token",
        chat_id="test-chat",
        http_timeout=5,
    )


def make_signal(stamp: datetime) -> dict:
    return {
        "symbol": "XAU/USD",
        "side": "BUY",
        "entry": 2600.0,
        "stop_loss": 2590.0,
        "take_profit_1": 2610.0,
        "take_profit_2": 2620.0,
        "atr": 6.0,
        "rsi": 60.0,
        "ema_fast": 2598.0,
        "ema_slow": 2590.0,
        "candle_time": stamp,
    }


def signal_key(signal: dict, timeframe: str = "15min") -> str:
    identity = (
        f'{signal["symbol"]}|{timeframe}|{signal["candle_time"].isoformat()}|'
        f"{STRATEGY_VERSION}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class DeliveryWalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "state.json"
        self.config = make_config(self.state_path)
        self.candles = make_bars()
        self.signal = make_signal(self.candles[-1]["time"])

    def tearDown(self):
        self.temp.cleanup()

    def run_with_signal(self):
        return patch.multiple(
            "gold_signal.service",
            fetch_closed_candles=lambda *_args, **_kwargs: self.candles,
            detect_signals=lambda *_args, **_kwargs: [self.signal],
            format_signal=lambda *_args, **_kwargs: "test signal message",
        )

    def test_ambiguous_timeout_keeps_cursor_and_retries_with_visible_marker(self):
        accepted_by_telegram = []

        def accepted_then_response_lost(*_args, **_kwargs):
            accepted_by_telegram.append("telegram accepted message")
            raise requests.Timeout("response lost after remote acceptance")

        with self.run_with_signal(), patch(
            "gold_signal.service.send_telegram",
            side_effect=accepted_then_response_lost,
        ):
            with self.assertRaises(requests.Timeout):
                run_once(self.config)

        self.assertEqual(accepted_by_telegram, ["telegram accepted message"])
        self.assertEqual(read_state(self.state_path), "")
        records = read_outbox(self.state_path)
        key = signal_key(self.signal)
        self.assertEqual(records[key]["status"], "UNKNOWN")
        self.assertEqual(records[key]["attempt_count"], 1)
        self.assertIn("response lost", records[key]["last_error"])

        with self.run_with_signal(), patch(
            "gold_signal.service.send_telegram", return_value=987,
        ) as sender:
            result = run_once(self.config)

        self.assertEqual(result, "sent")
        self.assertTrue(sender.call_args.args[1].startswith("<b>[RECOVERY RETRY]</b>"))
        self.assertEqual(read_state(self.state_path), self.candles[-1]["time"].isoformat())
        self.assertEqual(read_outbox(self.state_path), {})

    def test_stale_sending_record_is_classified_unknown_and_audited(self):
        key = signal_key(self.signal)
        write_outbox(self.state_path, {
            key: {
                "signal_id": key,
                "signal_timestamp": self.signal["candle_time"].isoformat(),
                "symbol": "XAU/USD",
                "timeframe": "15min",
                "status": "SENDING",
                "attempt_count": 1,
                "message": "original signal message",
                "telegram_message_id": None,
                "last_error": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        })

        with self.run_with_signal(), patch(
            "gold_signal.service.send_telegram", return_value=988,
        ) as sender, self.assertLogs("gold_signal_bot", level="ERROR") as captured:
            result = run_once(self.config)

        self.assertEqual(result, "sent")
        self.assertTrue(sender.call_args.args[1].startswith("<b>[RECOVERY RETRY]</b>"))
        self.assertTrue(any("status is UNKNOWN" in line for line in captured.output))
        self.assertTrue(any("decision: retry" in line for line in captured.output))

    def test_durable_sent_record_advances_cursor_without_resending(self):
        key = signal_key(self.signal)
        write_outbox(self.state_path, {
            key: {
                "signal_id": key,
                "signal_timestamp": self.signal["candle_time"].isoformat(),
                "symbol": "XAU/USD",
                "timeframe": "15min",
                "status": "SENT",
                "attempt_count": 1,
                "message": "original signal message",
                "telegram_message_id": 1234,
                "last_error": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        })

        with self.run_with_signal(), patch(
            "gold_signal.service.send_telegram",
        ) as sender:
            result = run_once(self.config)

        self.assertEqual(result, "processed_no_signal")
        sender.assert_not_called()
        self.assertEqual(read_state(self.state_path), self.candles[-1]["time"].isoformat())
        self.assertEqual(read_outbox(self.state_path), {})

    def test_corrupt_wal_fails_closed_instead_of_becoming_empty(self):
        outbox_path(self.state_path).write_text("{broken", encoding="utf-8")
        with patch(
            "gold_signal.service.fetch_closed_candles", return_value=self.candles,
        ):
            with self.assertRaisesRegex(RuntimeError, "Delivery WAL is unreadable"):
                run_once(self.config)
        self.assertEqual(read_state(self.state_path), "")

    def test_initial_startup_logs_number_of_intentionally_ignored_bars(self):
        with patch(
            "gold_signal.service.fetch_closed_candles", return_value=self.candles,
        ), patch(
            "gold_signal.service.detect_signals", return_value=[],
        ), self.assertLogs("gold_signal_bot", level="INFO") as captured:
            result = run_once(self.config)

        self.assertEqual(result, "processed_no_signal")
        expected = len(self.candles) - 1
        self.assertTrue(any(
            f"{expected} previous candles intentionally ignored" in line
            for line in captured.output
        ))


if __name__ == "__main__":
    unittest.main()
