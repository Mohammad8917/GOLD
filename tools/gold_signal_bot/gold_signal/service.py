"""Application orchestration with chronological catch-up and a Telegram delivery WAL."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import requests

from .config import Config, TIMEFRAMES, default_signal_ttl_candles
from .market_data import fetch_closed_candles, parse_bar_time
from .strategy import detect_signals
from .strategy_version import STRATEGY_VERSION
from .state import (
    process_lock, read_outbox, read_state, write_outbox, write_state,
)
from .telegram import format_expiry, format_signal, send_telegram
from .lifecycle import detect_lifecycle_events, format_lifecycle_notice
from .quota_warning import maybe_send_daily_quota_warning

LOG = logging.getLogger("gold_signal_bot")

INDICATOR_WARMUP_BARS = 200


def _signal_id(signal: dict, timeframe: str) -> str:
    stamp = signal["candle_time"].isoformat()
    identity = f'{signal.get("symbol", "XAU/USD")}|{timeframe}|{stamp}|{STRATEGY_VERSION}'
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _signal_is_fresh(candle_time: datetime, timeframe: str, *, now: datetime | None = None,
                     grace_seconds: int = 300) -> bool:
    """A signal may be announced only within five minutes after its candle closes."""
    if candle_time.tzinfo is None:
        candle_time = candle_time.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Freshness clock must be timezone-aware")
    interval = TIMEFRAMES[timeframe][1]
    close_at = candle_time + timedelta(seconds=interval)
    age = (current.astimezone(timezone.utc) - close_at.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= grace_seconds


def _redact_error(error: Exception, config: Config) -> str:
    value = str(error)
    for secret in (getattr(config, "api_key", ""), getattr(config, "bot_token", "")):
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value[:500]

def _append_error_history(record: dict, *, signal_id: str, error: str, at: str) -> None:
    """Keep the five most recent auditable delivery errors for one signal."""
    history = record.setdefault("error_history", [])
    if not isinstance(history, list):
        history = []
    history.append({
        "signal_id": signal_id,
        "timestamp": record.get("signal_timestamp", ""),
        "at": at,
        "error": str(error)[:500],
    })
    record["error_history"] = history[-5:]


def _recover_startup_sending(outbox: dict[str, dict], state_path) -> None:
    """Recover ambiguous initial and expiry sends after a process restart."""
    changed = False
    now = datetime.now(timezone.utc).isoformat()
    for key, record in outbox.items():
        if record.get("stale_notice_status") == "SENDING":
            record["stale_notice_status"] = "UNKNOWN"
            record["stale_notice_updated_at"] = now
            record["stale_notice_error"] = record.get("stale_notice_error") or "Process stopped while stale summary was SENDING; Telegram outcome is unknown"
            record.setdefault("recovery_history", []).append({"at": now, "from": "STALE_NOTICE_SENDING", "to": "STALE_NOTICE_UNKNOWN", "decision": "startup recovery; retry summary with [RECOVERY RETRY]"})
            changed = True
            LOG.error("Startup recovery: stale summary group %s has ambiguous delivery", record.get("stale_notice_group"))

        if record.get("status") == "SENDING":
            record["status"] = "UNKNOWN"
            record["updated_at"] = now
            recovery_error = "Process stopped while delivery was marked SENDING; Telegram outcome is unknown"
            record["last_error"] = record.get("last_error") or recovery_error
            _append_error_history(record, signal_id=key, error=recovery_error, at=now)
            record.setdefault("recovery_history", []).append({
                "at": now, "from": "SENDING", "to": "UNKNOWN",
                "decision": "startup recovery; retry with [RECOVERY RETRY]",
            })
            changed = True
            LOG.error(
                "Startup recovery: signal %s was left SENDING; status is UNKNOWN; "
                "decision: retry with [RECOVERY RETRY]",
                key,
            )

        if record.get("expiry_status") == "SENDING":
            record["expiry_status"] = "UNKNOWN"
            record["expiry_updated_at"] = now
            recovery_error = "Process stopped while expiry notification was marked SENDING; Telegram outcome is unknown"
            record["expiry_error"] = record.get("expiry_error") or recovery_error
            _append_error_history(record, signal_id=key, error=recovery_error, at=now)
            record.setdefault("recovery_history", []).append({
                "at": now, "from": "EXPIRY_SENDING", "to": "EXPIRY_UNKNOWN",
                "decision": "startup recovery; retry expiry with [RECOVERY RETRY]",
            })
            changed = True
            LOG.error("Startup recovery: expiry notice for signal %s was ambiguous; retrying visibly", key)

        lifecycle_notice = record.get("lifecycle_notice")
        if isinstance(lifecycle_notice, dict) and lifecycle_notice.get("status") == "SENDING":
            lifecycle_notice["status"] = "UNKNOWN"
            lifecycle_notice["updated_at"] = now
            lifecycle_notice["error"] = (
                lifecycle_notice.get("error")
                or "Process stopped while lifecycle notice was marked SENDING; Telegram outcome is unknown"
            )
            record["lifecycle_notice"] = lifecycle_notice
            record.setdefault("recovery_history", []).append({
                "at": now, "from": "LIFECYCLE_SENDING", "to": "LIFECYCLE_UNKNOWN",
                "decision": "startup recovery; retry lifecycle notice with [RECOVERY RETRY]",
            })
            changed = True
            LOG.error("Startup recovery: lifecycle notice for signal %s was ambiguous; retrying visibly", key)

        outbox[key] = record
    if changed:
        write_outbox(state_path, outbox)



def _process_stale_notice_groups(config: Config, outbox: dict[str, dict], session: requests.Session | None) -> int:
    """Send one durable, silent summary per evaluation-candle boundary."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    for key, record in outbox.items():
        group = record.get("stale_notice_group")
        if group and record.get("stale_notice_status") in {"PENDING", "UNKNOWN", "FAILED"}:
            groups.setdefault(str(group), []).append((key, record))

    sent = 0
    for group, members in sorted(groups.items()):
        cursor_raw = read_state(config.state_file)
        if not cursor_raw or parse_bar_time(cursor_raw) < parse_bar_time(group):
            continue  # wait until the whole catch-up boundary has been processed

        stamps = [parse_bar_time(record["signal_timestamp"]) for _, record in members]
        first_stamp, last_stamp = min(stamps), max(stamps)
        count = len(members)
        timeframe = str(members[0][1].get("timeframe", config.timeframe))
        message = (
            "⚠️ <b>GOLD / XAUUSD — STALE SIGNALS SKIPPED</b>\\n"
            f"<b>تعداد:</b> {count} سیگنال\\n"
            f"<b>بازه کندل‌ها:</b> {first_stamp:%Y-%m-%d %H:%M} تا {last_stamp:%Y-%m-%d %H:%M} UTC\\n"
            f"<b>تایم‌فریم:</b> {timeframe}\\n\\n"
            "این سیگنال‌های قدیمی ارسال نشدند؛ هیچ سفارش یا معامله‌ای اجرا نشده است."
        )
        if any(record.get("stale_notice_status") == "UNKNOWN" for _, record in members):
            message = "<b>[RECOVERY RETRY]</b>\\n" + message

        now = datetime.now(timezone.utc).isoformat()
        for _, record in members:
            record["stale_notice_status"] = "SENDING"
            record["stale_notice_attempt_count"] = int(record.get("stale_notice_attempt_count", 0)) + 1
            record["stale_notice_updated_at"] = now
            record["stale_notice_error"] = None
        write_outbox(config.state_file, outbox)

        try:
            message_id = send_telegram(config, message, session=session, disable_notification=True)
            if type(message_id) is not int or message_id <= 0:
                raise RuntimeError("Stale summary send succeeded without a valid Telegram message_id")
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            definitive = (
                isinstance(exc, RuntimeError) and str(exc).startswith("Telegram rejected")
            ) or (
                isinstance(exc, requests.HTTPError) and isinstance(status_code, int)
                and 400 <= status_code < 500 and status_code not in {408, 425, 429}
            )
            status = "FAILED" if definitive else "UNKNOWN"
            error = _redact_error(exc, config)
            for key, record in members:
                record["stale_notice_status"] = status
                record["stale_notice_error"] = error
                record["stale_notice_updated_at"] = datetime.now(timezone.utc).isoformat()
                record.setdefault("recovery_history", []).append({
                    "at": record["stale_notice_updated_at"], "from": "STALE_NOTICE_SENDING",
                    "to": "STALE_NOTICE_" + status,
                    "decision": "retry summary; signal cursor remains committed",
                })
                outbox[key] = record
            write_outbox(config.state_file, outbox)
            LOG.error("Stale summary group %s failed: %s", group, error)
            continue  # informational notice must not block signal catch-up

        now = datetime.now(timezone.utc).isoformat()
        for key, record in members:
            record["stale_notice_status"] = "SENT"
            record["stale_notice_message_id"] = message_id
            record["stale_notice_updated_at"] = now
            outbox[key] = record
        write_outbox(config.state_file, outbox)
        sent += 1
        LOG.info("Delivered one silent stale summary for %d skipped signals at boundary %s", count, group)
    return sent

def _trim_sent_history(outbox: dict[str, dict], limit: int = 1000) -> None:
    """Bound the audit journal while never deleting an unfinished delivery."""
    sent = sorted(
        (
            (record.get("updated_at", ""), key)
            for key, record in outbox.items()
            if record.get("status") == "SENT" and record.get("lifecycle_status") in {"EXPIRED", "TP2_COMPLETE", "SL_CLOSED"}
        )
    )
    for _, key in sent[:-limit]:
        outbox.pop(key, None)


def _process_due_expirations(
    config: Config, candles: list[dict], outbox: dict[str, dict],
    current_index: int, session: requests.Session | None,
    *, index_by_time: dict | None = None,
) -> int:
    """Send durable expiry notices when a signal reaches its TTL candle."""
    if index_by_time is None:
        index_by_time = {bar["time"]: index for index, bar in enumerate(candles)}
    sent_count = 0
    interval = TIMEFRAMES[config.timeframe][1]
    ttl_candles = getattr(config, "signal_ttl_candles", None)
    if ttl_candles is None:
        ttl_candles = default_signal_ttl_candles(config.timeframe)

    for key, record in list(outbox.items()):
        if record.get("status") != "SENT" or record.get("lifecycle_status") in {"EXPIRED", "TP2_COMPLETE", "SL_CLOSED"}:
            continue
        raw_stamp = record.get("signal_timestamp")
        if not raw_stamp:
            continue
        signal_time = parse_bar_time(raw_stamp)
        signal_index = index_by_time.get(signal_time)
        if signal_index is None:
            if signal_time < candles[0]["time"]:
                raise RuntimeError(
                    f"Cannot determine expiry for active signal {key}: its signal candle "
                    "is older than retained market history; refusing silent expiry."
                )
            continue

        expiry_index = signal_index + ttl_candles
        if expiry_index > current_index or expiry_index >= len(candles):
            continue
        expiry_bar = candles[expiry_index]
        expiry_at = expiry_bar["time"] + timedelta(seconds=interval)

        if record.get("expiry_status") == "SENT":
            record["lifecycle_status"] = "EXPIRED"
            record["expired_at"] = expiry_at.isoformat()
            outbox[key] = record
            write_outbox(config.state_file, outbox)
            continue
        if record.get("expiry_status") == "SENDING":
            raise RuntimeError(
                f"WAL invariant violation: expiry notification for signal {key} is SENDING outside startup recovery"
            )

        if not record.get("expiry_status"):
            record["expiry_status"] = "PENDING"
            record["expiry_message"] = format_expiry(
                record, config.timeframe, expiry_at, float(expiry_bar["close"]),
                ttl_candles,
            )
            record["expiry_candle_time"] = expiry_bar["time"].isoformat()
            record["expiry_at"] = expiry_at.isoformat()
            record["expiry_close"] = float(expiry_bar["close"])
            record["expiry_attempt_count"] = 0
            record["expiry_message_id"] = None
            record["expiry_error"] = None
            outbox[key] = record
            write_outbox(config.state_file, outbox)

        recovery = record.get("expiry_status") == "UNKNOWN"
        message = record["expiry_message"]
        if recovery:
            message = "<b>[RECOVERY RETRY]</b>\n" + message

        record["expiry_status"] = "SENDING"
        record["expiry_attempt_count"] = int(record.get("expiry_attempt_count", 0)) + 1
        record["expiry_updated_at"] = datetime.now(timezone.utc).isoformat()
        outbox[key] = record
        write_outbox(config.state_file, outbox)

        try:
            message_id = send_telegram(config, message, session=session)
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            definitive_rejection = (
                isinstance(exc, RuntimeError) and str(exc).startswith("Telegram rejected")
            ) or (
                isinstance(exc, requests.HTTPError)
                and isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in {408, 425, 429}
            )
            record["expiry_status"] = "FAILED" if definitive_rejection else "UNKNOWN"
            record["expiry_error"] = _redact_error(exc, config)
            record["expiry_updated_at"] = datetime.now(timezone.utc).isoformat()
            _append_error_history(
                record, signal_id=key, error=record["expiry_error"],
                at=record["expiry_updated_at"],
            )
            record.setdefault("recovery_history", []).append({
                "at": record["expiry_updated_at"],
                "from": "EXPIRY_SENDING",
                "to": "EXPIRY_" + record["expiry_status"],
                "decision": (
                    "retry definitive rejection without recovery marker"
                    if definitive_rejection else "retry ambiguous expiry send with [RECOVERY RETRY]"
                ),
            })
            outbox[key] = record
            write_outbox(config.state_file, outbox)
            raise

        if type(message_id) is not int or message_id <= 0:
            record["expiry_status"] = "UNKNOWN"
            record["expiry_error"] = "Expiry send succeeded without a valid Telegram message_id"
            record["expiry_updated_at"] = datetime.now(timezone.utc).isoformat()
            _append_error_history(
                record, signal_id=key, error=record["expiry_error"],
                at=record["expiry_updated_at"],
            )
            outbox[key] = record
            write_outbox(config.state_file, outbox)
            raise RuntimeError(
                f"Expiry notification for signal {key} has UNKNOWN delivery outcome; "
                "it will retry with [RECOVERY RETRY]"
            )

        record["expiry_status"] = "SENT"
        record["expiry_message_id"] = message_id
        record["expiry_updated_at"] = datetime.now(timezone.utc).isoformat()
        record["lifecycle_status"] = "EXPIRED"
        record["expired_at"] = expiry_at.isoformat()
        outbox[key] = record
        write_outbox(config.state_file, outbox)
        sent_count += 1
        LOG.info(
            "Delivered expiry notice for signal %s at %s (Telegram message_id=%s)",
            key, expiry_at.isoformat(), message_id,
        )

    return sent_count



def _process_due_lifecycle(
    config: Config, candles: list[dict], outbox: dict[str, dict],
    current_index: int, session: requests.Session | None,
    *, index_by_time: dict | None = None,
) -> int:
    """Detect and durably notify TP1/TP2/SL events from completed OHLC candles."""
    if index_by_time is None:
        index_by_time = {bar["time"]: index for index, bar in enumerate(candles)}
    ttl_candles = getattr(config, "signal_ttl_candles", None)
    if ttl_candles is None:
        ttl_candles = default_signal_ttl_candles(config.timeframe)
    delivered = 0

    for key, record in list(outbox.items()):
        if record.get("status") != "SENT":
            continue
        if record.get("lifecycle_status") in {"EXPIRED", "TP2_COMPLETE", "SL_CLOSED"}:
            continue

        raw_signal_time = record.get("signal_timestamp")
        if not raw_signal_time:
            continue
        signal_time = parse_bar_time(raw_signal_time)
        signal_index = index_by_time.get(signal_time)
        if signal_index is None:
            if signal_time < candles[0]["time"]:
                raise RuntimeError(
                    f"Cannot evaluate lifecycle for active signal {key}: its signal candle "
                    "is older than retained market history; refusing silent completion."
                )
            raise RuntimeError(f"Cannot locate signal candle for lifecycle evaluation: {key}")

        last_checked_raw = record.get("lifecycle_last_checked_candle") or raw_signal_time
        last_checked_time = parse_bar_time(last_checked_raw)
        last_checked_index = index_by_time.get(last_checked_time)
        if last_checked_index is None:
            if last_checked_time < candles[0]["time"]:
                raise RuntimeError(
                    f"Cannot resume lifecycle for signal {key}: last checked candle "
                    "is older than retained market history."
                )
            raise RuntimeError(f"Cannot locate last lifecycle candle for signal {key}")

        pending = record.setdefault("pending_lifecycle_events", [])
        if not isinstance(pending, list):
            raise RuntimeError(f"Invalid lifecycle event queue for signal {key}")

        if not pending:
            expiry_index = signal_index + ttl_candles
            scan_end = min(current_index, expiry_index)
            if scan_end > last_checked_index:
                found_events = False
                for index in range(last_checked_index + 1, scan_end + 1):
                    candle = candles[index]
                    events = detect_lifecycle_events(record, candle)
                    record["lifecycle_last_checked_candle"] = candle["time"].isoformat()
                    if events:
                        for event in events:
                            pending.append({
                                "event": event,
                                "candle_time": candle["time"].isoformat(),
                                "message": format_lifecycle_notice(record, event, candle["time"]),
                            })
                        found_events = True
                        break
                if not found_events:
                    record["lifecycle_last_checked_candle"] = candles[scan_end]["time"].isoformat()
                outbox[key] = record
                write_outbox(config.state_file, outbox)

        while pending:
            event_record = pending[0]
            event = event_record.get("event")
            notice = record.get("lifecycle_notice")
            if notice is not None and (
                not isinstance(notice, dict) or notice.get("event") != event
            ):
                raise RuntimeError(f"Lifecycle WAL event mismatch for signal {key}")

            notice_status = notice.get("status") if isinstance(notice, dict) else "PENDING"
            if notice_status == "SENDING":
                raise RuntimeError(
                    f"WAL invariant violation: lifecycle notice for signal {key} "
                    "is SENDING outside startup recovery"
                )
            recovery = notice_status == "UNKNOWN"
            message = str(event_record["message"])
            if recovery:
                message = "<b>[RECOVERY RETRY]</b>\\n" + message

            notice = {
                "event": event,
                "candle_time": event_record["candle_time"],
                "status": "SENDING",
                "attempt_count": int((notice or {}).get("attempt_count", 0)) + 1,
                "message": str(event_record["message"]),
                "message_id": None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }
            record["lifecycle_notice"] = notice
            outbox[key] = record
            write_outbox(config.state_file, outbox)

            try:
                message_id = send_telegram(config, message, session=session)
            except Exception as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                definitive_rejection = (
                    isinstance(exc, RuntimeError) and str(exc).startswith("Telegram rejected")
                ) or (
                    isinstance(exc, requests.HTTPError)
                    and isinstance(status_code, int) and 400 <= status_code < 500 and status_code not in {408, 425, 429}
                )
                notice["status"] = "FAILED" if definitive_rejection else "UNKNOWN"
                notice["error"] = _redact_error(exc, config)
                notice["updated_at"] = datetime.now(timezone.utc).isoformat()
                record["lifecycle_notice"] = notice
                _append_error_history(
                    record, signal_id=key, error=notice["error"], at=notice["updated_at"],
                )
                outbox[key] = record
                write_outbox(config.state_file, outbox)
                raise

            if type(message_id) is not int or message_id <= 0:
                notice["status"] = "UNKNOWN"
                notice["error"] = "Lifecycle send succeeded without a valid Telegram message_id"
                notice["updated_at"] = datetime.now(timezone.utc).isoformat()
                record["lifecycle_notice"] = notice
                outbox[key] = record
                write_outbox(config.state_file, outbox)
                raise RuntimeError(
                    f"Lifecycle notice for signal {key} has UNKNOWN delivery outcome; "
                    "it will retry with [RECOVERY RETRY]"
                )

            notice["status"] = "SENT"
            notice["message_id"] = message_id
            notice["updated_at"] = datetime.now(timezone.utc).isoformat()
            history = record.setdefault("lifecycle_event_history", [])
            history.append({
                "event": event,
                "candle_time": event_record["candle_time"],
                "message_id": message_id,
                "sent_at": notice["updated_at"],
            })
            record["lifecycle_event_history"] = history[-10:]
            pending.pop(0)
            record["lifecycle_notice"] = None
            record["lifecycle_last_checked_candle"] = event_record["candle_time"]
            if event == "TP1":
                record["tp1_hit"] = True
                record["lifecycle_status"] = "TP1_HIT"
            elif event == "TP2":
                record["lifecycle_status"] = "TP2_COMPLETE"
                pending.clear()
            elif event == "SL":
                record["lifecycle_status"] = "SL_CLOSED"
                pending.clear()
            outbox[key] = record
            _trim_sent_history(outbox)
            write_outbox(config.state_file, outbox)
            delivered += 1
            LOG.info(
                "Delivered %s lifecycle notice for signal %s (Telegram message_id=%s)",
                event, key, message_id,
            )

    return delivered

def run_once(config: Config, session: requests.Session | None = None) -> str:
    """Process new closed candles; WAL recovery prefers a visible duplicate to a lost signal.

    A SENDING record found after restart is ambiguous: Telegram may have accepted
    the POST before the process died. It is changed to UNKNOWN and retried with
    an explicit [RECOVERY RETRY] marker. Exactly-once delivery is not claimed.
    """
    with process_lock(config.state_file):
        # Check the once-daily quota warning before fetching candles. This way
        # the warning can still be sent when the next market-data request is
        # rejected because the daily quota has just been exhausted.
        try:
            maybe_send_daily_quota_warning(config, session=session)
        except Exception as exc:
            LOG.warning("Daily quota warning check/delivery unavailable: %s", _redact_error(exc, config))
        candles = fetch_closed_candles(config, session=session)
        if len(candles) < 60:
            raise RuntimeError("Insufficient candle history for signal evaluation")

        candle_index_by_time = {bar["time"]: index for index, bar in enumerate(candles)}
        cursor_raw = read_state(config.state_file)
        outbox = read_outbox(config.state_file)
        # This is the only permitted SENDING -> UNKNOWN transition point.
        _recover_startup_sending(outbox, config.state_file)

        if not cursor_raw:
            start_index = len(candles) - 1
            LOG.info(
                "Initial startup mode: only current closed candle evaluated; "
                "%d previous candles intentionally ignored",
                start_index,
            )
        else:
            cursor = parse_bar_time(cursor_raw)
            first_time, latest_time = candles[0]["time"], candles[-1]["time"]
            if cursor < first_time:
                raise RuntimeError(
                    "Signal-processing cursor is older than available provider history; "
                    "refusing to skip potentially missed signals"
                )
            if cursor > latest_time:
                raise RuntimeError("Signal-processing cursor is newer than provider data")
            start_index = next(
                (i for i, candle in enumerate(candles) if candle["time"] > cursor),
                len(candles),
            )
        if start_index >= len(candles):
            lifecycle_delivered = _process_due_lifecycle(
                config, candles, outbox, len(candles) - 1, session,
                index_by_time=candle_index_by_time,
            )
            expiry_delivered = _process_due_expirations(
                config, candles, outbox, len(candles) - 1, session,
                index_by_time=candle_index_by_time,
            )
            stale_notices = _process_stale_notice_groups(config, outbox, session)
            if lifecycle_delivered:
                return "lifecycle_updated"
            if expiry_delivered:
                return "expired"
            if stale_notices:
                return "stale_notice_sent"
            LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
            return "no_signal"
        if start_index < INDICATOR_WARMUP_BARS:
            raise RuntimeError(
                "Insufficient indicator warm-up history before the next candle: "
                f"need {INDICATOR_WARMUP_BARS} prior candles, have {start_index}; "
                "refusing to calculate signals from an unstable indicator seed"
            )

        signals = {
            signal["candle_time"]: signal
            for signal in detect_signals(
                candles,
                atr_multiplier=config.atr_multiplier,
                tp1_r=config.tp1_r,
                tp2_r=config.tp2_r,
            )
        }

        delivered = 0
        processed = 0
        for index in range(start_index, len(candles)):
            stamp = candles[index]["time"]
            delivered += _process_due_lifecycle(
                config, candles, outbox, index, session,
                index_by_time=candle_index_by_time,
            )
            delivered += _process_due_expirations(
                config, candles, outbox, index, session,
                index_by_time=candle_index_by_time,
            )
            data = signals.get(stamp)
            if data:
                key = _signal_id(data, config.timeframe)
                record = outbox.get(key)

                if record and record.get("status") == "SENT":
                    # Telegram success was durably recorded before cursor commit.
                    # Keep the record as an audit entry including message_id.
                    write_state(config.state_file, stamp)
                    processed += 1
                    continue

                if record and record.get("status") == "SENDING":
                    raise RuntimeError(
                        f"WAL invariant violation: signal {key} is SENDING outside startup recovery"
                    )
                recovery = bool(record and record.get("status") == "UNKNOWN")

                # Catch-up candles advance the cursor and lifecycle WAL, but
                # historical entries are never announced as new signals.
                if not _signal_is_fresh(stamp, config.timeframe, grace_seconds=getattr(config, "freshness_grace_seconds", 300)):
                    if not record or record.get("status") != "SENT":
                        now_iso = datetime.now(timezone.utc).isoformat()
                        outbox[key] = {
                            "signal_id": key,
                            "signal_timestamp": stamp.isoformat(),
                            "symbol": data.get("symbol", "XAU/USD"),
                            "timeframe": config.timeframe,
                            "side": data.get("side"),
                            "entry": data.get("entry"),
                            "stop_loss": data.get("stop_loss"),
                            "take_profit_1": data.get("take_profit_1"),
                            "take_profit_2": data.get("take_profit_2"),
                            "status": "SKIPPED_STALE",
                            "lifecycle_status": "CLOSED_STALE",
                            "closed_at": now_iso,
                            "close_reason": "Historical catch-up signal; never delivered",
                            "stale_notice_group": latest_time.isoformat(),
                            "stale_notice_status": "PENDING",
                            "stale_notice_attempt_count": 0,
                            "stale_notice_message_id": None,
                            "stale_notice_error": None,
                            "updated_at": now_iso,
                        }
                        write_outbox(config.state_file, outbox)
                    write_state(config.state_file, stamp)
                    processed += 1
                    continue

                if not record:
                    message = format_signal(data, config.timeframe)
                    record = {
                        "signal_id": key,
                        "signal_timestamp": stamp.isoformat(),
                        "symbol": data.get("symbol", "XAU/USD"),
                        "timeframe": config.timeframe,
                        "status": "PENDING",
                        "attempt_count": 0,
                        "message": message,
                        "telegram_message_id": None,
                        "side": data["side"],
                        "entry": float(data["entry"]) if data.get("entry") is not None else None,
                        "stop_loss": float(data["stop_loss"]) if data.get("stop_loss") is not None else None,
                        "take_profit_1": float(data["take_profit_1"]) if data.get("take_profit_1") is not None else None,
                        "take_profit_2": float(data["take_profit_2"]) if data.get("take_profit_2") is not None else None,
                        "lifecycle_status": "ACTIVE",
                        "lifecycle_last_checked_candle": stamp.isoformat(),
                        "tp1_hit": False,
                        "pending_lifecycle_events": [],
                        "lifecycle_notice": None,
                        "lifecycle_event_history": [],
                        "last_error": None,
                        "error_history": [],
                        "recovery_history": [],
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    outbox[key] = record
                    write_outbox(config.state_file, outbox)

                message = record["message"]
                if recovery:
                    message = "<b>[RECOVERY RETRY]</b>\n" + message

                record["status"] = "SENDING"
                record["attempt_count"] = int(record.get("attempt_count", 0)) + 1
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                outbox[key] = record
                write_outbox(config.state_file, outbox)

                try:
                    message_id = send_telegram(
                        config, message, session=session,
                    )
                except Exception as exc:
                    status_code = getattr(getattr(exc, "response", None), "status_code", None)
                    definitive_rejection = (
                        isinstance(exc, RuntimeError)
                        and str(exc).startswith("Telegram rejected")
                    ) or (
                        isinstance(exc, requests.HTTPError)
                        and isinstance(status_code, int)
                        and 400 <= status_code < 500 and status_code not in {408, 425, 429}
                    )
                    record["status"] = "FAILED" if definitive_rejection else "UNKNOWN"
                    record["last_error"] = _redact_error(exc, config)
                    record["updated_at"] = datetime.now(timezone.utc).isoformat()
                    _append_error_history(
                        record, signal_id=key, error=record["last_error"], at=record["updated_at"],
                    )
                    record.setdefault("recovery_history", []).append({
                        "at": record["updated_at"],
                        "from": "SENDING",
                        "to": record["status"],
                        "decision": (
                            "retry as a definitive rejection; no recovery marker"
                            if definitive_rejection
                            else "retry with [RECOVERY RETRY] because delivery is ambiguous"
                        ),
                    })
                    outbox[key] = record
                    write_outbox(config.state_file, outbox)
                    if definitive_rejection:
                        LOG.error(
                            "Telegram definitively rejected signal %s; cursor remains unchanged; "
                            "retry will be visible in logs: %s",
                            key, record["last_error"],
                        )
                    else:
                        LOG.error(
                            "Signal %s delivery outcome is UNKNOWN; cursor remains unchanged; "
                            "next attempt will use [RECOVERY RETRY]: %s",
                            key, record["last_error"],
                        )
                    raise

                if type(message_id) is not int or message_id <= 0:
                    record["status"] = "UNKNOWN"
                    record["last_error"] = (
                        "sendMessage returned success without a valid message_id"
                    )
                    record["updated_at"] = datetime.now(timezone.utc).isoformat()
                    _append_error_history(
                        record, signal_id=key, error=record["last_error"], at=record["updated_at"],
                    )
                    record.setdefault("recovery_history", []).append({
                        "at": record["updated_at"],
                        "from": "SENDING",
                        "to": "UNKNOWN",
                        "decision": "retry with [RECOVERY RETRY]; success response was not auditable",
                    })
                    outbox[key] = record
                    write_outbox(config.state_file, outbox)
                    LOG.error(
                        "Telegram response for signal %s lacked a valid message_id; "
                        "status is UNKNOWN and cursor remains unchanged",
                        key,
                    )
                    raise RuntimeError(
                        "Telegram sendMessage returned success without a valid message_id; "
                        "delivery is UNKNOWN and will be retried with [RECOVERY RETRY]"
                    )

                record["status"] = "SENT"
                record["telegram_message_id"] = message_id
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                outbox[key] = record
                _trim_sent_history(outbox)
                write_outbox(config.state_file, outbox)

                # Commit cursor only after the SENT WAL record is durable.
                write_state(config.state_file, stamp)
                delivered += 1
                LOG.info(
                    "Delivered %s XAU/USD signal for candle %s (Telegram message_id=%s)",
                    data["side"], stamp.isoformat(), message_id,
                )
            else:
                write_state(config.state_file, stamp)
            processed += 1

        stale_notices = _process_stale_notice_groups(config, outbox, session)
        if delivered:
            return "sent" if processed == 1 else "sent_catchup"
        if stale_notices:
            return "stale_notice_sent"
        if processed:
            return "processed_no_signal"
        LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
        return "no_signal"
