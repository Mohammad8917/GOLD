"""Security regression tests for live Twelve Data transport errors."""
from __future__ import annotations

import sys
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gold_signal.market_data import (  # noqa: E402
    TWELVE_DATA_URL, _get_with_retry, fetch_closed_candles,
)

SECRET = "SENSITIVE_TEST_KEY"
MALICIOUS_URL = f"{TWELVE_DATA_URL}?symbol=XAU%2FUSD&apikey={SECRET}"


def config():
    return SimpleNamespace(
        api_key=SECRET, timeframe="15min", http_timeout=5,
        retry_attempts=1, retry_base_seconds=0.01,
        stale_multiplier=3.0, gap_multiplier=1.5,
    )


def assert_redacted(test: unittest.TestCase, error: BaseException) -> None:
    rendered = "".join(traceback.format_exception(
        type(error), error, error.__traceback__
    ))
    for value in (SECRET, "apikey", "https://api.twelvedata.com"):
        test.assertNotIn(value, str(error))
        test.assertNotIn(value, rendered)
    test.assertIsNone(error.__cause__)
    test.assertIsNone(error.__context__)


class LiveMarketDataErrorRedactionTests(unittest.TestCase):
    def test_http_error_does_not_call_raise_for_status_or_leak_url(self) -> None:
        class Response:
            status_code = 403
            headers = {}

            def raise_for_status(self):
                raise AssertionError("unsafe raise_for_status must not be called")

        class Session:
            def get(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return Response()

        session = Session()
        with self.assertRaises(RuntimeError) as caught:
            _get_with_retry(
                session, TWELVE_DATA_URL,
                params={"symbol": "XAU/USD", "apikey": SECRET},
                timeout=5, attempts=1, base_delay=0.01,
            )
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertEqual(session.url, TWELVE_DATA_URL)
        self.assertEqual(session.kwargs["params"]["apikey"], SECRET)
        assert_redacted(self, caught.exception)

    def test_final_server_error_is_generic_and_redacted(self) -> None:
        class Response:
            status_code = 503
            headers = {}

            def raise_for_status(self):
                raise AssertionError("unsafe raise_for_status must not be called")

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            _get_with_retry(
                Session(), TWELVE_DATA_URL,
                params={"apikey": SECRET}, timeout=5,
                attempts=1, base_delay=0.01,
            )
        self.assertIn("HTTP 503", str(caught.exception))
        assert_redacted(self, caught.exception)

    def test_network_error_does_not_leak_requests_exception_url(self) -> None:
        import requests

        class Session:
            def get(self, *_args, **_kwargs):
                raise requests.Timeout(f"timeout for {MALICIOUS_URL}")

        with self.assertRaises(RuntimeError) as caught:
            _get_with_retry(
                Session(), TWELVE_DATA_URL,
                params={"apikey": SECRET}, timeout=5,
                attempts=1, base_delay=0.01,
            )
        self.assertIn("Timeout", str(caught.exception))
        assert_redacted(self, caught.exception)

    def test_provider_error_payload_message_is_never_echoed(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                return {"status": "error", "message": MALICIOUS_URL}

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            fetch_closed_candles(config(), Session())
        self.assertIn("error response", str(caught.exception))
        assert_redacted(self, caught.exception)

    def test_provider_error_code_is_safe_but_message_is_suppressed(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                return {"status": "error", "code": "400", "message": MALICIOUS_URL}

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            fetch_closed_candles(config(), Session())
        self.assertIn("code 400", str(caught.exception))
        assert_redacted(self, caught.exception)

    def test_invalid_json_error_is_sanitized_without_exception_context(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                raise ValueError(f"invalid response at {MALICIOUS_URL}")

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            fetch_closed_candles(config(), Session())
        self.assertIn("invalid JSON", str(caught.exception))
        assert_redacted(self, caught.exception)

    def test_fetch_session_get_parameters_match_expected_live_request(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                return {"status": "error", "code": "400", "message": "bad request"}

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        with self.assertRaises(RuntimeError):
            fetch_closed_candles(config(), session)
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, TWELVE_DATA_URL)
        self.assertEqual(kwargs["timeout"], 5)
        self.assertEqual(kwargs["params"]["symbol"], "XAU/USD")
        self.assertEqual(kwargs["params"]["interval"], "15min")
        self.assertEqual(kwargs["params"]["outputsize"], 5000)
        self.assertEqual(kwargs["params"]["timezone"], "UTC")
        self.assertEqual(kwargs["params"]["format"], "ISO")
        self.assertEqual(kwargs["params"]["apikey"], SECRET)


if __name__ == "__main__":
    unittest.main()
