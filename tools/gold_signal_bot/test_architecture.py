"""Architecture boundary and backwards-compatibility contract tests."""
import ast
from pathlib import Path
import unittest

import gold_signal_bot
from gold_signal import indicators, market_data, service, state, strategy, telegram

PACKAGE = Path(__file__).parent / "gold_signal"


class ArchitectureContractTests(unittest.TestCase):
    def test_legacy_facade_reexports_modular_public_api(self):
        self.assertIs(gold_signal_bot.ema, indicators.ema)
        self.assertIs(gold_signal_bot.atr, indicators.atr)
        self.assertIs(gold_signal_bot.detect_signal, strategy.detect_signal)
        self.assertIs(gold_signal_bot.fetch_closed_candles, market_data.fetch_closed_candles)
        self.assertIs(gold_signal_bot.run_once, service.run_once)
        self.assertIs(gold_signal_bot.process_lock, state.process_lock)
        self.assertIs(gold_signal_bot.send_telegram, telegram.send_telegram)

    def test_low_level_modules_do_not_import_application_or_delivery_layers(self):
        forbidden = {
            "config": {"cli", "service", "market_data", "strategy", "telegram", "state"},
            "indicators": {"cli", "service", "market_data", "strategy", "telegram", "state"},
            "market_data": {"cli", "service", "strategy", "telegram", "state"},
            "strategy": {"cli", "service", "market_data", "telegram", "state"},
            "telegram": {"cli", "service", "market_data", "strategy", "state"},
            "state": {"cli", "service", "market_data", "strategy", "telegram"},
            "service": {"cli"},
        }
        for module, blocked in forbidden.items():
            tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.rsplit(".", 1)[-1])
                elif isinstance(node, ast.Import):
                    imported.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
            self.assertFalse(
                blocked.intersection(imported),
                f"{module} imports forbidden layers: {sorted(blocked.intersection(imported))}",
            )

    def test_dotenv_loading_is_confined_to_cli_main(self):
        tree = ast.parse((PACKAGE / "cli.py").read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "load_dotenv"
        ]
        self.assertEqual(len(calls), 1)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        self.assertIn(calls[0], list(ast.walk(main)))

    def test_service_is_the_orchestration_boundary(self):
        self.assertIs(gold_signal_bot.run_once, service.run_once)
        self.assertTrue(callable(service.run_once))


if __name__ == "__main__":
    unittest.main()
