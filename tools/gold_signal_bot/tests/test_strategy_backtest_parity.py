"""Adaptive parity tests between live strategy and historical backtest signal paths.

The fixture is deterministic and offline: it contains 300 hourly OHLC candles
across rising, falling, sideways, and elevated-volatility regimes. It is not
represented as provider-sourced market data.
"""
from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest_historical import generate_signals  # noqa: E402
from gold_signal.strategy import detect_signals  # noqa: E402


def varied_candle_fixture(count: int = 300) -> list[dict]:
    """Build a fixed, reproducible multi-regime OHLC fixture."""
    if count < 60:
        raise ValueError("Parity fixture must contain at least 60 candles")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars = []
    previous_close = 2600.0
    for index in range(count):
        if index < 90:
            period = 9
            target = 2600.0 + 0.32 * index + 4.0 * math.sin(index * 2.0 * math.pi / period)
            spread = 1.5
        elif index < 180:
            elapsed = index - 90
            period = 10
            target = 2600.0 + 0.32 * 90 - 0.34 * elapsed + 4.0 * math.sin(index * 2.0 * math.pi / period)
            spread = 2.0
        elif index < 240:
            target = 2600.0 + 0.32 * 90 - 0.34 * 90 + 4.0 * math.sin(index * 2.0 * math.pi / 7)
            spread = 4.0
        else:
            target = (
                2600.0 + 0.32 * 90 - 0.34 * 90
                + 9.0 * math.sin(index * 2.0 * math.pi / 5)
            )
            spread = 8.0

        close = target
        bars.append({
            "time": start + timedelta(hours=index),
            "open": previous_close,
            "high": max(previous_close, close) + spread,
            "low": min(previous_close, close) - spread,
            "close": close,
        })
        previous_close = close
    return bars


class LiveBacktestSignalParityTests(unittest.TestCase):
    def test_signal_direction_timestamp_and_levels_match_for_every_candle(self) -> None:
        candles = varied_candle_fixture()
        self.assertGreaterEqual(len(candles), 50)

        live = detect_signals(candles, atr_multiplier=1.5, tp1_r=1.0, tp2_r=2.0)
        historical = generate_signals(candles, atr_mult=1.5, tp1_r=1.0, tp2_r=2.0)

        live_by_time = {signal["candle_time"]: signal for signal in live}
        historical_by_time = {signal["signal_time"]: signal for signal in historical}
        self.assertEqual(
            set(live_by_time),
            set(historical_by_time),
            "Signal timestamps differ: one path generated or omitted signals.",
        )

        # Compare the direction on every candle, including candles where neither
        # path should emit a signal (None), not just the positive signal subset.
        for candle in candles:
            stamp = candle["time"]
            live_signal = live_by_time.get(stamp)
            historical_signal = historical_by_time.get(stamp)
            live_side = live_signal["side"] if live_signal else None
            historical_side = historical_signal["side"] if historical_signal else None
            self.assertEqual(
                live_side, historical_side,
                f"Signal direction mismatch at {stamp.isoformat()}",
            )
            if live_signal is None:
                continue

            self.assertEqual(
                live_signal["candle_time"], historical_signal["signal_time"],
                f"Timestamp mismatch at {stamp.isoformat()}",
            )
            for live_key, historical_key in (
                ("entry", "entry"),
                ("stop_loss", "stop"),
                ("take_profit_1", "tp1"),
                ("take_profit_2", "tp2"),
            ):
                self.assertAlmostEqual(
                    live_signal[live_key], historical_signal[historical_key],
                    places=10,
                    msg=(
                        f"{live_key}/{historical_key} mismatch at "
                        f"{stamp.isoformat()}"
                    ),
                )

        self.assertTrue(live, "Fixture did not exercise any signal conditions.")
        self.assertIn("BUY", {signal["side"] for signal in live})
        self.assertIn("SELL", {signal["side"] for signal in live})


if __name__ == "__main__":
    unittest.main()
