"""Telegram message formatting and signal-only delivery."""
from __future__ import annotations

from typing import Any

import requests

from .config import Config

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def format_signal(data: dict[str, Any], timeframe: str) -> str:
    stamp = data["candle_time"].strftime("%Y-%m-%d %H:%M UTC")
    return (
        "🟡 <b>GOLD / XAUUSD — SIGNAL</b>\n"
        f"<b>جهت:</b> {data['side']}\n"
        f"<b>تایم‌فریم:</b> {timeframe}\n"
        f"<b>ورود مرجع:</b> {data['entry']:.2f}\n"
        f"<b>حد ضرر:</b> {data['stop_loss']:.2f}\n"
        f"<b>حد سود ۱:</b> {data['take_profit_1']:.2f}\n"
        f"<b>حد سود ۲:</b> {data['take_profit_2']:.2f}\n"
        f"<b>RSI(14):</b> {data['rsi']:.1f} | <b>ATR(14):</b> {data['atr']:.2f}\n"
        f"<b>کندل تأییدشده:</b> {stamp}\n\n"
        "روش: EMA20/EMA50 + شکست EMA20 + فیلتر RSI؛ SL بر پایه ATR.\n"
        "این فقط هشدار تحلیلی است؛ هیچ سفارشی اجرا نشده است. قیمت زنده و اسپرد کارگزار خود را بررسی کنید."
    )


def format_expiry(
    data: dict[str, Any], timeframe: str, expiry_at, close_price: float,
    ttl_candles: int,
) -> str:
    """Format an explicit lifecycle-expiry notice; never implies an order was executed."""
    stamp = expiry_at.strftime("%Y-%m-%d %H:%M UTC")
    side = data.get("side", "نامشخص")
    return (
        "⏳ <b>GOLD / XAUUSD — SIGNAL EXPIRED</b>\\n"
        f"<b>جهت:</b> {side}\\n"
        f"<b>تایم‌فریم:</b> {timeframe}\\n"
        f"<b>قیمت بسته‌شدن:</b> {close_price:.2f}\\n"
        f"<b>زمان انقضا:</b> {stamp}\\n"
        f"<b>اعتبار:</b> {ttl_candles} کندل بسته‌شده\\n\\n"
        "این سیگنال منقضی شد و دیگر معتبر نیست. هیچ سفارشی اجرا نشده است."
    )


def send_telegram(
    config: Config,
    message: str,
    session: requests.Session | None = None,
) -> int | None:
    """Send once and return Telegram's message_id when the API confirms success.

    The caller owns retry policy because a timeout can occur after Telegram has
    accepted the message. Blind POST retries here could create silent duplicates.
    """
    client = session or requests.Session()
    response = client.post(
        TELEGRAM_URL.format(token=config.bot_token),
        data={
            "chat_id": config.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=config.http_timeout,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(
            "Telegram rejected the message; check bot token, chat ID, and bot permissions"
        )
    payload = result.get("result")
    if isinstance(payload, dict):
        message_id = payload.get("message_id")
        if isinstance(message_id, int):
            return message_id
    return None
