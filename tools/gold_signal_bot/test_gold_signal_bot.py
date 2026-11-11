"""Offline tests; no network calls, broker connections, or credentials required."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import multiprocessing
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from gold_signal_bot import (
    Config, _get_with_retry, _is_expected_gold_pause, atr, detect_signal, ema,
    fetch_closed_candles, format_signal, parse_bar_time, process_lock, read_state,
    rsi, run_once, send_telegram, validate_candles, write_state,
)


def _child_lock_attempt(state_path, connection):
    try:
        with process_lock(Path(state_path)):
            connection.send("acquired")
    except RuntimeError:
        connection.send("blocked")
    finally:
        connection.close()


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_length(self):
        result = ema([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertEqual(len(result), 5)
        self.assertIsNone(result[0])
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[3], 3.0)
        self.assertAlmostEqual(result[4], 4.0)

    def test_rsi_matches_known_reference_values_and_stays_bounded(self):
        result = rsi([1.0, 2.0, 1.0, 2.0], 2)
        self.assertAlmostEqual(result[2], 50.0)
        self.assertAlmostEqual(result[3], 75.0)
        self.assertTrue(all(0.0 <= value <= 100.0 for value in result if value is not None))

    def test_atr_matches_known_reference_value(self):
        bars = [
            {"open": 10.0, "high": 12.0, "low": 9.0, "close": 10.0},
            {"open": 10.0, "high": 13.0, "low": 10.0, "close": 12.0},
            {"open": 12.0, "high": 14.0, "low": 11.0, "close": 13.0},
        ]
        self.assertAlmostEqual(atr(bars, 2)[2], 3.0)

    def test_naive_time_is_treated_as_utc(self):
        self.assertEqual(parse_bar_time("2026-11-10 10:00:00").tzinfo, timezone.utc)
        self.assertEqual(
            parse_bar_time("2026-11-10T10:00:00+02:00"),
            datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc),
        )

    def test_insufficient_history_never_creates_signal(self):
        bars = [{"time": datetime.now(timezone.utc), "open": 1, "high": 2,
                 "low": 0.5, "close": 1} for _ in range(20)]
        self.assertIsNone(detect_signal(bars))


class CandleValidationTests(unittest.TestCase):
    interval = 900

    def make_bars(self, count=65, *, now=None):
        now = now or datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
        last_open = now - timedelta(seconds=self.interval + 5)
        first_open = last_open - timedelta(seconds=self.interval * (count - 1))
        bars = []
        for i in range(count):
            stamp = first_open + timedelta(seconds=self.interval * i)
            price = 2600.0 + i
            bars.append({"time": stamp, "open": price, "high": price + 2,
                         "low": price - 2, "close": price + 1})
        return bars, now

    def test_sorts_and_collapses_identical_duplicates(self):
        bars, now = self.make_bars()
        shuffled = list(reversed(bars)) + [bars[10].copy()]
        result = validate_candles(shuffled, timeframe_seconds=self.interval, now=now)
        self.assertEqual(len(result), len(bars))
        self.assertEqual(result, bars)

    def test_rejects_conflicting_duplicate_timestamp(self):
        bars, now = self.make_bars()
        duplicate = bars[10].copy()
        duplicate["close"] += 5
        duplicate["high"] += 5
        with self.assertRaisesRegex(RuntimeError, "conflicting duplicate"):
            validate_candles(bars + [duplicate], timeframe_seconds=self.interval, now=now)

    def test_rejects_invalid_ohlc_relationship(self):
        bars, now = self.make_bars()
        bars[5]["high"] = bars[5]["open"] - 1
        with self.assertRaisesRegex(RuntimeError, "OHLC"):
            validate_candles(bars, timeframe_seconds=self.interval, now=now)

    def test_rejects_non_finite_price(self):
        bars, now = self.make_bars()
        bars[5]["close"] = float("nan")
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            validate_candles(bars, timeframe_seconds=self.interval, now=now)

    def test_rejects_unclosed_candle(self):
        bars, now = self.make_bars()
        bars[-1]["time"] = now - timedelta(seconds=100)
        with self.assertRaisesRegex(RuntimeError, "unclosed or future"):
            validate_candles(bars, timeframe_seconds=self.interval, now=now)

    def test_rejects_stale_data_during_open_session(self):
        bars, _ = self.make_bars()
        now = bars[-1]["time"] + timedelta(seconds=self.interval * 8)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            validate_candles(bars, timeframe_seconds=self.interval, now=now)

    def test_rejects_internal_gap(self):
        bars, now = self.make_bars()
        del bars[20]
        with self.assertRaisesRegex(RuntimeError, "Missing XAU/USD candles"):
            validate_candles(bars, timeframe_seconds=self.interval, now=now)

    def test_rejects_too_little_valid_history(self):
        bars, now = self.make_bars(count=20)
        with self.assertRaisesRegex(RuntimeError, "at least 60"):
            validate_candles(bars, timeframe_seconds=self.interval, now=now)

    def test_recognizes_daily_maintenance_window_only_when_bounded(self):
        start = datetime(2026, 11, 10, 20, 45, tzinfo=timezone.utc)
        end = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)
        extended = datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc)
        self.assertTrue(_is_expected_gold_pause(start, end))
        self.assertFalse(_is_expected_gold_pause(start, extended))

    def test_weekend_pause_ends_after_sunday_open(self):
        friday = datetime(2026, 11, 13, 21, 0, tzinfo=timezone.utc)
        sunday_before_open = datetime(2026, 11, 15, 21, 30, tzinfo=timezone.utc)
        sunday_after_open = datetime(2026, 11, 15, 23, 0, tzinfo=timezone.utc)
        self.assertTrue(_is_expected_gold_pause(friday, sunday_before_open))
        self.assertFalse(_is_expected_gold_pause(friday, sunday_after_open))


class RetryTests(unittest.TestCase):
    def test_retries_network_timeout_then_succeeds(self):
        session = Mock()
        response = Mock()
        response.status_code = 200
        response.headers = {}
        session.get.side_effect = [requests.Timeout("temporary"), response]
        with patch("gold_signal.market_data.time.sleep"):
            result = _get_with_retry(session, "https://example.invalid",
                                     params={}, timeout=5, attempts=2, base_delay=0.1)
        self.assertIs(result, response)
        self.assertEqual(session.get.call_count, 2)

    def test_retries_rate_limit_using_retry_after(self):
        session = Mock()
        limited = Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "0"}
        success = Mock()
        success.status_code = 200
        success.headers = {}
        session.get.side_effect = [limited, success]
        with patch("gold_signal.market_data.time.sleep") as sleeper:
            result = _get_with_retry(session, "https://example.invalid",
                                     params={}, timeout=5, attempts=2, base_delay=0.1)
        self.assertIs(result, success)
        self.assertEqual(session.get.call_count, 2)
        sleeper.assert_called_once_with(0.0)


class StateAndLockTests(unittest.TestCase):
    def test_state_round_trip(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            stamp = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
            self.assertEqual(read_state(path), "")
            write_state(path, stamp)
            self.assertEqual(read_state(path), stamp.isoformat())

    def test_process_lock_rejects_second_instance(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            context = multiprocessing.get_context("spawn")
            parent_connection, child_connection = context.Pipe(duplex=False)
            with process_lock(path):
                child = context.Process(
                    target=_child_lock_attempt,
                    args=(str(path), child_connection),
                )
                child.start()
                child.join(timeout=10)
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
                    self.fail("Child process hung while checking the OS-level lock")
                self.assertEqual(child.exitcode, 0)
                self.assertTrue(parent_connection.poll(3), "Child process returned no lock result")
                self.assertEqual(parent_connection.recv(), "blocked")
            parent_connection.close()


class ConfigTests(unittest.TestCase):
    def test_requires_market_data_api_key(self):
        with patch.dict("os.environ", {"GOLD_TELEGRAM_BOT_TOKEN": "t", "GOLD_TELEGRAM_CHAT_ID": "c"}, clear=True):
            with self.assertRaisesRegex(ValueError, "GOLD_TWELVE_DATA_API_KEY"):
                Config.from_env()

    def test_requires_telegram_bot_token(self):
        with patch.dict("os.environ", {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_CHAT_ID": "c"}, clear=True):
            with self.assertRaisesRegex(ValueError, "GOLD_TELEGRAM_BOT_TOKEN"):
                Config.from_env()

    def test_requires_telegram_chat_id(self):
        with patch.dict("os.environ", {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t"}, clear=True):
            with self.assertRaisesRegex(ValueError, "GOLD_TELEGRAM_CHAT_ID"):
                Config.from_env()

    def test_rejects_unsupported_timeframe(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_TIMEFRAME": "2min"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "Unsupported GOLD_TIMEFRAME"):
                Config.from_env()

    def test_rejects_poll_interval_below_minimum(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_POLL_SECONDS": "9"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "at least 10"):
                Config.from_env()

    def test_rejects_nonpositive_atr_multiplier(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_RISK_ATR_MULTIPLIER": "0"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "finite positive"):
                Config.from_env()

    def test_requires_tp2_greater_than_tp1(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_TP1_R": "2", "GOLD_TP2_R": "2"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "TP2_R must be greater"):
                Config.from_env()

    def test_rejects_retry_attempts_above_limit(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_RETRY_ATTEMPTS": "9"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "between 1 and 8"):
                Config.from_env()

    def test_rejects_zero_retry_base_delay(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_RETRY_BASE_SECONDS": "0"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "finite positive"):
                Config.from_env()

    def test_reads_custom_environment_values(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_TIMEFRAME": "1h",
               "GOLD_POLL_SECONDS": "120", "GOLD_RETRY_ATTEMPTS": "5",
               "GOLD_RETRY_BASE_SECONDS": "0.25", "GOLD_MAX_GAP_MULTIPLIER": "2",
               "GOLD_SIGNAL_STATE_FILE": str(Path.cwd() / "gold-test-state.json")}
        with patch.dict("os.environ", env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.timeframe, "1h")
        self.assertEqual(config.poll_seconds, 120)
        self.assertEqual(config.retry_attempts, 5)
        self.assertAlmostEqual(config.retry_base_seconds, 0.25)
        self.assertAlmostEqual(config.gap_multiplier, 2.0)

    def test_rejects_invalid_gap_multiplier(self):
        env = {"GOLD_TWELVE_DATA_API_KEY": "k", "GOLD_TELEGRAM_BOT_TOKEN": "t",
               "GOLD_TELEGRAM_CHAT_ID": "c", "GOLD_MAX_GAP_MULTIPLIER": "-1"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaisesRegex(ValueError, "finite positive"):
                Config.from_env()


class FetchTests(unittest.TestCase):
    def make_config(self, folder):
        return Config("api-key", "bot-token", "chat-id", "15min", 45, 1.5, 1.0, 2.0,
                      Path(folder) / "state.json", 5, 2, 0.01, 3.0, 1.5)

    def make_payload(self, count=65):
        now = datetime.now(timezone.utc)
        last_open = now - timedelta(seconds=905)
        first_open = last_open - timedelta(seconds=900 * (count - 1))
        values = []
        for i in range(count):
            stamp = first_open + timedelta(seconds=900 * i)
            price = 2600.0 + i * 0.1
            values.append({"datetime": stamp.isoformat(), "open": str(price),
                           "high": str(price + 1), "low": str(price - 1),
                           "close": str(price + 0.5)})
        return values

    def test_fetches_valid_spot_xauusd_and_sorts_bars(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = {"values": list(reversed(self.make_payload()))}
            session = Mock()
            session.get.return_value = response
            bars = fetch_closed_candles(config, session=session)
        self.assertEqual(len(bars), 65)
        self.assertLess(bars[0]["time"], bars[-1]["time"])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["symbol"], "XAU/USD")
        self.assertEqual(params["interval"], "15min")
        self.assertEqual(params["apikey"], "api-key")

    def test_provider_error_payload_is_reported(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = {"status": "error", "message": "Invalid API key"}
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "Twelve Data error"):
                fetch_closed_candles(config, session=session)

    def test_provider_rate_limit_payload_is_reported(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = {"status": "error", "message": "Rate limit exceeded"}
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "rate limit"):
                fetch_closed_candles(config, session=session)

    def test_empty_provider_values_are_rejected(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = {"values": []}
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "no XAU/USD candles"):
                fetch_closed_candles(config, session=session)

    def test_non_object_provider_payload_is_rejected(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = ["unexpected"]
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "unexpected response"):
                fetch_closed_candles(config, session=session)

    def test_malformed_provider_candle_is_rejected(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = {"values": [{"datetime": "2026-11-10T10:00:00Z"}]}
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "invalid fields"):
                fetch_closed_candles(config, session=session)

    def test_network_timeout_after_retries_is_propagated(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            session = Mock()
            session.get.side_effect = requests.Timeout("offline")
            with patch("gold_signal.market_data.time.sleep"):
                with self.assertRaises(requests.Timeout):
                    fetch_closed_candles(config, session=session)
        self.assertEqual(session.get.call_count, config.retry_attempts)

    def test_provider_response_with_non_dict_candle_is_rejected(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.json.return_value = {"values": ["bad-record"]}
            session = Mock()
            session.get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "malformed candle"):
                fetch_closed_candles(config, session=session)


class TelegramTests(unittest.TestCase):
    def make_config(self, folder):
        return Config("api-key", "secret-bot-token", "channel-123", "15min", 45, 1.5, 1.0, 2.0,
                      Path(folder) / "state.json", 5, 2, 0.01, 3.0, 1.5)

    def test_sends_message_with_expected_telegram_fields(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.json.return_value = {"ok": True, "result": {"message_id": 7}}
            session = Mock()
            session.post.return_value = response
            send_telegram(config, "hello", session=session)
        args, kwargs = session.post.call_args
        self.assertIn("secret-bot-token", args[0])
        self.assertEqual(kwargs["data"]["chat_id"], "channel-123")
        self.assertEqual(kwargs["data"]["text"], "hello")
        self.assertEqual(kwargs["data"]["parse_mode"], "HTML")

    def test_raises_on_telegram_http_error(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.raise_for_status.side_effect = requests.HTTPError("bad response")
            session = Mock()
            session.post.return_value = response
            with self.assertRaises(requests.HTTPError):
                send_telegram(config, "hello", session=session)

    def test_raises_when_telegram_api_returns_ok_false(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            response = Mock()
            response.json.return_value = {"ok": False, "description": "chat not found"}
            session = Mock()
            session.post.return_value = response
            with self.assertRaisesRegex(RuntimeError, "Telegram rejected"):
                send_telegram(config, "hello", session=session)

    def test_does_not_retry_ambiguous_telegram_timeout(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            session = Mock()
            session.post.side_effect = requests.Timeout("response lost")
            with self.assertRaises(requests.Timeout):
                send_telegram(config, "hello", session=session)
        self.assertEqual(session.post.call_count, 1)


class SignalGenerationTests(unittest.TestCase):
    def make_bars(self, side="BUY"):
        if side == "BUY":
            base = [2600 + i * 0.25 + (0.4 if i % 2 == 0 else -0.4) for i in range(58)]
            closes = base + [base[-1] - 2, base[-1] + 2]
        elif side == "SELL":
            base = [2700 - i * 0.25 + (0.4 if i % 2 == 0 else -0.4) for i in range(58)]
            closes = base + [base[-1] + 3, base[-1] + 1]
        else:
            closes = [2600.0] * 60
        start = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)
        bars = []
        for i, close in enumerate(closes):
            open_price = close - 0.1
            bars.append({"time": start + timedelta(minutes=15 * i), "open": open_price,
                         "high": max(open_price, close) + 1,
                         "low": min(open_price, close) - 1, "close": close})
        return bars

    def test_detects_buy_setup(self):
        bars = self.make_bars("BUY")
        result = detect_signal(bars)
        self.assertIsNotNone(result)
        self.assertEqual(
            set(result),
            {"symbol", "side", "entry", "stop_loss", "take_profit_1",
             "take_profit_2", "atr", "rsi", "ema_fast", "ema_slow", "candle_time"},
        )
        self.assertEqual(result["symbol"], "XAU/USD")
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["entry"], bars[-1]["close"])
        self.assertEqual(result["candle_time"], bars[-1]["time"])
        self.assertAlmostEqual(result["ema_fast"], 2612.2354104434135, places=9)
        self.assertAlmostEqual(result["ema_slow"], 2608.5662684642316, places=9)
        self.assertAlmostEqual(result["rsi"], 65.54844513700353, places=9)
        self.assertAlmostEqual(result["atr"], 2.373469387755018, places=9)
        self.assertGreaterEqual(result["rsi"], 50.0)
        self.assertLessEqual(result["rsi"], 70.0)
        self.assertGreater(result["ema_fast"], result["ema_slow"])
        self.assertLess(result["stop_loss"], result["entry"])
        self.assertGreater(result["take_profit_1"], result["entry"])
        self.assertGreater(result["take_profit_2"], result["take_profit_1"])
        risk = result["entry"] - result["stop_loss"]
        self.assertAlmostEqual(risk, result["atr"] * 1.5, places=9)
        self.assertAlmostEqual(result["take_profit_1"] - result["entry"], risk, places=9)
        self.assertAlmostEqual(result["take_profit_2"] - result["entry"], risk * 2.0, places=9)

    def test_detects_sell_setup(self):
        bars = self.make_bars("SELL")
        result = detect_signal(bars)
        self.assertIsNotNone(result)
        self.assertEqual(
            set(result),
            {"symbol", "side", "entry", "stop_loss", "take_profit_1",
             "take_profit_2", "atr", "rsi", "ema_fast", "ema_slow", "candle_time"},
        )
        self.assertEqual(result["symbol"], "XAU/USD")
        self.assertEqual(result["side"], "SELL")
        self.assertEqual(result["entry"], bars[-1]["close"])
        self.assertEqual(result["candle_time"], bars[-1]["time"])
        self.assertAlmostEqual(result["ema_fast"], 2687.959333345908, places=9)
        self.assertAlmostEqual(result["ema_slow"], 2691.5234964534675, places=9)
        self.assertAlmostEqual(result["rsi"], 41.05520236728954, places=9)
        self.assertAlmostEqual(result["atr"], 2.3198364580734023, places=9)
        self.assertGreaterEqual(result["rsi"], 30.0)
        self.assertLessEqual(result["rsi"], 50.0)
        self.assertLess(result["ema_fast"], result["ema_slow"])
        self.assertGreater(result["stop_loss"], result["entry"])
        self.assertLess(result["take_profit_1"], result["entry"])
        self.assertLess(result["take_profit_2"], result["take_profit_1"])
        risk = result["stop_loss"] - result["entry"]
        self.assertAlmostEqual(risk, result["atr"] * 1.5, places=9)
        self.assertAlmostEqual(result["entry"] - result["take_profit_1"], risk, places=9)
        self.assertAlmostEqual(result["entry"] - result["take_profit_2"], risk * 2.0, places=9)

    def test_flat_market_does_not_create_signal(self):
        self.assertIsNone(detect_signal(self.make_bars("FLAT")))

    def test_rejects_buy_cross_when_rsi_exceeds_upper_bound(self):
        bars = self.make_bars("BUY")
        bars[-1]["close"] += 5.0
        bars[-1]["high"] = bars[-1]["close"] + 1.0
        self.assertIsNone(detect_signal(bars))

    def test_rejects_sell_cross_when_rsi_below_lower_bound(self):
        bars = self.make_bars("SELL")
        bars[-1]["close"] -= 10.0
        bars[-1]["low"] = bars[-1]["close"] - 1.0
        self.assertIsNone(detect_signal(bars))

    def test_buy_stop_is_below_entry_and_targets_above(self):
        result = detect_signal(self.make_bars("BUY"))
        self.assertLess(result["stop_loss"], result["entry"])
        self.assertGreater(result["take_profit_1"], result["entry"])
        self.assertGreater(result["take_profit_2"], result["take_profit_1"])

    def test_sell_stop_is_above_entry_and_targets_below(self):
        result = detect_signal(self.make_bars("SELL"))
        self.assertGreater(result["stop_loss"], result["entry"])
        self.assertLess(result["take_profit_1"], result["entry"])
        self.assertLess(result["take_profit_2"], result["take_profit_1"])

    def test_custom_r_multiples_are_applied(self):
        result = detect_signal(self.make_bars("BUY"), atr_multiplier=2.0, tp1_r=1.5, tp2_r=3.0)
        risk = result["entry"] - result["stop_loss"]
        self.assertAlmostEqual(risk, result["atr"] * 2.0)
        self.assertAlmostEqual(result["take_profit_1"] - result["entry"], risk * 1.5)
        self.assertAlmostEqual(result["take_profit_2"] - result["entry"], risk * 3.0)

    def test_short_history_returns_no_signal(self):
        self.assertIsNone(detect_signal(self.make_bars("BUY")[:59]))

    def test_signal_carries_the_last_candle_timestamp(self):
        bars = self.make_bars("BUY")
        result = detect_signal(bars)
        self.assertEqual(result["candle_time"], bars[-1]["time"])

    def test_signal_is_for_spot_xauusd_symbol(self):
        result = detect_signal(self.make_bars("BUY"))
        self.assertEqual(result["symbol"], "XAU/USD")


class FormattingTests(unittest.TestCase):
    def test_formats_candle_timestamp_as_utc(self):
        stamp = datetime(2026, 11, 10, 12, 30, tzinfo=timezone.utc)
        data = {"side": "BUY", "entry": 2600, "stop_loss": 2590,
                "take_profit_1": 2610, "take_profit_2": 2620,
                "rsi": 60, "atr": 5, "candle_time": stamp}
        message = format_signal(data, "15min")
        self.assertIn("2026-11-10 12:30 UTC", message)

    def test_message_contains_signal_risk_and_signal_only_notice(self):
        stamp = datetime(2026, 11, 10, 12, 30, tzinfo=timezone.utc)
        data = {"side": "SELL", "entry": 2600, "stop_loss": 2610,
                "take_profit_1": 2590, "take_profit_2": 2580,
                "rsi": 40, "atr": 5, "candle_time": stamp}
        message = format_signal(data, "1h")
        for fragment in ("SELL", "1h", "حد ضرر", "حد سود", "هیچ سفارشی اجرا نشده"):
            self.assertIn(fragment, message)


class AdditionalStateTests(unittest.TestCase):
    def test_corrupt_state_json_fails_safe_to_empty(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("{not-json", encoding="utf-8")
            self.assertEqual(read_state(path), "")

    def test_non_object_state_json_fails_safe_to_empty(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("[]", encoding="utf-8")
            self.assertEqual(read_state(path), "")

    def test_write_state_creates_parent_and_cleans_temp_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "state.json"
            stamp = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
            write_state(path, stamp)
            self.assertEqual(read_state(path), stamp.isoformat())
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class RunOnceTests(unittest.TestCase):
    def make_config(self, folder):
        return Config("api-key", "bot-token", "chat-id", "15min", 45, 1.5, 1.0, 2.0,
                      Path(folder) / "state.json", 5, 2, 0.01, 3.0, 1.5)

    def make_signal(self, stamp):
        return {"symbol": "XAU/USD", "side": "BUY", "entry": 2600.0,
                "stop_loss": 2590.0, "take_profit_1": 2610.0,
                "take_profit_2": 2620.0, "atr": 6.0, "rsi": 60.0,
                "ema_fast": 2598.0, "ema_slow": 2590.0, "candle_time": stamp}

    def test_no_signal_does_not_send_telegram(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            with patch("gold_signal.service.fetch_closed_candles", return_value=[]), \
                 patch("gold_signal.service.detect_signal", return_value=None), \
                 patch("gold_signal.service.send_telegram") as sender:
                result = run_once(config)
        self.assertEqual(result, "no_signal")
        sender.assert_not_called()

    def test_duplicate_signal_is_not_sent_twice(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            stamp = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
            write_state(config.state_file, stamp)
            with patch("gold_signal.service.fetch_closed_candles", return_value=[]), \
                 patch("gold_signal.service.detect_signal", return_value=self.make_signal(stamp)), \
                 patch("gold_signal.service.send_telegram") as sender:
                result = run_once(config)
        self.assertEqual(result, "duplicate")
        sender.assert_not_called()

    def test_successful_send_persists_signal_candle(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            stamp = datetime(2026, 11, 10, 10, 15, tzinfo=timezone.utc)
            with patch("gold_signal.service.fetch_closed_candles", return_value=[]), \
                 patch("gold_signal.service.detect_signal", return_value=self.make_signal(stamp)), \
                 patch("gold_signal.service.format_signal", return_value="test message"), \
                 patch("gold_signal.service.send_telegram") as sender:
                result = run_once(config)
            self.assertEqual(result, "sent")
            sender.assert_called_once()
            self.assertEqual(read_state(config.state_file), stamp.isoformat())

    def test_failed_telegram_send_does_not_mark_signal_as_sent(self):
        with TemporaryDirectory() as folder:
            config = self.make_config(folder)
            stamp = datetime(2026, 11, 10, 10, 30, tzinfo=timezone.utc)
            with patch("gold_signal.service.fetch_closed_candles", return_value=[]), \
                 patch("gold_signal.service.detect_signal", return_value=self.make_signal(stamp)), \
                 patch("gold_signal.service.format_signal", return_value="test message"), \
                 patch("gold_signal.service.send_telegram", side_effect=requests.Timeout("offline")):
                with self.assertRaises(requests.Timeout):
                    run_once(config)
            self.assertEqual(read_state(config.state_file), "")


if __name__ == "__main__":
    unittest.main()
