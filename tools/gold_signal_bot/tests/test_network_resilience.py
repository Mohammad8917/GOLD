"""Unit tests for Telegram HTTP resource cleanup and bounded retry ownership."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import requests

from gold_signal.telegram import TELEGRAM_URL, send_telegram


class Response:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"ok": True, "result": {"message_id": 77}}
        self.error = error
        self.closed = False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


def config():
    return SimpleNamespace(bot_token="test-token", chat_id="test-chat", http_timeout=5)


class TelegramTransportResilienceTests(unittest.TestCase):
    def test_owned_session_and_response_close_after_success(self):
        response = Response()
        session = Session(response)
        from unittest.mock import patch
        with patch("gold_signal.telegram.requests.Session", return_value=session):
            self.assertEqual(send_telegram(config(), "hello"), 77)
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(session.posts[0][0], TELEGRAM_URL.format(token="test-token"))

    def test_caller_owned_session_stays_open_but_response_closes(self):
        response = Response()
        session = Session(response)
        self.assertEqual(send_telegram(config(), "hello", session), 77)
        self.assertTrue(response.closed)
        self.assertFalse(session.closed)

    def test_http_429_is_not_retried_inside_transport_and_resources_close(self):
        response = Response(error=requests.HTTPError(
            "rate limited", response=SimpleNamespace(status_code=429)
        ))
        session = Session(response)
        from unittest.mock import patch
        with patch("gold_signal.telegram.requests.Session", return_value=session):
            with self.assertRaises(requests.HTTPError):
                send_telegram(config(), "hello")
        self.assertEqual(len(session.posts), 1)
        self.assertTrue(response.closed)
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
