"""Application orchestration with chronological signal catch-up."""
from __future__ import annotations
import logging
import requests
from .config import Config
from .market_data import fetch_closed_candles, parse_bar_time
from .strategy import detect_signal
from .state import process_lock, read_state, write_state
from .telegram import format_signal, send_telegram

LOG = logging.getLogger("gold_signal_bot")

def run_once(config: Config, session: requests.Session | None = None) -> str:
    """Process every newly closed candle in order, rather than checking only newest.

    On first boot, evaluate only the newest closed candle to avoid a burst of
    historical alerts. Thereafter, the durable cursor makes outages recoverable
    while provider history still covers the cursor.
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
        delivered = 0
        processed = 0
        for index in range(start_index, len(candles)):
            data = detect_signal(
                candles[:index + 1], atr_multiplier=config.atr_multiplier,
                tp1_r=config.tp1_r, tp2_r=config.tp2_r,
            )
            if data:
                send_telegram(config, format_signal(data, config.timeframe), session=session)
                delivered += 1
                LOG.info("Delivered %s XAU/USD signal for candle %s", data["side"],
                         data["candle_time"].isoformat())
            # Persist only after Telegram accepts a signal, or after a no-signal
            # candle is fully evaluated. A crash between send and fsync can replay
            # one alert: Telegram sendMessage has no idempotency key (at-least-once).
            write_state(config.state_file, candles[index]["time"])
            processed += 1
        if delivered:
            return "sent" if processed == 1 else "sent_catchup"
        if processed:
            return "processed_no_signal"
        LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
        return "no_signal"
