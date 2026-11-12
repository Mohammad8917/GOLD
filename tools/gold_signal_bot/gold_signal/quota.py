"""Typed Twelve Data quota errors and API usage inspection.

The usage endpoint costs one API credit, so it is queried only after a 429
response (never on every polling cycle).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import requests

API_USAGE_URL = "https://api.twelvedata.com/api_usage"
DAILY_QUOTA_NEAR_LIMIT_RATIO = 0.95


class QuotaLimitError(RuntimeError):
    """A provider quota window is exhausted; retry_after is seconds until reset."""

    def __init__(self, window: str, retry_after: float, *, usage: dict[str, Any] | None = None):
        self.window = window
        self.retry_after = max(1.0, float(retry_after))
        self.usage = usage or {}
        if window == "daily":
            message = "Twelve Data daily API credit limit reached; paused until 00:00 UTC"
        else:
            message = "Twelve Data per-minute API credit limit reached; waiting for the next minute"
        super().__init__(f"{message} (retry in about {int(self.retry_after)} seconds)")


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_to_next_minute(now: datetime) -> float:
    boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1, seconds=1)
    return max(1.0, (boundary - now).total_seconds())


def _seconds_to_next_utc_day(now: datetime) -> float:
    boundary = (now + timedelta(days=1)).replace(hour=0, minute=0, second=2, microsecond=0)
    return max(1.0, (boundary - now).total_seconds())


def inspect_api_usage(
    client: requests.Session, *, api_key: str, timeout: int
) -> dict[str, Any] | None:
    """Read current plan usage after a quota response; costs one Twelve Data credit."""
    response = client.get(
        API_USAGE_URL,
        params={"apikey": api_key, "format": "JSON", "timezone": "UTC"},
        timeout=timeout,
    )
    try:
        if response.status_code == 401:
            raise RuntimeError("Twelve Data API key is invalid or missing (HTTP 401); replace the key")
        if response.status_code == 403:
            raise RuntimeError("Twelve Data access is forbidden (HTTP 403); check symbol and plan permissions")
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("status") == "error":
            return None
        return payload
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def quota_error(
    client: requests.Session,
    *,
    api_key: str,
    timeout: int,
    message: str = "",
    retry_after: str | None = None,
) -> QuotaLimitError:
    """Classify 429 using /api_usage where possible; fall back to provider message."""
    usage: dict[str, Any] | None = None
    try:
        usage = inspect_api_usage(client, api_key=api_key, timeout=timeout)
    except requests.RequestException:
        # The usage endpoint can itself be unavailable when a quota is exhausted.
        usage = None

    now = datetime.now(timezone.utc)
    if usage:
        try:
            daily_used = int(usage.get("daily_usage"))
            daily_limit = int(usage.get("plan_daily_limit"))
        except (TypeError, ValueError):
            daily_used = daily_limit = -1
        # A 429 plus daily usage at >=95% of the account's daily allowance is
        # conservatively treated as daily exhaustion; otherwise it is minute-level.
        # The threshold is relative to the live plan limit, never hard-coded to Basic.
        daily_threshold = int(daily_limit * DAILY_QUOTA_NEAR_LIMIT_RATIO + 0.999999)
        if daily_limit > 0 and daily_used >= daily_threshold:
            return QuotaLimitError("daily", _seconds_to_next_utc_day(now), usage=usage)
        if daily_limit > 0:
            # The usage timestamp may be cached or delayed. Schedule the retry from
            # this process's current UTC clock, not from the provider snapshot.
            return QuotaLimitError("minute", _seconds_to_next_minute(now), usage=usage)

    lowered = message.lower()
    if "daily" in lowered or "per day" in lowered or "current day" in lowered:
        return QuotaLimitError("daily", _seconds_to_next_utc_day(now))

    try:
        retry_seconds = float(retry_after) if retry_after else 0.0
    except (TypeError, ValueError):
        retry_seconds = 0.0
    return QuotaLimitError(
        "minute",
        retry_seconds if retry_seconds > 0 else _seconds_to_next_minute(now),
    )
