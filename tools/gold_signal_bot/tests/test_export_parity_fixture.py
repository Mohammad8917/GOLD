"""Offline tests for the sanitized Twelve Data parity-fixture exporter."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from export_parity_fixture import build_payload, write_payload  # noqa: E402


class ExportParityFixtureTests(unittest.TestCase):
    def test_payload_contains_only_sanitized_market_data_and_parity_metadata(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [{
            "time": start + timedelta(hours=index),
            "open": 2600.0, "high": 2602.0, "low": 2598.0, "close": 2601.0,
        } for index in range(50)]
        payload = build_payload(
            bars,
            timeframe="1h",
            start=date(2026, 1, 1),
            end=date(2026, 1, 3),
            parity={"signals_checked": 2, "mismatches": 0},
            exported_at="2026-01-03T00:00:00+00:00",
        )
        self.assertEqual(payload["candle_count"], 50)
        self.assertEqual(payload["source"], "Twelve Data")
        self.assertEqual(payload["parity"]["mismatches"], 0)
        serialized = json_dumps(payload)
        for forbidden in ("apikey", "api_key", "bot_token", "chat_id", "secret"):
            self.assertNotIn(forbidden, serialized.lower())

    def test_writer_creates_parent_and_atomic_json_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "fixture.json"
            payload = {"schema_version": 1, "candles": []}
            write_payload(path, payload)
            self.assertTrue(path.is_file())
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            self.assertIn('"schema_version": 1', path.read_text(encoding="utf-8"))


def json_dumps(value):
    import json
    return json.dumps(value)


if __name__ == "__main__":
    unittest.main()
