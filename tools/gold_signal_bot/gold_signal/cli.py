"""Command-line entry point; environment loading is intentionally kept here."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import Config, seconds_until_next_candle_close
from .quota import QuotaLimitError
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
            message_id = send_telegram(
                config,
                "✅ QuantDinger GOLD notifier: اتصال تلگرام موفق است.\nاین پیام آزمایشی است؛ معامله‌ای اجرا نمی‌شود.",
            )
            if type(message_id) is not int or message_id <= 0:
                raise RuntimeError("Telegram test returned success without a valid message_id")
            LOG.info("Telegram test message delivered (message_id=%s)", message_id)
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
    stop_event = threading.Event()
    def stop_handler(_signum: int, _frame: Any) -> None:
        stop_event.set()
        LOG.info("Stop requested")
    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)
    LOG.info("GOLD notifier started: XAU/USD, timeframe=%s; candle-close scheduling enabled", config.timeframe)
    import time
    last_heartbeat = time.monotonic()
    connectivity_lost = False
    while not stop_event.is_set():
        wait_seconds = seconds_until_next_candle_close(config.timeframe, offset_seconds=config.candle_close_offset_seconds)
        try:
            run_once(config)
            if connectivity_lost or time.monotonic() - last_heartbeat >= 1800:
                status = "connection restored" if connectivity_lost else "heartbeat: service alive"
                try:
                    message_id = send_telegram(config, f"GOLD bot: {status}; XAU/USD signal monitoring active.")
                    if type(message_id) is not int or message_id <= 0:
                        raise RuntimeError("Heartbeat returned no valid Telegram message_id")
                    last_heartbeat = time.monotonic()
                    connectivity_lost = False
                except Exception as heartbeat_error:
                    connectivity_lost = True
                    LOG.warning("Heartbeat delivery unavailable: %s", _redact(heartbeat_error, config))
        except QuotaLimitError as exc:
            wait_seconds = min(int(exc.retry_after) + 1, 60)
            connectivity_lost = True
            LOG.warning("Market-data quota pause (%s); retry in %s seconds", exc.window, wait_seconds)
        except Exception as exc:
            LOG.error("GOLD check failed; retrying shortly: %s", _redact(exc, config))
            connectivity_lost = True
            wait_seconds = 60
        stop_event.wait(max(1, wait_seconds))
    return 0
