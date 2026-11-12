"""Static safety contract: the bot may read market data and send Telegram alerts only."""
from __future__ import annotations

import re
import unittest
from pathlib import Path


class SignalOnlyArchitectureTests(unittest.TestCase):
    def test_runtime_has_no_exchange_sdk_or_order_execution_calls(self):
        root = Path(__file__).resolve().parents[1]
        runtime_files = list((root / "gold_signal").glob("*.py")) + [
            root / "gold_signal_bot.py",
            root / "backtest_historical.py",
            root / "export_parity_fixture.py",
        ]
        forbidden_import = re.compile(
            r"(?im)^\s*(?:from|import)\s+"
            r"(?:ccxt|binance|kucoin|krakenex|coinbase|pybit|okx|gate_api)\b"
        )
        forbidden_order_call = re.compile(
            r"\b(create_order|place_order|submit_order|cancel_order|futures_create_order|order_market_buy|order_market_sell)\s*\(",
            re.IGNORECASE,
        )
        forbidden_exchange_host = re.compile(
            r"(?i)https?://(?:api\.)?(?:binance|bybit|okx|kraken|coinbase)\."
        )
        post_call_files = []
        for path in runtime_files:
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(forbidden_import.search(source), str(path))
            self.assertIsNone(forbidden_order_call.search(source), str(path))
            self.assertIsNone(forbidden_exchange_host.search(source), str(path))
            if re.search(r"\.post\s*\(", source):
                post_call_files.append(path.name)
        self.assertEqual(post_call_files, ["telegram.py"])

    def test_runtime_dependencies_are_minimal_and_exchange_free(self):
        requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
        packages = {
            line.split("#", 1)[0].strip().split("=", 1)[0].split(">", 1)[0].split("<", 1)[0].lower()
            for line in requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(packages, {"requests", "python-dotenv"})


if __name__ == "__main__":
    unittest.main()