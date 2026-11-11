"""Durable signal-processing cursor, delivery WAL, and cross-process locking."""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

LOG = logging.getLogger("gold_signal_bot")


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


def read_outbox(state_path: Path) -> dict[str, dict]:
    """Read the delivery WAL; corruption must never be interpreted as empty."""
    path = outbox_path(state_path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(not isinstance(v, dict) for v in value.values()):
            raise ValueError("outbox must be a JSON object of records")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError("Delivery WAL is unreadable; refusing to risk duplicate or lost signals") from exc


def write_outbox(state_path: Path, records: dict[str, dict]) -> None:
    """Atomically persist all WAL records before/after Telegram operations."""
    _atomic_json_write(outbox_path(state_path), records)


_LOCAL_PROCESS_GUARD = threading.Lock()


@contextmanager
def process_lock(state_path: Path) -> Iterator[None]:
    """Cross-platform advisory lock plus in-process guard for duplicate bot instances."""
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
