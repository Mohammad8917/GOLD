"""Twelve Data transport, timestamp parsing, and OHLC history validation."""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .config import Config, TIMEFRAMES

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

def parse_bar_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Candle is missing datetime")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _is_expected_gold_pause(start: datetime, end: datetime) -> bool:
    """Conservative UTC exceptions for the daily maintenance break and weekend closure.

    Spot-metal provider sessions vary and can shift with DST. This deliberately
    exempts only obvious pause windows; tune data-age/gap limits for the provider.
    """
    if start.weekday() == 4 and (start.hour > 20 or (start.hour == 20 and start.minute >= 30)):
        if end.weekday() == 5 or (end.weekday() == 6 and (end.hour < 22 or (end.hour == 22 and end.minute == 0))):
            return True
    if start.weekday() == 5:
        if end.weekday() == 5 or (end.weekday() == 6 and (end.hour < 22 or (end.hour == 22 and end.minute == 0))):
            return True
    if start.weekday() == 6 and end.weekday() == 6 and (end.hour < 22 or (end.hour == 22 and end.minute == 0)):
        return True
    # Common one-hour daily liquidity/maintenance pause, with a 30-minute margin.
    if start.date() == end.date():
        pause_start = start.replace(hour=20, minute=30, second=0, microsecond=0)
        pause_end = start.replace(hour=22, minute=30, second=0, microsecond=0)
        return start >= pause_start and end <= pause_end
    return False

def validate_candles(candles: list[dict[str, Any]], *, timeframe_seconds: int,
                     now: datetime | None = None, stale_multiplier: float = 3.0,
                     gap_multiplier: float = 1.5) -> list[dict[str, Any]]:
    """Validate sorted, unique, closed OHLC bars and reject stale/incomplete history."""
    if timeframe_seconds <= 0:
        raise ValueError("timeframe_seconds must be positive")
    if not candles:
        raise RuntimeError("No valid XAU/USD candles received")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    by_time: dict[datetime, dict[str, Any]] = {}
    for source in candles:
        try:
            stamp = parse_bar_time(source["time"])
            values = {key: float(source[key]) for key in ("open", "high", "low", "close")}
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("A candle has a missing or invalid timestamp/OHLC field") from exc
        if not all(math.isfinite(v) for v in values.values()):
            raise RuntimeError("A candle contains a non-finite OHLC value")
        o, h, low, close = values["open"], values["high"], values["low"], values["close"]
        if low <= 0 or h < max(o, close, low) or low > min(o, close):
            raise RuntimeError("A candle violates OHLC price invariants")
        if stamp + timedelta(seconds=timeframe_seconds) > current:
            raise RuntimeError("Provider returned an unclosed or future XAU/USD candle")
        normalized = {"time": stamp, **values}
        previous = by_time.get(stamp)
        if previous is not None and previous != normalized:
            raise RuntimeError("Provider returned conflicting duplicate candles")
        by_time[stamp] = normalized
    result = [by_time[key] for key in sorted(by_time)]
    if len(result) < 60:
        raise RuntimeError(f"Only {len(result)} valid closed XAU/USD candles received; at least 60 are required")
    latest_end = result[-1]["time"] + timedelta(seconds=timeframe_seconds)
    age_seconds = (current - latest_end).total_seconds()
    max_age = timeframe_seconds * stale_multiplier
    if age_seconds > max_age and not _is_expected_gold_pause(latest_end, current):
        raise RuntimeError(
            f"XAU/USD data is stale: latest closed candle is {int(age_seconds)} seconds old "
            f"(limit {int(max_age)} seconds)"
        )
    for left, right in zip(result, result[1:]):
        gap_seconds = (right["time"] - left["time"]).total_seconds()
        if gap_seconds > timeframe_seconds * gap_multiplier:
            left_end = left["time"] + timedelta(seconds=timeframe_seconds)
            if not _is_expected_gold_pause(left_end, right["time"]):
                raise RuntimeError(
                    f"Missing XAU/USD candles: unexpected {int(gap_seconds)}-second gap "
                    f"after {left['time'].isoformat()}"
                )
    return result

def _get_with_retry(client: requests.Session, url: str, *, params: dict[str, Any],
                    timeout: int, attempts: int, base_delay: float) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params, timeout=timeout)
            if response.status_code in (408, 425, 429) or response.status_code >= 500:
                if attempt + 1 == attempts:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(30.0, max(0.0, float(retry_after)))
                except (TypeError, ValueError):
                    delay = min(30.0, base_delay * (2 ** attempt))
                time.sleep(delay)
                continue
            response.raise_for_status()
            return response
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                raise
            time.sleep(min(30.0, base_delay * (2 ** attempt)))
    if last_error:
        raise last_error
    raise RuntimeError("Market-data request failed after retries")

def fetch_closed_candles(config: Config, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Fetch spot XAU/USD from Twelve Data only; never substitute futures."""
    client = session or requests.Session()
    response = _get_with_retry(
        client, TWELVE_DATA_URL,
        params={"symbol": "XAU/USD", "interval": config.timeframe, "outputsize": 120,
                "timezone": "UTC", "format": "ISO", "apikey": config.api_key},
        timeout=config.http_timeout, attempts=config.retry_attempts,
        base_delay=config.retry_base_seconds,
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Twelve Data returned an unexpected response")
    if payload.get("status") == "error" or payload.get("code"):
        message = str(payload.get("message") or payload.get("code") or "unknown provider error")
        if "rate" in message.lower() or str(payload.get("code")) == "429":
            raise RuntimeError("Twelve Data rate limit reached; retry on the next polling cycle")
        raise RuntimeError(f"Twelve Data error: {message[:250]}")
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError("Twelve Data returned no XAU/USD candles")
    parsed = []
    for item in values:
        if not isinstance(item, dict):
            raise RuntimeError("Twelve Data returned a malformed candle record")
        try:
            parsed.append({"time": parse_bar_time(item["datetime"]),
                           "open": float(item["open"]), "high": float(item["high"]),
                           "low": float(item["low"]), "close": float(item["close"])})
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("Twelve Data returned a candle with invalid fields") from exc
    seconds = TIMEFRAMES[config.timeframe][1]
    return validate_candles(parsed, timeframe_seconds=seconds,
                            stale_multiplier=config.stale_multiplier,
                            gap_multiplier=config.gap_multiplier)
