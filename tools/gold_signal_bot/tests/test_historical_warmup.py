"""Bounded historical warm-up expansion tests using a fake provider."""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtest_historical import fetch_history_with_warmup  # noqa: E402


def bars_with_warmup(start: date, warmup_count: int, timeframe: str = "1h") -> list[dict]:
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    bars = []
    for index in range(warmup_count):
        stamp = start_utc - timedelta(hours=warmup_count - index)
        bars.append({
            "time": stamp, "open": 2600.0, "high": 2602.0,
            "low": 2598.0, "close": 2601.0,
        })
    for index in range(24):
        stamp = start_utc + timedelta(hours=index)
        bars.append({
            "time": stamp, "open": 2600.0, "high": 2602.0,
            "low": 2598.0, "close": 2601.0,
        })
    return bars


class HistoricalWarmupExpansionTests(unittest.TestCase):
    def test_expands_request_until_200_actual_pre_window_candles_are_available(self) -> None:
        start = date(2026, 4, 10)
        end = start
        first = bars_with_warmup(start, 150)
        second = bars_with_warmup(start, 205)
        with patch("backtest_historical.fetch_history", side_effect=[first, second]) as fetch:
            bars, warmup_count = fetch_history_with_warmup(
                "unused-key", "1h", start, end, object(),
            )
        self.assertEqual(warmup_count, 205)
        self.assertEqual(len(bars), len(second))
        self.assertEqual(fetch.call_count, 2)
        first_start = fetch.call_args_list[0].args[2]
        second_start = fetch.call_args_list[1].args[2]
        self.assertLess(second_start, first_start)

    def test_stops_after_three_attempts_with_clear_error_if_warmup_stays_short(self) -> None:
        start = date(2026, 4, 10)
        insufficient = bars_with_warmup(start, 150)
        with patch("backtest_historical.fetch_history", return_value=insufficient) as fetch:
            with self.assertRaisesRegex(RuntimeError, "3 attempts.*150.*200"):
                fetch_history_with_warmup(
                    "unused-key", "1h", start, start, object(),
                )
        self.assertEqual(fetch.call_count, 3)

    def test_actual_test_window_coverage_is_still_required(self) -> None:
        start = date(2026, 4, 10)
        insufficient_test_window = bars_with_warmup(start, 205)[:-10]
        with patch("backtest_historical.fetch_history", return_value=insufficient_test_window):
            with self.assertRaisesRegex(RuntimeError, "ends too early"):
                fetch_history_with_warmup(
                    "unused-key", "1h", start, start, object(),
                )


if __name__ == "__main__":
    unittest.main()
