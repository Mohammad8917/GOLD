"""Application orchestration with chronological catch-up and a Telegram delivery WAL."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import requests

from .config import Config, TIMEFRAMES
from .market_data import fetch_closed_candles, parse_bar_time
from .strategy import detect_signals
from .strategy_version import STRATEGY_VERSION
from .state import (
    process_lock, read_outbox, read_state, write_outbox, write_state,
)
from .telegram import format_expiry, format_signal, send_telegram

LOG = logging.getLogger("gold_signal_bot")

INDICATOR_WARMUP_BARS = 200


def _signal_id(signal: dict, timeframe: str) -> str:
    stamp = signal["candle_time"].isoformat()
    identity = f'{signal.get("symbol", "XAU/USD")}|{timeframe}|{stamp}|{STRATEGY_VERSION}'
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _redact_error(error: Exception, config: Config) -> str:
    value = str(error)
    for secret in (config.api_key, config.bot_token):
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
            LOG.error("Startup recovery: signal %s was left SENDING; retrying visibly", key)

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

        outbox[key] = record
    if changed:
        write_outbox(state_path, outbox)


def _trim_sent_history(outbox: dict[str, dict], limit: int = 1000) -> None:
    """Bound the audit journal while never deleting an unfinished delivery."""
    sent = sorted(
        (
            (record.get("updated_at", ""), key)
            for key, record in outbox.items()
            if record.get("status") == "SENT" and record.get("lifecycle_status") == "EXPIRED"
        )
    )
    for _, key in sent[:-limit]:
        outbox.pop(key, None)


def _process_due_expirations(
    config: Config, candles: list[dict], outbox: dict[str, dict],
    current_index: int, session: requests.Session | None,
) -> int:
    """Send durable expiry notices when a signal reaches its TTL candle."""
    index_by_time = {bar["time"]: index for index, bar in enumerate(candles)}
    sent_count = 0
    interval = TIMEFRAMES[config.timeframe][1]

    for key, record in list(outbox.items()):
        if record.get("status") != "SENT" or record.get("lifecycle_status") == "EXPIRED":
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

        expiry_index = signal_index + config.signal_ttl_candles
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
                config.signal_ttl_candles,
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
                and isinstance(status_code, int) and 400 <= status_code < 500
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


def run_once(config: Config, session: requests.Session | None = None) -> str:
    """Process new closed candles; WAL recovery prefers a visible duplicate to a lost signal.

    A SENDING record found after restart is ambiguous: Telegram may have accepted
    the POST before the process died. It is changed to UNKNOWN and retried with
    an explicit [RECOVERY RETRY] marker. Exactly-once delivery is not claimed.
    """
    with process_lock(config.state_file):
        candles = fetch_closed_candles(config, session=session)
        if len(candles) < 60:
            raise RuntimeError("Insufficient candle history for signal evaluation")

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
            expiry_delivered = _process_due_expirations(
                config, candles, outbox, len(candles) - 1, session,
            )
            if expiry_delivered:
                return "expired"
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
            delivered += _process_due_expirations(
                config, candles, outbox, index, session,
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
                        "entry": float(data["entry"]),
                        "stop_loss": float(data["stop_loss"]),
                        "take_profit_1": float(data["take_profit_1"]),
                        "take_profit_2": float(data["take_profit_2"]),
                        "lifecycle_status": "ACTIVE",
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
                        and 400 <= status_code < 500
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

        if delivered:
            return "sent" if processed == 1 else "sent_catchup"
        if processed:
            return "processed_no_signal"
        LOG.info("No new confirmed GOLD signal on %s", config.timeframe)
        return "no_signal"
