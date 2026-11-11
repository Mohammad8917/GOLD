"""Consistent Telegram delivery mocks for offline tests."""
from __future__ import annotations

from unittest.mock import Mock

import requests


def mock_send_success(message_id: int = 12345) -> Mock:
    """Simulate Telegram acceptance with an auditable positive message_id."""
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("message_id must be a positive integer")
    return Mock(return_value=message_id)


def mock_send_success_no_id() -> Mock:
    """Simulate a nominal success response lacking a usable message_id."""
    return Mock(return_value=None)


def mock_send_fail(error: Exception | None = None) -> Mock:
    """Simulate a definitive Telegram rejection unless an error is supplied."""
    return Mock(side_effect=error or RuntimeError("Telegram rejected test message"))


def mock_send_timeout() -> Mock:
    """Simulate an ambiguous network timeout."""
    return Mock(side_effect=requests.Timeout("simulated Telegram timeout"))


class SimulatedTelegramProcessCrash(BaseException):
    """Abrupt process death after Telegram has accepted the message."""


def mock_send_crash_after_accept() -> tuple[Mock, list[str]]:
    """Simulate remote acceptance followed by abrupt process termination."""
    accepted: list[str] = []

    def accepted_then_crash(*_args, **_kwargs):
        accepted.append("accepted")
        raise SimulatedTelegramProcessCrash("process terminated after remote acceptance")

    return Mock(side_effect=accepted_then_crash), accepted
