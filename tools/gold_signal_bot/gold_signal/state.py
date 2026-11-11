"""Durable signal-processing cursor, versioned delivery WAL, and process locking."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

LOG = logging.getLogger("gold_signal_bot")
WAL_VERSION = 1
_SIGNAL_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_VALID_STATUSES = {"PENDING", "SENDING", "UNKNOWN", "FAILED", "SENT"}


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_state(path: Path) -> str:
    """Return the last fully processed candle timestamp.

    Older installations stored only last_signal_candle. Treat it as the cursor
    during migration so the bot can resume after the last known send.
    """
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state must be a JSON object")
        return str(state.get("last_processed_candle") or state.get("last_signal_candle") or "")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError("State file is unreadable; refusing to reset the processing cursor") from exc


def write_state(path: Path, candle_time: datetime) -> None:
    """Atomically persist the processed-through watermark."""
    _atomic_json_write(path, {"last_processed_candle": candle_time.isoformat()})


def outbox_path(state_path: Path) -> Path:
    """Return the sidecar path used for the write-ahead delivery log."""
    return state_path.with_name(state_path.name + ".outbox.json")


def _validate_records(records: object) -> dict[str, dict]:
    if not isinstance(records, dict):
        raise ValueError("WAL records must be a JSON object")
    validated: dict[str, dict] = {}
    for key, record in records.items():
        if not isinstance(key, str) or not _SIGNAL_ID_RE.fullmatch(key):
            raise ValueError(f"invalid WAL signal ID: {key!r}")
        if not isinstance(record, dict):
            raise ValueError(f"WAL record {key} must be a JSON object")
        status = record.get("status")
        if status not in _VALID_STATUSES:
            raise ValueError(f"WAL record {key} has invalid status: {status!r}")
        if status == "SENT":
            message_id = record.get("telegram_message_id")
            if type(message_id) is not int or message_id <= 0:
                raise ValueError(f"WAL SENT record {key} lacks a valid Telegram message_id")
        validated[key] = record
    return validated


def read_outbox(state_path: Path) -> dict[str, dict]:
    """Read and validate a versioned WAL; migrate the prior unversioned format.

    Migration policy: legacy root-level signal records are accepted as schema
    version 0 and returned in memory as version 1 records. The next WAL write
    persists the version-1 envelope. Legacy SENT entries without an auditable
    Telegram message_id are downgraded to UNKNOWN so they cannot violate the
    SENT invariant; the retry remains visible through the normal recovery path.
    """
    path = outbox_path(state_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("WAL root must be a JSON object")

        if "wal_version" not in value and "records" not in value:
            records = _validate_records(value)
            now = datetime.now(timezone.utc).isoformat()
            for key, record in records.items():
                if record.get("status") == "SENT":
                    message_id = record.get("telegram_message_id")
                    if type(message_id) is not int or message_id <= 0:
                        record["status"] = "UNKNOWN"
                        record["last_error"] = (
                            "Migrated legacy SENT record had no valid Telegram message_id; "
                            "delivery outcome is treated as unknown"
                        )
                        record["updated_at"] = now
                        record.setdefault("recovery_history", []).append({
                            "at": now,
                            "from": "SENT",
                            "to": "UNKNOWN",
                            "decision": "legacy WAL migration; retry with [RECOVERY RETRY]",
                        })
            LOG.warning("Migrating legacy unversioned delivery WAL to schema version %d", WAL_VERSION)
            return records

        version = value.get("wal_version")
        if type(version) is not int:
            raise ValueError("WAL wal_version must be an integer")
        if version != WAL_VERSION:
            raise ValueError(f"Unsupported WAL version {version}; supported version is {WAL_VERSION}")
        if set(value) != {"wal_version", "records"}:
            raise ValueError("WAL version-1 envelope contains unexpected or missing fields")
        return _validate_records(value["records"])
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError(
            "Delivery WAL is unreadable or uses an unsupported schema; refusing to risk duplicate or lost signals"
        ) from exc


def write_outbox(state_path: Path, records: dict[str, dict]) -> None:
    """Atomically persist all WAL records in the current versioned envelope."""
    validated = _validate_records(records)
    _atomic_json_write(
        outbox_path(state_path),
        {"wal_version": WAL_VERSION, "records": validated},
    )


_LOCAL_PROCESS_GUARD = threading.Lock()


@contextmanager
def process_lock(state_path: Path) -> Iterator[None]:
    """Acquire a non-blocking OS advisory lock for this state path.

    The lock is kernel-owned (flock on POSIX, msvcrt byte-range lock on Windows),
    not a PID-based stale lock. The .lock file may remain after exit; that is
    harmless because the OS releases the lock automatically when a process
    closes its handle or terminates. A crashed process therefore cannot leave
    a permanently-held stale lock file. Contenders fail fast with RuntimeError.
    """
    if not _LOCAL_PROCESS_GUARD.acquire(blocking=False):
        raise RuntimeError("Another GOLD notifier instance is already running")
    lock_path = state_path.with_name(state_path.name + ".lock")
    handle = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another GOLD notifier instance is already running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another GOLD notifier instance is already running") from exc
        acquired = True
        yield
    finally:
        if handle is not None:
            if acquired:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
        _LOCAL_PROCESS_GUARD.release()
