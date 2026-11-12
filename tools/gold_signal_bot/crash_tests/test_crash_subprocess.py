"""Real subprocess termination tests for WAL crash boundaries.

Run separately from the fast offline suite:
    python crash_tests/test_crash_subprocess.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BOT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_ROOT))

from gold_signal.state import read_outbox  # noqa: E402
from tests.test_delivery_wal import make_signal, signal_key  # noqa: E402

WORKER = r"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gold_signal import service
from gold_signal.service import run_once

mode, state_name, marker_name, message_name = sys.argv[1:5]
state_path = Path(state_name)
marker_path = Path(marker_name)
message_path = Path(message_name)

start = datetime(2026, 11, 10, tzinfo=timezone.utc)
candles = [
    {
        "time": start + timedelta(minutes=15 * i),
        "open": 2600.0,
        "high": 2602.0,
        "low": 2598.0,
        "close": 2601.0,
    }
    for i in range(202)
]
stamp = candles[-1]["time"]
signal = {
    "symbol": "XAU/USD",
    "side": "BUY",
    "entry": 2600.0,
    "stop_loss": 2590.0,
    "take_profit_1": 2610.0,
    "take_profit_2": 2620.0,
    "atr": 6.0,
    "rsi": 60.0,
    "ema_fast": 2598.0,
    "ema_slow": 2590.0,
    "candle_time": stamp,
}
config = SimpleNamespace(
    state_file=state_path,
    timeframe="15min",
    atr_multiplier=1.5,
    tp1_r=1.0,
    tp2_r=2.0,
    api_key="test-api-key",
    bot_token="test-bot-token",
    chat_id="test-chat",
    http_timeout=5,
)

def hold_at_boundary(label):
    marker_path.write_text(label, encoding="utf-8")
    while True:
        time.sleep(1)

with patch("gold_signal.service.fetch_closed_candles", return_value=candles), \
     patch("gold_signal.service.detect_signals", return_value=[signal]), \
     patch("gold_signal.service.format_signal", return_value="subprocess crash test signal"):
    if mode in ("before_sending", "after_sending"):
        real_write = service.write_outbox

        def intercepted_write(path, records):
            statuses = [record.get("status") for record in records.values()]
            if mode == "before_sending" and "PENDING" in statuses and "SENDING" not in statuses:
                real_write(path, records)
                hold_at_boundary("pending-durable")
            real_write(path, records)
            if mode == "after_sending" and "SENDING" in statuses:
                hold_at_boundary("sending-durable")

        service.write_outbox = intercepted_write
        run_once(config)
    elif mode == "after_accept":
        def accept_then_hold(_config, _message, session=None):
            marker_path.write_text("telegram-accepted", encoding="utf-8")
            while True:
                time.sleep(1)
        service.send_telegram = accept_then_hold
        run_once(config)
    elif mode == "recover":
        def recover_send(_config, message, session=None):
            message_path.write_text(message, encoding="utf-8")
            return 777
        service.send_telegram = recover_send
        run_once(config)
    else:
        raise SystemExit("unknown mode: " + mode)
"""


class SubprocessCrashBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.marker_path = self.root / "boundary.marker"
        self.message_path = self.root / "delivered-message.txt"
        self.signal = make_signal(
            datetime(2026, 11, 10, tzinfo=timezone.utc) + timedelta(minutes=15 * 201)
        )

    def tearDown(self):
        self.temp.cleanup()

    def _start_worker(self, mode: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable, "-c", WORKER, mode,
                str(self.state_path), str(self.marker_path), str(self.message_path),
            ],
            cwd=str(BOT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _terminate_at_marker(self, mode: str, expected_marker: str) -> None:
        process = self._start_worker(mode)
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline and not self.marker_path.exists():
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail(f"worker exited before crash boundary: {stdout}\n{stderr}")
                time.sleep(0.05)
            self.assertTrue(self.marker_path.exists(), f"worker did not reach {mode} boundary")
            self.assertEqual(self.marker_path.read_text(encoding="utf-8"), expected_marker)
        finally:
            if process.poll() is None:
                # Popen.terminate maps to TerminateProcess on Windows and SIGTERM on POSIX.
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.fail("subprocess did not terminate after Popen.terminate()")
        self.assertNotEqual(process.returncode, 0, "terminated worker unexpectedly exited normally")

    def _recover(self) -> str:
        completed = subprocess.run(
            [
                sys.executable, "-c", WORKER, "recover",
                str(self.state_path), str(self.marker_path), str(self.message_path),
            ],
            cwd=str(BOT_ROOT),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        return self.message_path.read_text(encoding="utf-8")

    def _assert_sent_and_cursor_committed(self) -> None:
        key = signal_key(self.signal)
        records = read_outbox(self.state_path)
        self.assertEqual(records[key]["status"], "SENT")
        self.assertEqual(records[key]["telegram_message_id"], 777)
        self.assertTrue(self.state_path.exists())

    def test_termination_before_sending_persisted_recovers_pending_normally(self):
        self._terminate_at_marker("before_sending", "pending-durable")
        key = signal_key(self.signal)
        self.assertEqual(read_outbox(self.state_path)[key]["status"], "PENDING")
        message = self._recover()
        self.assertNotIn("[RECOVERY RETRY]", message)
        self._assert_sent_and_cursor_committed()

    def test_termination_after_sending_persisted_recovers_unknown_with_marker(self):
        self._terminate_at_marker("after_sending", "sending-durable")
        key = signal_key(self.signal)
        self.assertEqual(read_outbox(self.state_path)[key]["status"], "SENDING")
        message = self._recover()
        self.assertIn("[RECOVERY RETRY]", message)
        self._assert_sent_and_cursor_committed()

    def test_termination_after_telegram_acceptance_recovers_unknown_with_marker(self):
        self._terminate_at_marker("after_accept", "telegram-accepted")
        key = signal_key(self.signal)
        self.assertEqual(read_outbox(self.state_path)[key]["status"], "SENDING")
        message = self._recover()
        self.assertIn("[RECOVERY RETRY]", message)
        self._assert_sent_and_cursor_committed()


if __name__ == "__main__":
    unittest.main(verbosity=2)
