"""Regression tests for historical backtest execution accounting."""
from __future__ import annotations

import io
import os
import sys
import traceback
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest_historical import (  # noqa: E402
    evaluate_chronological_holdout, monte_carlo_risk_metrics,
    select_test_window_signals, simulate,
    validate_history_continuity,
    validate_test_window_coverage, fetch_history,
)


def candle(index: int, *, open_: float = 100.0, high: float = 100.0,
           low: float = 100.0, close: float = 100.0) -> dict:
    return {
        "time": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index),
        "open": open_, "high": high, "low": low, "close": close,
    }


def signal(index: int = 0) -> dict:
    return {
        "signal_index": index, "signal_time": candle(index)["time"],
        "side": "BUY", "entry": 100.0, "stop": 90.0,
        "tp1": 110.0, "tp2": 120.0, "risk": 10.0,
    }



class HistoricalRequestSecretRedactionTests(unittest.TestCase):
    def test_http_error_does_not_expose_api_key_or_request_url(self) -> None:
        class Response:
            status_code = 403

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            fetch_history(
                "SENSITIVE_TEST_KEY", "15min",
                date(2026, 1, 1), date(2026, 1, 2), Session(),
            )
        message = str(caught.exception)
        self.assertIn("HTTP 403", message)
        self.assertNotIn("SENSITIVE_TEST_KEY", message)
        self.assertNotIn("apikey", message)
        self.assertNotIn("https://", message)

    def test_network_error_does_not_expose_api_key_or_request_url(self) -> None:
        import requests

        class Session:
            def get(self, *_args, **_kwargs):
                raise requests.Timeout(
                    "timed out for https://api.twelvedata.com/time_series?apikey=SENSITIVE_TEST_KEY"
                )

        with self.assertRaises(RuntimeError) as caught:
            fetch_history(
                "SENSITIVE_TEST_KEY", "15min",
                date(2026, 1, 1), date(2026, 1, 2), Session(),
            )
        message = str(caught.exception)
        self.assertIn("Timeout", message)
        self.assertNotIn("SENSITIVE_TEST_KEY", message)
        self.assertNotIn("apikey", message)
        self.assertNotIn("https://", message)


    def test_invalid_json_does_not_expose_response_text_in_message_or_traceback(self) -> None:
        class Response:
            status_code = 200

            def json(self):
                raise ValueError(
                    "invalid JSON from https://api.twelvedata.com/time_series?apikey=SENSITIVE_TEST_KEY"
                )

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            fetch_history(
                "SENSITIVE_TEST_KEY", "15min",
                date(2026, 1, 1), date(2026, 1, 2), Session(),
            )
        rendered = "".join(traceback.format_exception(
            type(caught.exception), caught.exception, caught.exception.__traceback__
        ))
        for secret in ("SENSITIVE_TEST_KEY", "apikey", "https://api.twelvedata.com"):
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(secret, rendered)
        self.assertIsNone(caught.exception.__cause__)

    def test_provider_error_payload_never_echoes_raw_fields(self) -> None:
        malicious = "https://api.twelvedata.com/time_series?apikey=SENSITIVE_TEST_KEY"
        for payload in (
            {"status": "error", "message": malicious},
            {"code": 401, "message": malicious},
        ):
            with self.subTest(payload_kind=tuple(payload.keys())):
                class Response:
                    status_code = 200

                    def json(self):
                        return payload

                class Session:
                    def get(self, *_args, **_kwargs):
                        return Response()

                with self.assertRaises(RuntimeError) as caught:
                    fetch_history(
                        "SENSITIVE_TEST_KEY", "15min",
                        date(2026, 1, 1), date(2026, 1, 2), Session(),
                    )
                self.assertNotIn("SENSITIVE_TEST_KEY", str(caught.exception))
                self.assertNotIn("apikey", str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))

    def test_missing_or_invalid_values_are_reported_without_payload_text(self) -> None:
        malicious = "https://api.twelvedata.com/time_series?apikey=SENSITIVE_TEST_KEY"
        for payload in (
            {"message": malicious, "values": None},
            {"values": malicious},
            {"values": []},
        ):
            with self.subTest(values_type=type(payload.get("values")).__name__):
                class Response:
                    status_code = 200

                    def json(self):
                        return payload

                class Session:
                    def get(self, *_args, **_kwargs):
                        return Response()

                with self.assertRaises(RuntimeError) as caught:
                    fetch_history(
                        "SENSITIVE_TEST_KEY", "15min",
                        date(2026, 1, 1), date(2026, 1, 2), Session(),
                    )
                self.assertNotIn("SENSITIVE_TEST_KEY", str(caught.exception))
                self.assertNotIn("apikey", str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))

    def test_malformed_candle_does_not_echo_provider_value(self) -> None:
        malicious = "https://api.twelvedata.com/time_series?apikey=SENSITIVE_TEST_KEY"
        payload = {"values": [{
            "datetime": malicious, "open": "100", "high": "101",
            "low": "99", "close": "100",
        }]}

        class Response:
            status_code = 200

            def json(self):
                return payload

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with self.assertRaises(RuntimeError) as caught:
            fetch_history(
                "SENSITIVE_TEST_KEY", "15min",
                date(2026, 1, 1), date(2026, 1, 2), Session(),
            )
        self.assertNotIn("SENSITIVE_TEST_KEY", str(caught.exception))
        self.assertNotIn("apikey", str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))

    def test_session_get_is_called_once_with_expected_request_parameters(self) -> None:
        values = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for index in range(100):
            stamp = start + timedelta(minutes=15 * index)
            values.append({
                "datetime": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "open": "100", "high": "101", "low": "99", "close": "100",
            })

        class Response:
            status_code = 200

            def json(self):
                return {"values": values}

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        result = fetch_history(
            "SENSITIVE_TEST_KEY", "15min",
            date(2026, 1, 1), date(2026, 1, 2), session,
        )
        self.assertEqual(len(result), 100)
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.twelvedata.com/time_series")
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["params"]["symbol"], "XAU/USD")
        self.assertEqual(kwargs["params"]["interval"], "15min")
        self.assertEqual(kwargs["params"]["timezone"], "UTC")
        self.assertEqual(kwargs["params"]["format"], "ISO")
        self.assertEqual(kwargs["params"]["order"], "asc")
        self.assertEqual(kwargs["params"]["start_date"], "2026-01-01T00:00:00")
        self.assertEqual(kwargs["params"]["end_date"], "2026-01-02T00:00:00")
        self.assertEqual(kwargs["params"]["apikey"], "SENSITIVE_TEST_KEY")

    def test_main_error_output_suppresses_raw_exception_message(self) -> None:
        secret_message = (
            "failure at https://api.twelvedata.com/time_series?apikey=SENSITIVE_TEST_KEY"
        )
        stderr = io.StringIO()
        with patch.object(sys, "argv", [
            "backtest_historical.py", "--start", "2026-01-01", "--end", "2026-01-02"
        ]), patch.dict(os.environ, {"GOLD_TWELVE_DATA_API_KEY": "SENSITIVE_TEST_KEY"}), \
             patch("backtest_historical.fetch_history_with_warmup", side_effect=RuntimeError(secret_message)), \
             redirect_stderr(stderr):
            from backtest_historical import main
            result = main()
        output = stderr.getvalue()
        self.assertEqual(result, 1)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("SENSITIVE_TEST_KEY", output)
        self.assertNotIn("apikey", output)
        self.assertNotIn("https://", output)


class HistoricalBacktestExecutionTests(unittest.TestCase):
    def test_entry_and_each_partial_exit_cost_are_charged_once(self) -> None:
        bars = [
            candle(0),
            candle(1, high=111.0, low=99.0),
            candle(2, high=121.0, low=109.0),
        ]
        trades, skipped = simulate(bars, [signal()], cost_per_side=0.20)
        self.assertEqual(skipped, 0)
        self.assertEqual(len(trades), 1)
        # Entry: -0.02R; TP1 half: +0.49R; TP2 half: +0.99R.
        self.assertAlmostEqual(trades[0]["net_r"], 1.46, places=6)
        self.assertTrue(trades[0]["tp1_hit"])
        self.assertEqual(trades[0]["exit_reason"], "TP2")

    def test_favorable_gap_at_target_uses_open_price(self) -> None:
        bars = [candle(0), candle(1, open_=112.0, high=122.0, low=111.0)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.0)
        self.assertEqual(len(trades), 1)
        # TP1 receives the favorable open at 112 (+1.2R); TP2 fills at
        # its 120 level (+2.0R), because the bar opened below TP2.
        self.assertAlmostEqual(trades[0]["net_r"], 1.6, places=6)

    def test_gap_through_stop_uses_worse_open_price(self) -> None:
        bars = [candle(0), candle(1, open_=88.0, high=95.0, low=87.0)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.20)
        self.assertEqual(len(trades), 1)
        # -1.2R price loss, plus one entry and one exit cost of 0.02R each.
        self.assertAlmostEqual(trades[0]["net_r"], -1.24, places=6)
        self.assertEqual(trades[0]["exit_reason"], "SL")

    def test_signal_candle_itself_is_never_used_for_exit(self) -> None:
        bars = [candle(0, high=130.0, low=80.0), candle(1)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.0)
        self.assertEqual(trades, [])

    def test_stop_wins_when_stop_and_target_touch_same_bar(self) -> None:
        bars = [candle(0), candle(1, high=125.0, low=85.0)]
        trades, _ = simulate(bars, [signal()], cost_per_side=0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "SL")
        self.assertAlmostEqual(trades[0]["net_r"], -1.0, places=6)


class ChronologicalHoldoutTests(unittest.TestCase):
    def test_insufficient_history_is_not_reported_as_holdout_result(self) -> None:
        result = evaluate_chronological_holdout(
            [candle(i) for i in range(50)], [], 0.0
        )
        self.assertEqual(result["status"], "insufficient_history")

    def test_development_and_holdout_are_separated_by_time(self) -> None:
        bars = [candle(i) for i in range(100)]
        signals = [
            {**signal(10), "signal_time": bars[10]["time"]},
            {**signal(80), "signal_time": bars[80]["time"]},
        ]
        result = evaluate_chronological_holdout(bars, signals, 0.0)
        self.assertEqual(result["status"], "computed")
        self.assertEqual(result["split_time"], bars[70]["time"].isoformat())
        self.assertEqual(result["development"]["signals_generated"], 1)
        self.assertEqual(result["holdout"]["signals_generated"], 1)
        self.assertIn("not walk-forward optimization", result["warning"])




class MonteCarloRiskTests(unittest.TestCase):
    def test_insufficient_trade_sample_is_not_given_false_precision(self) -> None:
        result = monte_carlo_risk_metrics([0.5, -0.2] * 10)
        self.assertEqual(result["status"], "insufficient_sample")
        self.assertEqual(result["trade_count"], 20)

    def test_bootstrap_is_reproducible_and_reports_risk_percentiles(self) -> None:
        returns = [0.8, -0.4, 0.3, -0.2, 1.1, -0.7] * 6
        first = monte_carlo_risk_metrics(returns, simulations=200, block_size=3, seed=7)
        second = monte_carlo_risk_metrics(returns, simulations=200, block_size=3, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "computed")
        self.assertEqual(first["simulations"], 200)
        self.assertIn("p05", first["net_r_percentiles"])
        self.assertIn("p95", first["max_drawdown_r_percentiles"])
        self.assertGreaterEqual(first["probability_of_net_loss_percent"], 0.0)
        self.assertLessEqual(first["probability_of_net_loss_percent"], 100.0)

    def test_bootstrap_rejects_non_finite_trade_returns(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            monte_carlo_risk_metrics([1.0] * 29 + [float("nan")])




class HistoricalCoverageTests(unittest.TestCase):
    def test_rejects_provider_history_starting_too_late(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        end = datetime(2026, 1, 10, tzinfo=timezone.utc).date()
        bars = [candle(0)]
        bars[0]["time"] = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError, "starts too late"):
            validate_test_window_coverage(bars, start, end, "15min")

    def test_rejects_provider_history_ending_too_early(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        end = datetime(2026, 1, 10, tzinfo=timezone.utc).date()
        bars = [candle(0)]
        bars[0]["time"] = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError, "ends too early"):
            validate_test_window_coverage(bars, start, end, "15min")

    def test_accepts_full_requested_window(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc).date()
        end = datetime(2026, 1, 2, tzinfo=timezone.utc).date()
        bars = [
            {**candle(0), "time": datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)},
            {**candle(1), "time": datetime(2026, 1, 2, 23, 45, tzinfo=timezone.utc)},
        ]
        validate_test_window_coverage(bars, start, end, "15min")



class HistoricalWarmupTests(unittest.TestCase):
    def test_warmup_signals_are_excluded_but_test_window_signals_remain(self) -> None:
        start = datetime(2026, 1, 2, tzinfo=timezone.utc).date()
        signals = [
            {"signal_time": datetime(2026, 1, 1, 23, 45, tzinfo=timezone.utc)},
            {"signal_time": datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)},
            {"signal_time": datetime(2026, 1, 2, 0, 15, tzinfo=timezone.utc)},
        ]
        selected = select_test_window_signals(signals, start)
        self.assertEqual(selected, signals[1:])


class HistoricalDataContinuityTests(unittest.TestCase):
    def test_unexplained_intraday_gap_fails_closed(self) -> None:
        bars = [candle(0), candle(5)]  # 75-minute gap on a 15-minute series
        with self.assertRaisesRegex(RuntimeError, "Unexpected historical data gap"):
            validate_history_continuity(bars, "15min")

    def test_expected_weekend_closure_is_allowed(self) -> None:
        friday_close = datetime(2026, 1, 2, 20, 15, tzinfo=timezone.utc)
        sunday_open = datetime(2026, 1, 4, 22, 0, tzinfo=timezone.utc)
        bars = [
            {"time": friday_close, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"time": sunday_open, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        ]
        validate_history_continuity(bars, "15min")

    def test_daily_timeframe_allows_calendar_weekend_gaps(self) -> None:
        friday = datetime(2026, 1, 2, tzinfo=timezone.utc)
        monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
        bars = [
            {"time": friday, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"time": monday, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        ]
        validate_history_continuity(bars, "1day")

    def test_daily_timeframe_rejects_unexplained_weekday_gap(self) -> None:
        monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
        thursday = datetime(2026, 1, 8, tzinfo=timezone.utc)
        bars = [
            {"time": monday, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"time": thursday, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        ]
        with self.assertRaisesRegex(RuntimeError, "Unexpected historical data gap"):
            validate_history_continuity(bars, "1day")


if __name__ == "__main__":
    unittest.main()
