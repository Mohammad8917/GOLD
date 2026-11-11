"""Causal EMA/RSI/ATR signal rules with efficient batch evaluation."""
from __future__ import annotations
import math
from typing import Any
from .indicators import atr, ema, rsi

def detect_signals(candles: list[dict[str, Any]], *, atr_multiplier: float = 1.5,
                   tp1_r: float = 1.0, tp2_r: float = 2.0) -> list[dict[str, Any]]:
    """Return every signal in chronological order using indicators calculated once.

    Each decision at index i uses only candle data through i. Computing the full
    indicator arrays is causal: EMA, Wilder RSI, and ATR are recursive/past-only.
    This avoids O(n²) repeated prefix calculations during outage catch-up.
    """
    if len(candles) < 60:
        return []
    if not all(math.isfinite(v) and v > 0 for v in (atr_multiplier, tp1_r, tp2_r)):
        raise ValueError("Risk and target multipliers must be finite and positive")
    if tp2_r <= tp1_r:
        raise ValueError("tp2_r must be greater than tp1_r")
    closes = [float(c["close"]) for c in candles]
    fast, slow = ema(closes, 20), ema(closes, 50)
    momentum, volatility = rsi(closes, 14), atr(candles, 14)
    signals: list[dict[str, Any]] = []
    for i in range(1, len(candles)):
        previous = i - 1
        if any(x is None for x in (fast[i], fast[previous], slow[i], momentum[i], volatility[i])):
            continue
        close = closes[i]
        side = ""
        if (closes[previous] <= fast[previous] and close > fast[i]
                and fast[i] > slow[i] and 50 <= momentum[i] <= 70):
            side = "BUY"
        elif (closes[previous] >= fast[previous] and close < fast[i]
                and fast[i] < slow[i] and 30 <= momentum[i] <= 50):
            side = "SELL"
        if not side:
            continue
        risk = float(volatility[i]) * atr_multiplier
        if not math.isfinite(risk) or risk <= 0:
            continue
        if side == "BUY":
            stop, tp1, tp2 = close - risk, close + risk * tp1_r, close + risk * tp2_r
        else:
            stop, tp1, tp2 = close + risk, close - risk * tp1_r, close - risk * tp2_r
        signals.append({
            "symbol": "XAU/USD", "side": side, "entry": close, "stop_loss": stop,
            "take_profit_1": tp1, "take_profit_2": tp2, "atr": float(volatility[i]),
            "rsi": float(momentum[i]), "ema_fast": float(fast[i]), "ema_slow": float(slow[i]),
            "candle_time": candles[i]["time"],
        })
    return signals

def detect_signal(candles: list[dict[str, Any]], *, atr_multiplier: float = 1.5,
                  tp1_r: float = 1.0, tp2_r: float = 2.0) -> dict[str, Any] | None:
    """Return a signal only when the newest completed candle triggers one."""
    if not candles:
        return None
    signals = detect_signals(candles, atr_multiplier=atr_multiplier, tp1_r=tp1_r, tp2_r=tp2_r)
    latest_time = candles[-1]["time"]
    return next((signal for signal in reversed(signals)
                 if signal["candle_time"] == latest_time), None)
