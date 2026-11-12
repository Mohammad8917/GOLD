"""OHLC-based signal lifecycle events; this module never executes orders."""
from __future__ import annotations

from datetime import datetime
import math


def detect_lifecycle_events(record: dict, candle: dict) -> list[str]:
    """Return lifecycle events for one completed candle using conservative stop-first rules."""
    side = str(record.get("side", "")).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported signal side for lifecycle: {side!r}")

    try:
        stop = float(record["stop_loss"])
        tp1 = float(record["take_profit_1"])
        tp2 = float(record["take_profit_2"])
        high, low = float(candle["high"]), float(candle["low"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "Cannot track signal lifecycle: numeric SL, TP1, TP2 and OHLC levels are required"
        ) from exc
    if not all(math.isfinite(value) for value in (stop, tp1, tp2, high, low)):
        raise RuntimeError("Cannot track signal lifecycle: all price levels must be finite")
    if min(stop, tp1, tp2, high, low) <= 0 or high < low:
        raise RuntimeError(
            "Cannot track signal lifecycle: price levels must be positive and candle high must be >= low"
        )

    stop_hit = low <= stop if side == "BUY" else high >= stop
    if stop_hit:
        # Match the backtest's conservative rule when SL and a target touch
        # inside the same OHLC candle: assume the stop happened first.
        return ["SL"]

    tp1_hit = high >= tp1 if side == "BUY" else low <= tp1
    tp2_hit = high >= tp2 if side == "BUY" else low <= tp2
    already_tp1 = bool(record.get("tp1_hit"))

    if tp2_hit:
        return (["TP1"] if not already_tp1 else []) + ["TP2"]
    if tp1_hit and not already_tp1:
        return ["TP1"]
    return []


def format_lifecycle_notice(record: dict, event: str, candle_time: datetime) -> str:
    """Describe simulated signal progress without implying a real position was traded."""
    side = record.get("side", "نامشخص")
    stamp = candle_time.strftime("%Y-%m-%d %H:%M UTC")
    levels = {
        "TP1": ("حد سود ۱", float(record["take_profit_1"])),
        "TP2": ("حد سود ۲", float(record["take_profit_2"])),
        "SL": ("حد ضرر", float(record["stop_loss"])),
    }
    if event not in levels:
        raise ValueError(f"Unsupported lifecycle event: {event!r}")
    label, price = levels[event]
    header = f"🟡 <b>GOLD / XAUUSD — {event}</b>"
    if event == "TP1":
        detail = (
            "TP1 برخورد — طبق مدل سیگنال، ۵۰٪ حجم فرضی محقق شد؛ "
            "بخش باقی‌مانده تا TP2 یا SL پایش می‌شود."
        )
    elif event == "TP2":
        detail = "TP2 برخورد — سیگنال کامل شد و بخش باقی‌مانده طبق مدل فرضی بسته شد."
    elif record.get("tp1_hit"):
        detail = (
            "حد ضرر بخش باقی‌مانده لمس شد. نتیجه کل پس از TP1 و هزینه‌های اجرا "
            "باید جداگانه محاسبه شود؛ لزوماً زیان خالص نیست."
        )
    else:
        detail = "حد ضرر لمس شد — سیگنال با زیان فرضی بسته شد."
    return (
        f"{header}\n"
        f"<b>جهت:</b> {side}\n"
        f"<b>{label}:</b> {price:.2f}\n"
        f"<b>کندل تأیید:</b> {stamp}\n\n"
        f"{detail}\n"
        "این فقط پیگیری سیگنال بر اساس OHLC است؛ هیچ سفارش واقعی اجرا یا بسته نشده است."
    )
