"""Regression tests for historical backtest execution accounting."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest_historical import simulate  # noqa: E402


def candle(index: int, *, open_: float = 100.0, high: float = 100.0,
           low: float = 100.0, close: float = 100.0) -> dict:
    return {
        "time": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index),
        "open": open_, "high": high, "low": low, "close": close,
    }


def signal(index: int = 0) -> dict:
    return {
        "signal_index": index, "signal_time": candle(index)["time"],
        "side": "BUY", "entry": 100.0, "stop": 90.0,
        "tp1": 110.0, "tp2": 120.0, "risk": 10.0,
    }


class HistoricalBacktestExecutionTests(unittest.TestCase):
    def test_entry_and_each_partial_exit_cost_are_charged_once(self) -> None:
        bars = [
            candle(0),
            candle(1, high=111.0, low=99.0),
            candle(2, high=121.0, low=109.0),
        ]
        trades, skipped = simulate(bars, [signal()], cost_per_side=0.20)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(trades), 1)
        # Entry: -0.02R; TP1 half: +0.49R; TP2 half: +0.99R.
        self.assertAlmostEqual(trades[0]["net_r"], 1.46, places=6)
        self.assertTrue(trades[0]["tp1_hit"])
        self.assertEqual(trades[0]["exit_reason"], "TP2")

    def test_favorable_gap_at_target_uses_open_price(self) -> None:
        bars = [candle(0), candle(1, open_=112.0, high=122.0, low=111.0)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.0)
        self.assertEqual(len(trades), 1)
        # Half exits at 112 (+1.2R), half at 122 (+2.2R).
        self.assertAlmostEqual(trades[0]["net_r"], 1.7, places=6)

    def test_gap_through_stop_uses_worse_open_price(self) -> None:
        bars = [candle(0), candle(1, open_=88.0, high=95.0, low=87.0)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.20)
        self.assertEqual(len(trades), 1)
        # -1.2R price loss, plus one entry and one exit cost of 0.02R each.
        self.assertAlmostEqual(trades[0]["net_r"], -1.24, places=6)
        self.assertEqual(trades[0]["exit_reason"], "SL")

    def test_signal_candle_itself_is_never_used_for_exit(self) -> None:
        bars = [candle(0, high=130.0, low=80.0), candle(1)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.0)
        self.assertEqual(trades, [])

    def test_stop_wins_when_stop_and_target_touch_same_bar(self) -> None:
        bars = [candle(0), candle(1, high=125.0, low=85.0)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "SL")
        self.assertAlmostEqual(trades[0]["net_r"], -1.0, places=6)


if __name__ == "__main__":
    unittest.main()
