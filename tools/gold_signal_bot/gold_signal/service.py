"""Application orchestration with chronological catch-up and a Telegram delivery WAL."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import requests

from .config import Config
from .market_data import fetch_closed_candles, parse_bar_time
from .strategy import detect_signals
from .state import (
    process_lock, read_outbox, read_state, write_outbox, write_state,
)
from .telegram import format_signal, send_telegram

LOG = logging.getLogger("gold_signal_bot")

INDICATOR_WARMUP_BARS = 200
STRATEGY_VERSION = "ema20-ema50-rsi14-atr14-v1"


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
            # A crash after cursor fsync but before WAL cleanup leaves harmless
            # SENT records behind. Remove records already covered by the cursor.
            stale_ids = [
                key for key, record in outbox.items()
                if record.get("signal_timestamp")
                and parse_bar_time(record["signal_timestamp"]) <= cursor
            ]
            if stale_ids:
                for key in stale_ids:
                    outbox.pop(key, None)
                write_outbox(config.state_file, outbox)

        if start_index >= len(candles):
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
            data = signals.get(stamp)
            if data:
                key = _signal_id(data, config.timeframe)
                record = outbox.get(key)

                if record and record.get("status") == "SENT":
                    # Telegram success was durably recorded before cursor commit.
                    write_state(config.state_file, stamp)
                    outbox.pop(key, None)
                    write_outbox(config.state_file, outbox)
                    processed += 1
                    continue

                recovery = bool(record and record.get("status") in {"SENDING", "UNKNOWN"})
                if record and record.get("status") == "SENDING":
                    record["status"] = "UNKNOWN"
                    record["updated_at"] = datetime.now(timezone.utc).isoformat()
                    outbox[key] = record
                    write_outbox(config.state_file, outbox)
                    LOG.error(
                        "Signal %s was left SENDING after restart; status is UNKNOWN; "
                        "decision: retry with [RECOVERY RETRY]",
                        key,
                    )

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
                        "last_error": None,
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
                record["last_error"] = None
                outbox[key] = record
                write_outbox(config.state_file, outbox)

                try:
                    message_id = send_telegram(
                        config, message, session=session,
                    )
                except Exception as exc:
                    record["status"] = "UNKNOWN"
                    record["last_error"] = _redact_error(exc, config)
                    record["updated_at"] = datetime.now(timezone.utc).isoformat()
                    outbox[key] = record
                    write_outbox(config.state_file, outbox)
                    LOG.error(
                        "Signal %s delivery outcome is UNKNOWN; cursor remains unchanged; "
                        "next attempt will use [RECOVERY RETRY]: %s",
                        key, record["last_error"],
                    )
                    raise

                record["status"] = "SENT"
                record["telegram_message_id"] = message_id
                record["updated_at"] = datetime.now(timezone.utc).isoformat()
                outbox[key] = record
                write_outbox(config.state_file, outbox)

                # Commit cursor only after the SENT WAL record is durable.
                write_state(config.state_file, stamp)
                outbox.pop(key, None)
                write_outbox(config.state_file, outbox)
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
