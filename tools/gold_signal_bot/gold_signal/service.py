"""Application orchestration with chronological signal catch-up."""
from __future__ import annotations
import logging
import requests
from .config import Config
from .market_data import fetch_closed_candles, parse_bar_time
from .strategy import detect_signals
from .state import process_lock, read_state, write_state
from .telegram import format_signal, send_telegram

LOG = logging.getLogger("gold_signal_bot")

def run_once(config: Config, session: requests.Session | None = None) -> str:
    """Process all newly closed candles in order, with O(n) signal evaluation.

    First boot evaluates only the newest closed bar. Later cycles recover all
    newly closed bars retained by the provider, or fail closed if history is short.
    """
    with process_lock(config.state_file):
        candles = fetch_closed_candles(config, session=session)
        if len(candles) < 60:
            raise RuntimeError("Insufficient candle history for signal evaluation")
        cursor_raw = read_state(config.state_file)
        if not cursor_raw:
            start_index = len(candles) - 1
        else:
            cursor = parse_bar_time(cursor_raw)
            first_time, latest_time = candles[0]["time"], candles[-1]["time"]
            if cursor < first_time:
                raise RuntimeError(
                    "Signal-processing cursor is older than available provider history; "
                    "refusing to skip potentially missed signals"
                )
            if cursor > latest_time:
                raise RuntimeError("Signal-processing cursor is newer than provider data")
            start_index = next(
                (i for i, candle in enumerate(candles) if candle["time"] > cursor),
                len(candles),
            )
        if start_index >= len(candles):
            LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
            return "no_signal"
        signals = {
            signal["candle_time"]: signal
            for signal in detect_signals(
                candles, atr_multiplier=config.atr_multiplier,
                tp1_r=config.tp1_r, tp2_r=config.tp2_r,
            )
        }
        delivered = 0
        processed = 0
        for index in range(start_index, len(candles)):
            stamp = candles[index]["time"]
            data = signals.get(stamp)
            if data:
                send_telegram(config, format_signal(data, config.timeframe), session=session)
                delivered += 1
                LOG.info("Delivered %s XAU/USD signal for candle %s", data["side"], stamp.isoformat())
            # At-least-once: a crash after Telegram accepts but before local fsync
            # can replay one message; Telegram sendMessage offers no idempotency key.
            write_state(config.state_file, stamp)
            processed += 1
        if delivered:
            return "sent" if processed == 1 else "sent_catchup"
        if processed:
            return "processed_no_signal"
        LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
        return "no_signal"
