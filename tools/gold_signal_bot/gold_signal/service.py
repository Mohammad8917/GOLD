"""Application orchestration for one signal-generation cycle."""
from __future__ import annotations

import logging
import requests

from .config import Config
from .market_data import fetch_closed_candles
from .strategy import detect_signal
from .state import process_lock, read_state, write_state
from .telegram import format_signal, send_telegram

LOG = logging.getLogger("gold_signal_bot")

def run_once(config: Config, session: requests.Session | None = None) -> str:
    with process_lock(config.state_file):
        candles = fetch_closed_candles(config, session=session)
        data = detect_signal(candles, atr_multiplier=config.atr_multiplier,
                             tp1_r=config.tp1_r, tp2_r=config.tp2_r)
        if not data:
            LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
            return "no_signal"
        key = data["candle_time"].isoformat()
        if key == read_state(config.state_file):
            LOG.info("Signal for this candle was already delivered")
            return "duplicate"
        send_telegram(config, format_signal(data, config.timeframe), session=session)
        # Persist only after Telegram confirms success; a failed send remains eligible next cycle.
        write_state(config.state_file, data["candle_time"])
        LOG.info("Delivered %s XAU/USD signal for candle %s", data["side"], key)
        return "sent"
