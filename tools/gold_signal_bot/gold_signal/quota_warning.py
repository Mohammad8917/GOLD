"""Once-daily pre-emptive Telegram warning for Basic-plan daily credits."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .quota import inspect_api_usage
from .telegram import send_telegram

LOG = logging.getLogger("gold_signal_bot")
DAILY_WARNING_RATIO = 0.80


def _state_path(config) -> Path:
    return config.state_file.with_name(config.state_file.name + ".quota-warning.json")


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError("Quota-warning state is unreadable; refusing to reset daily warning history") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Quota-warning state must be a JSON object")
    return value


def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def maybe_send_daily_quota_warning(config, session: requests.Session | None = None) -> bool:
    """Inspect usage at most once per UTC day and send one warning at >=80%.

    The usage endpoint costs one credit. A pending message is persisted before
    Telegram delivery so a temporary Telegram failure does not cause repeated
    usage-endpoint calls on every polling cycle.
    """
    # Production Config requires a Twelve Data key. Test doubles and
    # non-market-data callers may omit it; do not issue a billable request.
    api_key = getattr(config, "api_key", "")
    if not api_key:
        return False

    path = _state_path(config)
    state = _read_state(path)
    today = datetime.now(timezone.utc).date().isoformat()

    pending = state.get("pending_message")
    if pending and state.get("pending_warning_date") != today:
        # A warning left undelivered across UTC midnight is stale after the
        # provider's daily quota reset; do not send yesterday's warning today.
        state.pop("pending_message", None)
        state.pop("pending_warning_date", None)
        state.pop("warning_delivery_status", None)
        pending = None
    if state.get("last_usage_check_date") != today:
        # Persist the attempt before making the billable request. If the
        # endpoint times out, do not poll it again on every notifier cycle.
        state["last_usage_check_date"] = today
        _write_state(path, state)
        client = session or requests.Session()
        usage = inspect_api_usage(client, api_key=config.api_key, timeout=config.http_timeout)
        try:
            used = int(usage.get("daily_usage")) if usage else -1
            limit = int(usage.get("plan_daily_limit")) if usage else -1
        except (TypeError, ValueError):
            used = limit = -1

        if limit > 0 and used >= 0 and used / limit >= DAILY_WARNING_RATIO:
            remaining = max(0, limit - used)
            percent = used * 100.0 / limit
            state["pending_warning_date"] = today
            state["pending_message"] = (
                "⚠️ <b>GOLD — هشدار مصرف API</b>\n"
                f"<b>مصرف روزانه:</b> {used:,} از {limit:,} اعتبار ({percent:.1f}٪)\n"
                f"<b>اعتبار باقی‌مانده:</b> {remaining:,}\n"
                "<b>بازنشانی سهمیه:</b> ساعت 00:00 UTC\n\n"
                "برای جلوگیری از توقف دریافت داده، دفعات بررسی را کاهش دهید. "
                "این ربات فقط سیگنال ارسال می‌کند و معامله‌ای اجرا نمی‌کند."
            )
        _write_state(path, state)
        pending = state.get("pending_message")

    if not pending:
        return False

    message = str(pending)
    if state.get("warning_delivery_status") == "UNKNOWN":
        message = "<b>[RECOVERY RETRY]</b>\n" + message
    state["warning_delivery_status"] = "SENDING"
    _write_state(path, state)
    try:
        message_id = send_telegram(config, message, session=session)
    except Exception:
        state["warning_delivery_status"] = "UNKNOWN"
        _write_state(path, state)
        raise

    if type(message_id) is not int or message_id <= 0:
        state["warning_delivery_status"] = "UNKNOWN"
        _write_state(path, state)
        raise RuntimeError("Quota warning send succeeded without a valid Telegram message_id")

    state["last_quota_warning_date"] = today
    state["warning_message_id"] = message_id
    state["warning_delivery_status"] = "SENT"
    state.pop("pending_message", None)
    _write_state(path, state)
    LOG.warning("Daily API quota reached at least %.0f%%; Telegram warning delivered", DAILY_WARNING_RATIO * 100)
    return True
