"""Pure signal decision logic for the initial EMA/RSI/ATR strategy."""
from __future__ import annotations

import math
from typing import Any

from .indicators import atr, ema, rsi

def detect_signal(candles: list[dict[str, Any]], *, atr_multiplier: float = 1.5,
                  tp1_r: float = 1.0, tp2_r: float = 2.0) -> dict[str, Any] | None:
    """EMA20/EMA50 trend + EMA20 price cross + RSI filter on completed candles."""
    if len(candles) < 60:
        return None
    closes = [float(c["close"]) for c in candles]
    fast, slow = ema(closes, 20), ema(closes, 50)
    momentum, volatility = rsi(closes, 14), atr(candles, 14)
    i, previous = len(candles) - 1, len(candles) - 2
    if any(x is None for x in (fast[i], fast[previous], slow[i], momentum[i], volatility[i])):
        return None
    close = closes[i]
    side = ""
    if closes[previous] <= fast[previous] and close > fast[i] and fast[i] > slow[i] and 50 <= momentum[i] <= 70:
        side = "BUY"
    elif closes[previous] >= fast[previous] and close < fast[i] and fast[i] < slow[i] and 30 <= momentum[i] <= 50:
        side = "SELL"
    if not side:
        return None
    risk = float(volatility[i]) * atr_multiplier
    if not math.isfinite(risk) or risk <= 0:
        return None
    if side == "BUY":
        stop, tp1, tp2 = close - risk, close + risk * tp1_r, close + risk * tp2_r
    else:
        stop, tp1, tp2 = close + risk, close - risk * tp1_r, close - risk * tp2_r
    return {"symbol": "XAU/USD", "side": side, "entry": close, "stop_loss": stop,
            "take_profit_1": tp1, "take_profit_2": tp2, "atr": float(volatility[i]),
            "rsi": float(momentum[i]), "ema_fast": float(fast[i]), "ema_slow": float(slow[i]),
            "candle_time": candles[i]["time"]}
