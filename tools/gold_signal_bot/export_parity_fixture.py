"""Export a sanitized Twelve Data XAU/USD OHLC fixture for real-feed parity checks.

The export contains public market candles and parity counts only. It never writes
API keys, Telegram credentials, account details, or request URLs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest_historical import fetch_history, generate_signals  # noqa: E402
from gold_signal.strategy import detect_signals  # noqa: E402


def compare_signal_paths(
    bars: list[dict[str, Any]], *, atr_multiplier: float = 1.5,
    tp1_r: float = 1.0, tp2_r: float = 2.0,
) -> dict[str, Any]:
    """Compare both paths candle-by-candle on the same provider-sourced bars."""
    live = detect_signals(
        bars, atr_multiplier=atr_multiplier, tp1_r=tp1_r, tp2_r=tp2_r,
    )
    historical = generate_signals(
        bars, atr_mult=atr_multiplier, tp1_r=tp1_r, tp2_r=tp2_r,
    )
    live_by_time = {item["candle_time"]: item for item in live}
    historical_by_time = {item["signal_time"]: item for item in historical}
    if set(live_by_time) != set(historical_by_time):
        missing_live = sorted(str(value) for value in set(historical_by_time) - set(live_by_time))
        missing_historical = sorted(str(value) for value in set(live_by_time) - set(historical_by_time))
        raise AssertionError(
            "Real-feed signal timestamp mismatch; no fixture was written. "
            f"Missing live={missing_live[:5]}, missing historical={missing_historical[:5]}"
        )

    fields = (
        ("side", "side"),
        ("entry", "entry"),
        ("stop_loss", "stop"),
        ("take_profit_1", "tp1"),
        ("take_profit_2", "tp2"),
    )
    for stamp, live_signal in live_by_time.items():
        old_signal = historical_by_time[stamp]
        for live_key, historical_key in fields:
            left, right = live_signal[live_key], old_signal[historical_key]
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                if abs(float(left) - float(right)) > 1e-10:
                    raise AssertionError(
                        f"Real-feed {live_key} mismatch at {stamp}: {left!r} != {right!r}"
                    )
            elif left != right:
                raise AssertionError(
                    f"Real-feed {live_key} mismatch at {stamp}: {left!r} != {right!r}"
                )

    directions = Counter(item["side"] for item in live)
    return {
        "signals_checked": len(live),
        "buy_signals": directions.get("BUY", 0),
        "sell_signals": directions.get("SELL", 0),
        "timestamp_count": len(live_by_time),
        "mismatches": 0,
        "comparison": "live detect_signals vs historical generate_signals",
        "tolerance": 1e-10,
    }


def build_payload(
    bars: list[dict[str, Any]], *, timeframe: str, start: date, end: date,
    parity: dict[str, Any], exported_at: str | None = None,
) -> dict[str, Any]:
    """Build a secret-free, deterministic-schema JSON payload."""
    return {
        "schema_version": 1,
        "source": "Twelve Data",
        "symbol": "XAU/USD",
        "timeframe": timeframe,
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "timezone": "UTC",
        "exported_at": exported_at or datetime.now(timezone.utc).isoformat(),
        "candle_count": len(bars),
        "parity": parity,
        "candles": [
            {
                "time": bar["time"].astimezone(timezone.utc).isoformat(),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
            }
            for bar in bars
        ],
    }


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export sanitized real Twelve Data XAU/USD candles and run live/backtest parity."
    )
    parser.add_argument("--start", required=True, type=date.fromisoformat, help="UTC start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=date.fromisoformat, help="UTC end date, YYYY-MM-DD")
    parser.add_argument("--timeframe", default=os.getenv("GOLD_TIMEFRAME", "1h"))
    parser.add_argument("--min-candles", type=int, default=200)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports") / "twelvedata_xauusd_parity.json",
    )
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must be on or after --start")
    if args.min_candles < 50:
        parser.error("--min-candles must be at least 50")

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("GOLD_TWELVE_DATA_API_KEY", "").strip()
    if not api_key:
        parser.error("GOLD_TWELVE_DATA_API_KEY is missing from the project-root .env")

    with requests.Session() as session:
        bars = fetch_history(api_key, args.timeframe, args.start, args.end, session)

    if len(bars) < args.min_candles:
        raise RuntimeError(
            f"Only {len(bars)} completed candles were downloaded; "
            f"{args.min_candles} are required. Widen the date range or lower --min-candles "
            "(never below 50)."
        )

    parity = compare_signal_paths(bars)
    payload = build_payload(
        bars, timeframe=args.timeframe, start=args.start, end=args.end, parity=parity,
    )
    write_payload(args.out, payload)
    print(f"Exported {len(bars)} sanitized XAU/USD candles to: {args.out}")
    print(
        "Real-feed parity: PASS | "
        f"signals={parity['signals_checked']} | BUY={parity['buy_signals']} | "
        f"SELL={parity['sell_signals']} | mismatches={parity['mismatches']}"
    )
    print("The JSON contains OHLC data and parity metadata only; no API key or Telegram secret.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
