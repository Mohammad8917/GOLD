"""Tests for streaming tick conversion and its fail-closed data contract."""
from __future__ import annotations

import csv
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from convert_ticks_to_ohlc import convert_ticks  # noqa: E402


def epoch_ms(iso: str) -> int:
    stamp = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(stamp.timestamp() * 1000)


class TickConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "ticks.csv"
        self.output = self.root / "derived"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_ticks(self, rows: list[tuple[int, str, str]]) -> None:
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "askPrice", "bidPrice"])
            writer.writerows(rows)

    def read_bars(self, timeframe: str = "1min") -> list[dict[str, str]]:
        path = self.output / f"ticks_{timeframe}_ohlc.csv"
        with path.open("r", newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_bid_ask_mid_ohlc_and_spread_metrics_are_aggregated(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([
            (start, "101.00", "100.00"),
            (start + 20_000, "102.00", "101.00"),
            (start + 60_000, "100.00", "99.00"),
        ])
        report = convert_ticks(self.source, self.output, timeframes=("1min",))
        bars = self.read_bars()
        self.assertEqual(len(bars), 2)
        first = bars[0]
        self.assertEqual(first["bar_start_utc"], "2025-11-14T00:00:00.000Z")
        self.assertEqual(first["tick_count"], "2")
        self.assertEqual(
            [first[k] for k in ("bid_open", "bid_high", "bid_low", "bid_close")],
            ["100", "101", "100", "101"],
        )
        self.assertEqual(
            [first[k] for k in ("ask_open", "ask_high", "ask_low", "ask_close")],
            ["101", "102", "101", "102"],
        )
        self.assertEqual(
            [first[k] for k in ("mid_open", "mid_high", "mid_low", "mid_close")],
            ["100.5", "101.5", "100.5", "101.5"],
        )
        self.assertEqual(first["spread_mean"], "1")
        self.assertEqual(first["spread_max"], "1")
        self.assertEqual(first["max_tick_gap_ms"], "20000")
        self.assertEqual(first["tick_gaps_gt_1s"], "1")
        self.assertEqual(report["input_records"], 3)
        self.assertEqual(report["bars_by_timeframe"]["1min"], 2)
        self.assertFalse(report["raw_input_modified"])
        self.assertFalse(report["backtest_or_live_strategy_connected"])

    def test_missing_time_buckets_are_reported_not_synthesized(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([
            (start, "101", "100"),
            (start + 180_000, "102", "101"),
        ])
        report = convert_ticks(self.source, self.output, timeframes=("1min",))
        self.assertEqual(len(self.read_bars()), 2)
        self.assertEqual(report["missing_buckets_by_timeframe"]["1min"], 2)

    def test_all_timeframes_are_aggregated_in_one_conversion(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([
            (start, "101", "100"),
            (start + 60_000, "102", "101"),
            (start + 300_000, "103", "102"),
        ])
        report = convert_ticks(
            self.source, self.output,
            timeframes=("1min", "5min", "15min", "1h"),
        )
        self.assertEqual(set(report["bars_by_timeframe"]), {"1min", "5min", "15min", "1h"})
        self.assertTrue(all((self.output / name).is_file() for name in report["outputs"].values()))

    def test_global_gap_metrics_include_gaps_across_candle_boundaries(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([
            (start + 59_000, "101", "100"),
            (start + 120_000, "102", "101"),
        ])
        report = convert_ticks(self.source, self.output, timeframes=("1min",))
        metrics = report["global_intertick_gap_metrics"]
        self.assertEqual(metrics["max_gap_ms"], 61_000)
        self.assertEqual(metrics["gaps_gt_60s"], 1)
        self.assertEqual(report["first_tick_utc"], "2025-11-14T00:00:59.000Z")
        self.assertEqual(report["last_tick_utc"], "2025-11-14T00:02:00.000Z")

    def test_raw_input_hash_is_unchanged(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([(start, "101", "100")])
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        convert_ticks(self.source, self.output, timeframes=("1min",))
        after = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_duplicate_timestamp_fails_closed(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([(start, "101", "100"), (start, "102", "101")])
        with self.assertRaisesRegex(ValueError, "duplicate or out of order"):
            convert_ticks(self.source, self.output, timeframes=("1min",))
        self.assertFalse((self.output / "ticks_1min_ohlc.csv").exists())

    def test_out_of_order_timestamp_fails_closed(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([(start + 1000, "101", "100"), (start, "102", "101")])
        with self.assertRaisesRegex(ValueError, "duplicate or out of order"):
            convert_ticks(self.source, self.output, timeframes=("1min",))

    def test_crossed_or_nonpositive_market_prices_fail_closed(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        for ask, bid in (("99", "100"), ("0", "0"), ("NaN", "100")):
            with self.subTest(ask=ask, bid=bid):
                self.write_ticks([(start, ask, bid)])
                with self.assertRaises(ValueError):
                    convert_ticks(self.source, self.output, timeframes=("1min",))

    def test_expected_date_range_is_enforced_as_half_open(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        end = epoch_ms("2025-11-15T00:00:00Z")
        self.write_ticks([(start, "101", "100"), (end, "102", "101")])
        with self.assertRaisesRegex(ValueError, "end-exclusive"):
            convert_ticks(
                self.source, self.output, timeframes=("1min",),
                expected_start="2025-11-14T00:00:00Z",
                expected_end_exclusive="2025-11-15T00:00:00Z",
            )

    def test_expected_record_count_mismatch_fails_closed(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([(start, "101", "100")])
        with self.assertRaisesRegex(ValueError, "record count mismatch"):
            convert_ticks(
                self.source, self.output, timeframes=("1min",),
                expected_records=2,
            )
        self.assertFalse((self.output / "ticks_1min_ohlc.csv").exists())

    def test_expected_file_size_mismatch_is_rejected_before_conversion(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([(start, "101", "100")])
        with self.assertRaisesRegex(ValueError, "byte size mismatch"):
            convert_ticks(
                self.source, self.output, timeframes=("1min",),
                expected_bytes=self.source.stat().st_size + 1,
            )
        self.assertFalse(self.output.exists())

    def test_output_is_not_overwritten_without_explicit_flag(self) -> None:
        start = epoch_ms("2025-11-14T00:00:00Z")
        self.write_ticks([(start, "101", "100")])
        convert_ticks(self.source, self.output, timeframes=("1min",))
        with self.assertRaises(FileExistsError):
            convert_ticks(self.source, self.output, timeframes=("1min",))

    def test_invalid_header_is_rejected(self) -> None:
        self.source.write_text("time,bid,ask\n1,100,101\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "header"):
            convert_ticks(self.source, self.output, timeframes=("1min",))


if __name__ == "__main__":
    unittest.main()
