"""Backward-compatible public entry point for the modular GOLD signal bot.

Keep this facade stable for existing commands and integrations. Implementation
lives in the gold_signal package; importing this module performs no .env loading.
"""
from gold_signal.config import Config, TIMEFRAMES
from gold_signal.indicators import atr, ema, rsi
from gold_signal.market_data import (
    _get_with_retry, _is_expected_gold_pause, fetch_closed_candles,
    parse_bar_time, validate_candles,
)
from gold_signal.service import run_once
from gold_signal.state import process_lock, read_state, write_state
from gold_signal.strategy import detect_signal
from gold_signal.telegram import format_signal, send_telegram
from gold_signal.cli import main

__all__ = [
    "Config", "TIMEFRAMES", "atr", "detect_signal", "ema", "fetch_closed_candles",
    "format_signal", "main", "parse_bar_time", "process_lock", "read_state",
    "rsi", "run_once", "send_telegram", "validate_candles", "write_state",
]

if __name__ == "__main__":
    raise SystemExit(main())
