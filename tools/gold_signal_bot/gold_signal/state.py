"""Durable signal de-duplication state and cross-process locking."""
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

def read_state(path: Path) -> str:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state must be a JSON object")
        return str(state.get("last_signal_candle") or "")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, AttributeError):
        LOG.warning("State file is unreadable; duplicate protection starts fresh")
        return ""

def write_state(path: Path, candle_time: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"last_signal_candle": candle_time.isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass

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
            # Inspect file length instead of reading the locked byte: on Windows,
            # another process holding the byte-range lock may deny that read.
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
