"""TDD regression tests for configurable signal lifetime in historical simulation."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_historical import simulate  # noqa: E402


def bar(index: int, *, close: float = 104.0, high: float = 105.0,
        low: float = 99.0) -> dict:
    return {
        "time": datetime(2026, 2, 1, tzinfo=timezone.utc) + timedelta(hours=index),
        "open": 100.0, "high": high, "low": low, "close": close,
    }


def buy_signal() -> dict:
    return {
        "signal_index": 0,
        "signal_time": bar(0)["time"],
        "side": "BUY",
        "entry": 100.0,
        "stop": 90.0,
        "tp1": 110.0,
        "tp2": 120.0,
        "risk": 10.0,
    }


class SignalTtlBacktestTests(unittest.TestCase):
    def test_default_ttl_expires_at_the_24th_following_closed_candle_close(self) -> None:
        bars = [bar(index) for index in range(26)]
        with patch.dict(os.environ, {"GOLD_TIMEFRAME": "1h"}, clear=False):
            trades, skipped = simulate(bars, [buy_signal()], cost_per_side=0.0)

        self.assertEqual(skipped, 0)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade["exit_reason"], "EXPIRED")
        self.assertEqual(trade["exit_time"], (bars[24]["time"] + timedelta(hours=1)).isoformat())
        self.assertEqual(trade["bars_held"], 24)
        self.assertAlmostEqual(trade["net_r"], 0.4, places=6)

    def test_custom_ttl_is_respected(self) -> None:
        bars = [bar(index, close=102.0) for index in range(8)]
        with patch.dict(os.environ, {"GOLD_TIMEFRAME": "1h"}, clear=False):
            trades, _ = simulate(
                bars, [buy_signal()], cost_per_side=0.0, signal_ttl_candles=5
            )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "EXPIRED")
        self.assertEqual(trades[0]["exit_time"], (bars[5]["time"] + timedelta(hours=1)).isoformat())
        self.assertEqual(trades[0]["bars_held"], 5)

    def test_stop_or_target_before_expiry_takes_precedence(self) -> None:
        bars = [bar(index) for index in range(27)]
        bars[7] = bar(7, close=89.0, high=100.0, low=88.0)
        trades, _ = simulate(bars, [buy_signal()], cost_per_side=0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "SL")
        self.assertEqual(trades[0]["exit_time"], bars[7]["time"].isoformat())
        self.assertEqual(trades[0]["bars_held"], 7)


if __name__ == "__main__":
    unittest.main()
