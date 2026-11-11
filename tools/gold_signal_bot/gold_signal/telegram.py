"""Telegram message formatting and signal-only delivery."""
from __future__ import annotations

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

def send_telegram(config: Config, message: str, session: requests.Session | None = None) -> None:
    """Do not blindly retry POST: a timeout can happen after Telegram accepted it."""
    client = session or requests.Session()
    response = client.post(TELEGRAM_URL.format(token=config.bot_token),
                           data={"chat_id": config.chat_id, "text": message,
                                 "parse_mode": "HTML", "disable_web_page_preview": "true"},
                           timeout=config.http_timeout)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the message; check bot token, chat ID, and bot permissions")
