"""Regression tests for explicit open-position reporting in the backtest."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_historical import simulate, summarize  # noqa: E402


def candle(index: int, *, close: float = 104.0, high: float = 105.0,
           low: float = 99.0) -> dict:
    return {
        "time": datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(hours=index),
        "open": 100.0, "high": high, "low": low, "close": close,
    }


def signal() -> dict:
    return {
        "signal_index": 0, "signal_time": candle(0)["time"], "side": "BUY",
        "entry": 100.0, "stop": 90.0, "tp1": 110.0, "tp2": 120.0,
        "risk": 10.0,
    }


class OpenPositionReportingTests(unittest.TestCase):
    def test_unfinished_position_is_reported_separately_from_completed_trades(self) -> None:
        bars = [candle(i) for i in range(6)]
        open_positions = []
        with patch.dict(os.environ, {"GOLD_TIMEFRAME": "1h"}, clear=False):
            trades, skipped = simulate(
                bars, [signal()], 0.0, signal_ttl_candles=24,
                open_positions=open_positions,
            )
        self.assertEqual(trades, [])
        self.assertEqual(skipped, 0)
        self.assertEqual(len(open_positions), 1)
        position = open_positions[0]
        self.assertEqual(position["side"], "BUY")
        self.assertEqual(position["bars_held"], 5)
        self.assertEqual(position["remaining_fraction"], 1.0)
        self.assertEqual(position["mark_price"], 104.0)
        self.assertAlmostEqual(position["realized_r"], 0.0, places=6)
        self.assertAlmostEqual(position["unrealized_r"], 0.4, places=6)
        self.assertAlmostEqual(position["total_marked_r"], 0.4, places=6)
        self.assertEqual(position["bars_until_expiry"], 19)
        self.assertEqual(
            position["mark_time"],
            (bars[-1]["time"] + timedelta(hours=1)).isoformat(),
        )
        report = summarize(
            trades, [signal()], skipped, bars,
            date(2026, 3, 1), date(2026, 3, 1), open_positions,
        )
        self.assertEqual(report["completed_trades"], 0)
        self.assertEqual(report["open_positions_count"], 1)
        self.assertEqual(report["unfinished_positions_excluded"], 1)
        self.assertIsNotNone(report["open_position_warning"])

    def test_partial_tp1_reports_realized_and_remaining_unrealized_components(self) -> None:
        bars = [candle(i) for i in range(6)]
        bars[1] = candle(1, close=105.0, high=111.0, low=99.0)
        open_positions = []
        with patch.dict(os.environ, {"GOLD_TIMEFRAME": "1h"}, clear=False):
            trades, _ = simulate(
                bars, [signal()], 0.0, signal_ttl_candles=24,
                open_positions=open_positions,
            )
        self.assertEqual(trades, [])
        self.assertEqual(len(open_positions), 1)
        position = open_positions[0]
        self.assertTrue(position["tp1_hit"])
        self.assertEqual(position["remaining_fraction"], 0.5)
        self.assertAlmostEqual(position["realized_r"], 0.5, places=6)
        self.assertAlmostEqual(position["unrealized_r"], 0.2, places=6)
        self.assertAlmostEqual(position["total_marked_r"], 0.7, places=6)

    def test_ttl_exit_is_completed_not_left_in_open_positions(self) -> None:
        bars = [candle(i) for i in range(25)]
        open_positions = []
        trades, _ = simulate(
            bars, [signal()], 0.0, signal_ttl_candles=24,
            open_positions=open_positions,
        )
        self.assertEqual(open_positions, [])
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "EXPIRED")


if __name__ == "__main__":
    unittest.main()
