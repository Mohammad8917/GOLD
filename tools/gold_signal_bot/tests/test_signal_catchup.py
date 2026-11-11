"""Tests for chronological signal catch-up and durable processing state."""
from __future__ import annotations
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gold_signal.service import run_once, INDICATOR_WARMUP_BARS
from gold_signal.state import read_state, write_state

def bars(count: int = 202) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"time": start + timedelta(minutes=15*i), "open": 100.0,
             "high": 101.0, "low": 99.0, "close": 100.0} for i in range(count)]

def config(path: Path) -> SimpleNamespace:
    return SimpleNamespace(state_file=path, timeframe="15min",
                           atr_multiplier=1.5, tp1_r=1.0, tp2_r=2.0)

class CatchupTests(unittest.TestCase):
    def test_first_run_only_processes_latest_bar_with_full_warmup(self):
        candles = bars()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
                 patch("gold_signal.service.detect_signals", return_value=[]) as detect:
                result = run_once(config(state))
            self.assertEqual(result, "processed_no_signal")
            detect.assert_called_once()
            self.assertEqual(read_state(state), candles[-1]["time"].isoformat())

    def test_first_run_fails_closed_when_warmup_is_short(self):
        candles = bars(INDICATOR_WARMUP_BARS)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
                 patch("gold_signal.service.detect_signals") as detect:
                with self.assertRaisesRegex(RuntimeError, "Insufficient indicator warm-up history"):
                    run_once(config(state))
            detect.assert_not_called()
            self.assertEqual(read_state(state), "")

    def test_catchup_fails_closed_when_cursor_leaves_fewer_than_200_prior_bars(self):
        candles = bars()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            # The next candle would be index 199, with only 199 prior candles.
            write_state(state, candles[198]["time"])
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
                 patch("gold_signal.service.detect_signals") as detect:
                with self.assertRaisesRegex(RuntimeError, "need 200 prior candles, have 199"):
                    run_once(config(state))
            detect.assert_not_called()
            self.assertEqual(read_state(state), candles[198]["time"].isoformat())

    def test_outage_catchup_processes_every_new_bar_in_order(self):
        candles = bars()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            write_state(state, candles[199]["time"])
            signals = [
                {"side": "BUY", "candle_time": candles[200]["time"]},
                {"side": "SELL", "candle_time": candles[201]["time"]},
            ]
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
                 patch("gold_signal.service.detect_signals", return_value=signals), \
                 patch("gold_signal.service.send_telegram") as send, \
                 patch("gold_signal.service.format_signal", side_effect=lambda d, _tf: str(d)):
                result = run_once(config(state))
            self.assertEqual(result, "sent_catchup")
            self.assertEqual(send.call_count, 2)
            self.assertEqual(read_state(state), candles[-1]["time"].isoformat())

    def test_unavailable_history_fails_closed_instead_of_skipping(self):
        candles = bars()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            write_state(state, candles[0]["time"] - timedelta(minutes=15))
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles):
                with self.assertRaisesRegex(RuntimeError, "older than available"):
                    run_once(config(state))

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "refusing to reset"):
                read_state(state)

    def test_legacy_signal_cursor_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
            state.write_text(json.dumps({"last_signal_candle": stamp.isoformat()}), encoding="utf-8")
            self.assertEqual(read_state(state), stamp.isoformat())

if __name__ == "__main__":
    unittest.main()
