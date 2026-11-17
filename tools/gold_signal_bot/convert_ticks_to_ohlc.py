"""Streaming UTC tick-to-OHLC conversion for the local XAU/USD bid/ask dataset.

The raw input is read-only from this program's perspective. No missing candles
are synthesized and no spread/gap observations are silently filtered.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

TIMEFRAME_MS = {
    "1min": 60_000,
    "5min": 300_000,
    "15min": 900_000,
    "1h": 3_600_000,
}
EXPECTED_HEADER = ["timestamp", "askPrice", "bidPrice"]
OUTPUT_FIELDS = [
    "bar_start_utc", "bar_start_epoch_ms", "tick_count",
    "bid_open", "bid_high", "bid_low", "bid_close",
    "ask_open", "ask_high", "ask_low", "ask_close",
    "mid_open", "mid_high", "mid_low", "mid_close",
    "spread_mean", "spread_max", "spread_gt_5_count", "spread_gt_10_count",
    "max_tick_gap_ms", "tick_gaps_gt_1s", "tick_gaps_gt_5s", "tick_gaps_gt_60s",
]


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_epoch_ms(value: str | None) -> int | None:
    if value is None:
        return None
    stamp = parse_utc(value)
    return int(stamp.timestamp() * 1000)


def epoch_ms_to_iso(value: int) -> str:
    seconds, milliseconds = divmod(value, 1000)
    stamp = datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=milliseconds * 1000
    )
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass
class Candle:
    start_ms: int
    first_bid: Decimal
    first_ask: Decimal
    bid_high: Decimal
    bid_low: Decimal
    ask_high: Decimal
    ask_low: Decimal
    mid_high: Decimal
    mid_low: Decimal
    tick_count: int = 0
    spread_sum: Decimal = Decimal("0")
    spread_max: Decimal = Decimal("0")
    spread_gt_5_count: int = 0
    spread_gt_10_count: int = 0
    max_tick_gap_ms: int = 0
    tick_gaps_gt_1s: int = 0
    tick_gaps_gt_5s: int = 0
    tick_gaps_gt_60s: int = 0
    last_tick_ms: int | None = None
    last_bid: Decimal | None = None
    last_ask: Decimal | None = None
    last_mid: Decimal | None = None

    @classmethod
    def start(cls, bucket_ms: int, bid: Decimal, ask: Decimal) -> "Candle":
        mid = (bid + ask) / Decimal(2)
        candle = cls(
            start_ms=bucket_ms,
            first_bid=bid, first_ask=ask,
            bid_high=bid, bid_low=bid,
            ask_high=ask, ask_low=ask,
            mid_high=mid, mid_low=mid,
        )
        candle.add(0, bid, ask, is_first=True)
        return candle

    def add(self, timestamp_ms: int, bid: Decimal, ask: Decimal,
            *, is_first: bool = False) -> None:
        mid = (bid + ask) / Decimal(2)
        spread = ask - bid
        if not is_first and self.last_tick_ms is not None:
            gap = timestamp_ms - self.last_tick_ms
            self.max_tick_gap_ms = max(self.max_tick_gap_ms, gap)
            self.tick_gaps_gt_1s += gap > 1_000
            self.tick_gaps_gt_5s += gap > 5_000
            self.tick_gaps_gt_60s += gap > 60_000
        self.bid_high = max(self.bid_high, bid)
        self.bid_low = min(self.bid_low, bid)
        self.ask_high = max(self.ask_high, ask)
        self.ask_low = min(self.ask_low, ask)
        self.mid_high = max(self.mid_high, mid)
        self.mid_low = min(self.mid_low, mid)
        self.spread_sum += spread
        self.spread_max = max(self.spread_max, spread)
        self.spread_gt_5_count += spread > Decimal("5")
        self.spread_gt_10_count += spread > Decimal("10")
        self.tick_count += 1
        self.last_tick_ms = timestamp_ms
        self.last_bid, self.last_ask, self.last_mid = bid, ask, mid

    def row(self) -> dict[str, str | int]:
        assert self.last_bid is not None and self.last_ask is not None
        assert self.last_mid is not None
        first_mid = (self.first_bid + self.first_ask) / Decimal(2)
        assert self.tick_count > 0
        return {
            "bar_start_utc": epoch_ms_to_iso(self.start_ms),
            "bar_start_epoch_ms": self.start_ms,
            "tick_count": self.tick_count,
            "bid_open": decimal_text(self.first_bid),
            "bid_high": decimal_text(self.bid_high),
            "bid_low": decimal_text(self.bid_low),
            "bid_close": decimal_text(self.last_bid),
            "ask_open": decimal_text(self.first_ask),
            "ask_high": decimal_text(self.ask_high),
            "ask_low": decimal_text(self.ask_low),
            "ask_close": decimal_text(self.last_ask),
            "mid_open": decimal_text(first_mid),
            "mid_high": decimal_text(self.mid_high),
            "mid_low": decimal_text(self.mid_low),
            "mid_close": decimal_text(self.last_mid),
            "spread_mean": decimal_text(self.spread_sum / self.tick_count),
            "spread_max": decimal_text(self.spread_max),
            "spread_gt_5_count": self.spread_gt_5_count,
            "spread_gt_10_count": self.spread_gt_10_count,
            "max_tick_gap_ms": self.max_tick_gap_ms,
            "tick_gaps_gt_1s": self.tick_gaps_gt_1s,
            "tick_gaps_gt_5s": self.tick_gaps_gt_5s,
            "tick_gaps_gt_60s": self.tick_gaps_gt_60s,
        }


class TimeframeAggregator:
    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self.current: Candle | None = None
        self.bar_count = 0
        self.missing_buckets = 0

    def push(self, timestamp_ms: int, bid: Decimal, ask: Decimal) -> dict | None:
        bucket = (timestamp_ms // self.interval_ms) * self.interval_ms
        if self.current is None:
            self.current = Candle.start(bucket, bid, ask)
            return None
        if bucket == self.current.start_ms:
            self.current.add(timestamp_ms, bid, ask)
            return None
        completed = self.current.row()
        previous_bucket = self.current.start_ms
        self.bar_count += 1
        self.missing_buckets += max(
            0, (bucket - previous_bucket) // self.interval_ms - 1
        )
        self.current = Candle.start(bucket, bid, ask)
        return completed

    def finish(self) -> dict | None:
        if self.current is None:
            return None
        completed = self.current.row()
        self.bar_count += 1
        self.current = None
        return completed


def _write_row(writer: csv.DictWriter, row: dict) -> None:
    writer.writerow(row)


def convert_ticks(
    input_path: Path,
    output_dir: Path,
    *,
    timeframes: tuple[str, ...] = ("1min", "5min", "15min", "1h"),
    expected_start: str | None = None,
    expected_end_exclusive: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Convert a chronological millisecond bid/ask CSV in one streaming pass."""
    if not timeframes or len(set(timeframes)) != len(timeframes):
        raise ValueError("Select one or more unique timeframes.")
    unknown = sorted(set(timeframes) - set(TIMEFRAME_MS))
    if unknown:
        raise ValueError(f"Unsupported timeframe(s): {', '.join(unknown)}")
    start_ms, end_ms = to_epoch_ms(expected_start), to_epoch_ms(expected_end_exclusive)
    if start_ms is not None and end_ms is not None and end_ms <= start_ms:
        raise ValueError("Expected end-exclusive timestamp must be after start.")
    input_path = input_path.resolve()
    output_dir = output_dir.resolve()
    if not input_path.is_file():
        raise FileNotFoundError("Input tick CSV does not exist.")
    output_dir.mkdir(parents=True, exist_ok=True)

    final_paths = {
        tf: output_dir / f"{input_path.stem}_{tf}_ohlc.csv"
        for tf in timeframes
    }
    report_path = output_dir / f"{input_path.stem}_conversion_report.json"
    all_final_paths = [*final_paths.values(), report_path]
    if not overwrite and any(path.exists() for path in all_final_paths):
        raise FileExistsError("One or more output files already exist; use --overwrite only after reviewing them.")
    partial_paths = {tf: path.with_name(path.name + ".partial") for tf, path in final_paths.items()}
    partial_report = report_path.with_name(report_path.name + ".partial")
    if any(path.exists() for path in [*partial_paths.values(), partial_report]):
        raise FileExistsError("A partial output already exists; inspect or remove it before retrying.")

    aggregators = {tf: TimeframeAggregator(TIMEFRAME_MS[tf]) for tf in timeframes}
    handles: dict[str, TextIO] = {}
    writers: dict[str, csv.DictWriter] = {}
    ticks = 0
    spread_gt_5 = 0
    spread_gt_10 = 0
    previous_timestamp: int | None = None
    try:
        for tf, path in partial_paths.items():
            handle = path.open("w", newline="", encoding="utf-8")
            handles[tf] = handle
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
            writers[tf] = writer
            writer.writeheader()

        with input_path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.reader(source, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError("Input CSV is empty.") from None
            if header != EXPECTED_HEADER:
                raise ValueError("Input CSV header must be exactly timestamp,askPrice,bidPrice.")
            for line_number, fields in enumerate(reader, start=2):
                if len(fields) != 3:
                    raise ValueError(f"Malformed CSV row at line {line_number}.")
                try:
                    timestamp_ms = int(fields[0])
                    ask, bid = Decimal(fields[1]), Decimal(fields[2])
                except (ValueError, InvalidOperation):
                    raise ValueError(f"Invalid timestamp or price at line {line_number}.") from None
                if timestamp_ms < 0 or not ask.is_finite() or not bid.is_finite():
                    raise ValueError(f"Non-finite or negative timestamp/price at line {line_number}.")
                if ask <= 0 or bid <= 0 or ask < bid:
                    raise ValueError(f"Invalid bid/ask ordering or non-positive price at line {line_number}.")
                if previous_timestamp is not None and timestamp_ms <= previous_timestamp:
                    raise ValueError(
                        f"Tick timestamps are duplicate or out of order at line {line_number}; input was not modified."
                    )
                if start_ms is not None and timestamp_ms < start_ms:
                    raise ValueError(f"Tick precedes the expected UTC start at line {line_number}.")
                if end_ms is not None and timestamp_ms >= end_ms:
                    raise ValueError(f"Tick is at or beyond the expected end-exclusive boundary at line {line_number}.")
                previous_timestamp = timestamp_ms
                ticks += 1
                spread = ask - bid
                spread_gt_5 += spread > Decimal("5")
                spread_gt_10 += spread > Decimal("10")
                for tf, aggregator in aggregators.items():
                    completed = aggregator.push(timestamp_ms, bid, ask)
                    if completed is not None:
                        _write_row(writers[tf], completed)

        if ticks == 0:
            raise ValueError("Input CSV contains no tick records.")
        bars_by_timeframe: dict[str, int] = {}
        missing_by_timeframe: dict[str, int] = {}
        for tf, aggregator in aggregators.items():
            completed = aggregator.finish()
            if completed is not None:
                _write_row(writers[tf], completed)
            bars_by_timeframe[tf] = aggregator.bar_count
            missing_by_timeframe[tf] = aggregator.missing_buckets
        for handle in handles.values():
            handle.flush()
            handle.close()
        handles.clear()

        report = {
            "status": "converted",
            "input_file": input_path.name,
            "input_bytes": input_path.stat().st_size,
            "input_records": ticks,
            "timestamp_unit": "unix_milliseconds",
            "timezone": "UTC",
            "candle_boundary": "epoch-aligned UTC intervals; [start, end)",
            "price_policy": "bid, ask, and per-tick midpoint OHLC are all retained; no basis selected for strategy use",
            "spread_policy": "no filtering; mean/max and counts above 5 and 10 price units are recorded",
            "gap_policy": "no synthetic candles; missing interval buckets and within-bar tick gaps are reported",
            "duplicate_policy": "strictly increasing timestamps required; duplicates and out-of-order ticks fail conversion",
            "tick_count_spread_gt_5": spread_gt_5,
            "tick_count_spread_gt_10": spread_gt_10,
            "bars_by_timeframe": bars_by_timeframe,
            "missing_buckets_by_timeframe": missing_by_timeframe,
            "outputs": {tf: path.name for tf, path in final_paths.items()},
            "raw_input_modified": False,
            "backtest_or_live_strategy_connected": False,
            "approval": "NOT_GRANTED; review output and compare before strategy/backtest use",
        }
        partial_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        for tf, partial in partial_paths.items():
            partial.replace(final_paths[tf])
        partial_report.replace(report_path)
        return report
    except Exception:
        for handle in handles.values():
            try:
                handle.close()
            except OSError:
                pass
        for path in [*partial_paths.values(), partial_report]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stream a local XAU/USD bid/ask tick CSV into UTC OHLC files; no live trading."
    )
    parser.add_argument("--input", required=True, help="Path to the read-only raw tick CSV.")
    parser.add_argument("--output-dir", required=True, help="Directory for derived OHLC CSVs and report.")
    parser.add_argument("--timeframes", nargs="+", choices=sorted(TIMEFRAME_MS),
                        default=["1min", "5min", "15min", "1h"])
    parser.add_argument("--expected-start", help="Optional UTC ISO timestamp, inclusive.")
    parser.add_argument("--expected-end-exclusive", help="Optional UTC ISO timestamp, exclusive.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace existing derived outputs only; never changes the input file.")
    args = parser.parse_args()
    try:
        report = convert_ticks(
            Path(args.input), Path(args.output_dir),
            timeframes=tuple(args.timeframes),
            expected_start=args.expected_start,
            expected_end_exclusive=args.expected_end_exclusive,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"Tick conversion failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
