"""Critical hardening tests for candle scheduling and stale signal suppression."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from gold_signal.config import seconds_until_next_candle_close
from gold_signal.service import _signal_is_fresh


class CriticalHardeningTests(unittest.TestCase):
    def test_15m_schedule_targets_four_seconds_after_next_boundary(self):
        now = datetime(2026, 11, 12, 10, 14, 58, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            seconds_until_next_candle_close("15min", now=now, offset_seconds=4),
            6.0,
        )

    def test_schedule_rejects_naive_clock(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            seconds_until_next_candle_close(
                "15min", now=datetime(2026, 11, 12, 10, 14, 58)
            )

    def test_recent_closed_candle_can_emit_signal(self):
        stamp = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
        now = stamp + timedelta(minutes=15, seconds=30)
        self.assertTrue(_signal_is_fresh(stamp, "15min", now=now))

    def test_six_hour_old_candle_is_never_fresh(self):
        stamp = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
        now = datetime(2026, 11, 12, 10, 15, tzinfo=timezone.utc)
        self.assertFalse(_signal_is_fresh(stamp, "15min", now=now))


if __name__ == "__main__":
    unittest.main()
