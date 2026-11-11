"""Tests for chronological signal catch-up and durable processing state."""
from __future__ import annotations
import json, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gold_signal.service import run_once
from gold_signal.state import read_state, write_state

def bars(count: int = 62) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [{"time": start + timedelta(minutes=15*i), "open": 100.0,
             "high": 101.0, "low": 99.0, "close": 100.0} for i in range(count)]

def config(path: Path) -> SimpleNamespace:
    return SimpleNamespace(state_file=path, timeframe="15min",
                           atr_multiplier=1.5, tp1_r=1.0, tp2_r=2.0)

class CatchupTests(unittest.TestCase):
    def test_first_run_only_processes_latest_bar(self):
        candles = bars()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            seen = []
            def detect(prefix, **_kwargs):
                seen.append(prefix[-1]["time"])
                return None
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
                 patch("gold_signal.service.detect_signal", side_effect=detect):
                result = run_once(config(state))
            self.assertEqual(result, "processed_no_signal")
            self.assertEqual(seen, [candles[-1]["time"]])
            self.assertEqual(read_state(state), candles[-1]["time"].isoformat())

    def test_outage_catchup_processes_every_new_bar_in_order(self):
        candles = bars()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            write_state(state, candles[59]["time"])
            seen = []
            def detect(prefix, **_kwargs):
                stamp = prefix[-1]["time"]
                seen.append(stamp)
                if stamp in (candles[60]["time"], candles[61]["time"]):
                    return {"side": "BUY", "candle_time": stamp}
                return None
            with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
                 patch("gold_signal.service.detect_signal", side_effect=detect), \
                 patch("gold_signal.service.send_telegram") as send, \
                 patch("gold_signal.service.format_signal", side_effect=lambda d, _tf: str(d)):
                result = run_once(config(state))
            self.assertEqual(result, "sent_catchup")
            self.assertEqual(seen, [candles[60]["time"], candles[61]["time"]])
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
