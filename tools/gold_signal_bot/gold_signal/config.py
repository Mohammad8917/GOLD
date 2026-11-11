"""Environment-backed configuration for the standalone GOLD notifier."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

TIMEFRAMES = {
    "1min": ("1min", 60), "5min": ("5min", 300), "15min": ("15min", 900),
    "30min": ("30min", 1800), "1h": ("1h", 3600), "4h": ("4h", 14400),
    "1day": ("1day", 86400),
}

@dataclass(frozen=True)
class Config:
    api_key: str
    bot_token: str
    chat_id: str
    timeframe: str
    poll_seconds: int
    atr_multiplier: float
    tp1_r: float
    tp2_r: float
    state_file: Path
    http_timeout: int
    retry_attempts: int
    retry_base_seconds: float
    stale_multiplier: float
    gap_multiplier: float

    @classmethod
    def from_env(cls, *, require_market_data: bool = True) -> "Config":
        api_key = os.getenv("GOLD_TWELVE_DATA_API_KEY", "").strip()
        bot_token = os.getenv("GOLD_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("GOLD_TELEGRAM_CHAT_ID", "").strip()
        if require_market_data and not api_key:
            raise ValueError("GOLD_TWELVE_DATA_API_KEY is not set in the project-root .env")
        if not bot_token:
            raise ValueError("GOLD_TELEGRAM_BOT_TOKEN is not set in the project-root .env")
        if not chat_id:
            raise ValueError("GOLD_TELEGRAM_CHAT_ID is not set in the project-root .env")
        timeframe = os.getenv("GOLD_TIMEFRAME", "15min").strip().lower()
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported GOLD_TIMEFRAME {timeframe!r}")
        interval_seconds = TIMEFRAMES[timeframe][1]
        poll_seconds = int(os.getenv("GOLD_POLL_SECONDS", str(max(20, min(60, interval_seconds // 10)))))
        if poll_seconds < 10:
            raise ValueError("GOLD_POLL_SECONDS must be at least 10")
        atr_multiplier = float(os.getenv("GOLD_RISK_ATR_MULTIPLIER", "1.5"))
        tp1_r = float(os.getenv("GOLD_TP1_R", "1.0"))
        tp2_r = float(os.getenv("GOLD_TP2_R", "2.0"))
        if not all(math.isfinite(v) and v > 0 for v in (atr_multiplier, tp1_r, tp2_r)):
            raise ValueError("ATR multiplier and TP R-multiples must be finite positive numbers")
        if tp2_r <= tp1_r:
            raise ValueError("GOLD_TP2_R must be greater than GOLD_TP1_R")
        retry_attempts = int(os.getenv("GOLD_RETRY_ATTEMPTS", "3"))
        retry_base = float(os.getenv("GOLD_RETRY_BASE_SECONDS", "1"))
        stale_multiplier = float(os.getenv("GOLD_STALE_CANDLE_MULTIPLIER", "3"))
        gap_multiplier = float(os.getenv("GOLD_MAX_GAP_MULTIPLIER", "1.5"))
        if not 1 <= retry_attempts <= 8:
            raise ValueError("GOLD_RETRY_ATTEMPTS must be between 1 and 8")
        if not all(math.isfinite(v) and v > 0 for v in (retry_base, stale_multiplier, gap_multiplier)):
            raise ValueError("Retry delay and candle validation multipliers must be finite positive numbers")
        state_value = os.getenv("GOLD_SIGNAL_STATE_FILE", "").strip()
        state_file = (
            Path(state_value).expanduser()
            if state_value
            else Path.home() / ".quantdinger_gold_signal_state.json"
        )
        return cls(api_key, bot_token, chat_id, timeframe, poll_seconds,
                   atr_multiplier, tp1_r, tp2_r, state_file,
                   max(5, int(os.getenv("GOLD_HTTP_TIMEOUT", "20"))),
                   retry_attempts, retry_base, stale_multiplier, gap_multiplier)
