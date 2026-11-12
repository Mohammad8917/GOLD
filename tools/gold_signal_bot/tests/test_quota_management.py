"""Offline tests for Twelve Data quota classification and reset handling."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from gold_signal.quota import API_USAGE_URL, QuotaLimitError, quota_error
from gold_signal.market_data import TWELVE_DATA_URL, _get_with_retry
from gold_signal.quota_warning import maybe_send_daily_quota_warning


class QuotaManagementTests(unittest.TestCase):
    def make_usage_response(self, payload):
        response = Mock()
        response.status_code = 200
        response.json.return_value = payload
        return response

    def test_daily_limit_uses_api_usage_and_pauses_until_utc_midnight(self):
        session = Mock()
        session.get.return_value = self.make_usage_response({
            "timestamp": "2026-11-11T23:59:10Z",
            "current_usage": 1,
            "plan_limit": 8,
            "daily_usage": 800,
            "plan_daily_limit": 800,
            "plan_category": "Basic",
        })
        error = quota_error(
            session, api_key="test-key", timeout=5,
            message="API credits exhausted",
        )
        self.assertIsInstance(error, QuotaLimitError)
        self.assertEqual(error.window, "daily")
        self.assertGreaterEqual(error.retry_after, 1)
        self.assertEqual(error.usage["daily_usage"], 800)
        self.assertEqual(session.get.call_args.kwargs["params"]["apikey"], "test-key")
        self.assertEqual(session.get.call_args.args[0], API_USAGE_URL)
        self.assertEqual(session.get.call_count, 1, "usage endpoint must be queried only once per 429")

    def test_minute_limit_is_not_misclassified_as_daily(self):
        session = Mock()
        session.get.return_value = self.make_usage_response({
            "timestamp": "2026-11-11T12:34:20Z",
            "current_usage": 8,
            "plan_limit": 8,
            "daily_usage": 120,
            "plan_daily_limit": 800,
            "plan_category": "Basic",
        })
        error = quota_error(session, api_key="test-key", timeout=5)
        self.assertEqual(error.window, "minute")
        self.assertGreaterEqual(error.retry_after, 1)

    def test_minute_retry_uses_local_current_clock_not_stale_usage_timestamp(self):
        session = Mock()
        session.get.return_value = self.make_usage_response({
            "timestamp": "2020-01-01T00:00:00Z",
            "current_usage": 8, "plan_limit": 8,
            "daily_usage": 120, "plan_daily_limit": 800,
        })
        with patch("gold_signal.quota._parse_utc", side_effect=AssertionError("stale provider timestamp must not be used")):
            error = quota_error(session, api_key="test-key", timeout=5)
        self.assertEqual(error.window, "minute")
        self.assertGreaterEqual(error.retry_after, 1)

    def test_daily_error_message_is_fallback_when_usage_endpoint_unavailable(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("usage endpoint unavailable")
        error = quota_error(
            session, api_key="test-key", timeout=5,
            message="You have run out of API credits for the current day",
        )
        self.assertEqual(error.window, "daily")

    def test_unknown_429_defaults_to_minute_window_and_honors_retry_after(self):
        session = Mock()
        session.get.side_effect = requests.ConnectionError("usage endpoint unavailable")
        error = quota_error(
            session, api_key="test-key", timeout=5, retry_after="12",
        )
        self.assertEqual(error.window, "minute")
        self.assertEqual(error.retry_after, 12.0)

    def test_daily_usage_near_95_percent_threshold_is_classified_as_daily(self):
        session = Mock()
        session.get.return_value = self.make_usage_response({
            "timestamp": "2026-11-11T12:34:20Z",
            "current_usage": 1, "plan_limit": 8,
            "daily_usage": 760, "plan_daily_limit": 800,
        })
        error = quota_error(session, api_key="test-key", timeout=5)
        self.assertEqual(error.window, "daily")

    def test_daily_warning_at_80_percent_is_sent_only_once_per_utc_day(self):
        with TemporaryDirectory() as folder:
            config = SimpleNamespace(
                state_file=Path(folder) / "state.json",
                api_key="test-key",
                http_timeout=5,
            )
            session = Mock()
            usage = {"daily_usage": 640, "plan_daily_limit": 800}
            with patch("gold_signal.quota_warning.inspect_api_usage", return_value=usage) as inspect, \
                 patch("gold_signal.quota_warning.send_telegram", return_value=123) as send:
                self.assertTrue(maybe_send_daily_quota_warning(config, session))
                self.assertFalse(maybe_send_daily_quota_warning(config, session))
            inspect.assert_called_once()
            send.assert_called_once()
            sent_text = send.call_args.args[1]
            self.assertIn("640", sent_text)
            self.assertIn("80.0", sent_text)
            self.assertIn("00:00 UTC", sent_text)

    def test_daily_warning_skips_non_market_data_config_without_api_key(self):
        with TemporaryDirectory() as folder:
            config = SimpleNamespace(state_file=Path(folder) / "state.json")
            with (
                patch("gold_signal.quota_warning.inspect_api_usage") as inspect,
                patch("gold_signal.quota_warning.send_telegram") as send,
            ):
                self.assertFalse(maybe_send_daily_quota_warning(config, Mock()))
            inspect.assert_not_called()
            send.assert_not_called()

    def test_failed_daily_usage_check_is_not_repeated_each_poll_cycle(self):
        with TemporaryDirectory() as folder:
            config = SimpleNamespace(
                state_file=Path(folder) / "state.json",
                api_key="test-key",
                http_timeout=5,
            )
            with patch(
                "gold_signal.quota_warning.inspect_api_usage",
                side_effect=requests.ConnectionError("offline"),
            ) as inspect:
                with self.assertRaises(requests.ConnectionError):
                    maybe_send_daily_quota_warning(config, Mock())
                self.assertFalse(maybe_send_daily_quota_warning(config, Mock()))
            inspect.assert_called_once()

    def test_daily_warning_is_not_sent_below_80_percent(self):
        with TemporaryDirectory() as folder:
            config = SimpleNamespace(
                state_file=Path(folder) / "state.json",
                api_key="test-key",
                http_timeout=5,
            )
            with patch("gold_signal.quota_warning.inspect_api_usage", return_value={
                "daily_usage": 639, "plan_daily_limit": 800,
            }) as inspect, patch("gold_signal.quota_warning.send_telegram") as send:
                self.assertFalse(maybe_send_daily_quota_warning(config, Mock()))
                self.assertFalse(maybe_send_daily_quota_warning(config, Mock()))
            inspect.assert_called_once()
            send.assert_not_called()

    def test_usage_endpoint_401_and_403_have_distinct_actionable_messages(self):
        for status, expected in (
            (401, "API key is invalid or missing"),
            (403, "access is forbidden"),
        ):
            with self.subTest(status=status):
                session = Mock()
                response = Mock()
                response.status_code = status
                session.get.return_value = response
                with self.assertRaisesRegex(RuntimeError, expected):
                    quota_error(session, api_key="test-key", timeout=5)

    def test_usage_endpoint_credential_rejection_is_not_swallowed(self):
        session = Mock()
        response = Mock()
        response.status_code = 401
        session.get.return_value = response
        with self.assertRaisesRegex(RuntimeError, "API key is invalid or missing"):
            quota_error(session, api_key="test-key", timeout=5)

    def test_time_series_http_401_and_403_have_distinct_actionable_messages(self):
        for status, expected in (
            (401, "API key is invalid or missing"),
            (403, "access is forbidden"),
        ):
            with self.subTest(status=status):
                session = Mock()
                response = Mock()
                response.status_code = status
                session.get.return_value = response
                with self.assertRaisesRegex(RuntimeError, expected):
                    _get_with_retry(
                        session, TWELVE_DATA_URL, params={"apikey": "test-key"},
                        timeout=5, attempts=1, base_delay=0.1,
                    )
                session.get.assert_called_once()

    def test_time_series_http_429_uses_api_usage_and_raises_daily_pause(self):
        session = Mock()
        limited = Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "1"}
        limited.json.return_value = {"code": 429, "message": "API credits exhausted"}
        usage = self.make_usage_response({
            "timestamp": "2026-11-11T23:59:10Z",
            "current_usage": 1, "plan_limit": 8,
            "daily_usage": 800, "plan_daily_limit": 800,
        })
        session.get.side_effect = [limited, usage]
        with self.assertRaises(QuotaLimitError) as raised:
            _get_with_retry(
                session, TWELVE_DATA_URL,
                params={"apikey": "test-key"}, timeout=5, attempts=3, base_delay=0.1,
            )
        self.assertEqual(raised.exception.window, "daily")
        self.assertEqual(session.get.call_count, 2)

if __name__ == "__main__":
    unittest.main()
