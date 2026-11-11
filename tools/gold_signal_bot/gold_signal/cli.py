"""Command-line entry point; environment loading is intentionally kept here."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import Config
from .service import run_once
from .telegram import send_telegram

ROOT = Path(__file__).resolve().parents[3]
LOG = logging.getLogger("gold_signal_bot")

def _redact(message: Any, config: Config) -> str:
    value = str(message)
    for secret in (config.api_key, config.bot_token):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value[:500]


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Signal-only XAUUSD notifier; no broker or order execution.")
    parser.add_argument("--once", action="store_true", help="check once and exit")
    parser.add_argument("--test-telegram", action="store_true", help="send a Telegram connectivity test")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("GOLD_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env(require_market_data=not args.test_telegram)
    except (ValueError, TypeError) as exc:
        LOG.error("%s", exc)
        return 2
    if args.test_telegram:
        try:
            send_telegram(config, "✅ QuantDinger GOLD notifier: اتصال تلگرام موفق است.\nاین پیام آزمایشی است؛ معامله‌ای اجرا نمی‌شود.")
            LOG.info("Telegram test message delivered")
            return 0
        except Exception as exc:
            LOG.error("Telegram test failed: %s", _redact(exc, config))
            return 1
    if args.once:
        try:
            run_once(config)
            return 0
        except Exception as exc:
            LOG.error("GOLD check failed: %s", _redact(exc, config))
            return 1
    stopping = False
    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True
        LOG.info("Stop requested")
    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    LOG.info("GOLD notifier started: XAU/USD, timeframe=%s, poll=%ss; signal-only mode",
             config.timeframe, config.poll_seconds)
    while not stopping:
        try:
            run_once(config)
        except Exception as exc:
            LOG.error("GOLD check failed; will retry next cycle: %s", _redact(exc, config))
        for _ in range(config.poll_seconds):
            if stopping:
                break
            time.sleep(1)
    return 0
