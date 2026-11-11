"""AST fingerprint guard for strategy and indicator logic."""
from __future__ import annotations

import unittest

from gold_signal.strategy_version import (
    STRATEGY_AST_HASH,
    STRATEGY_VERSION,
    current_strategy_ast_hash,
    strategy_ast_hash,
)


class StrategyVersionTests(unittest.TestCase):
    def test_committed_strategy_and_indicator_ast_matches_fingerprint(self):
        self.assertTrue(STRATEGY_VERSION.strip())
        self.assertRegex(STRATEGY_VERSION, r"-v[1-9][0-9]*$")
        self.assertEqual(
            current_strategy_ast_hash(),
            STRATEGY_AST_HASH,
            "Strategy/indicator logic changed: deliberately bump STRATEGY_VERSION "
            "and update STRATEGY_AST_HASH together in strategy_version.py.",
        )

    def test_comments_formatting_and_docstrings_do_not_change_fingerprint(self):
        original = {
            "strategy.py": '"""module docs"""\ndef signal(x):\n    """function docs"""\n    return x > 10\n',
            "indicators.py": "def rsi(x):\n    return x / 2\n",
        }
        cosmetic = {
            "strategy.py": '# comment\n\ndef signal(x):\n    """new docs"""\n\n    return x>10 # trailing comment\n',
            "indicators.py": '"""indicator docs"""\n\ndef rsi(x):\n    # comment\n    return (x / 2)\n',
        }
        self.assertEqual(strategy_ast_hash(original), strategy_ast_hash(cosmetic))

    def test_logic_change_changes_fingerprint(self):
        original = {
            "strategy.py": "def signal(x):\n    return x > 10\n",
            "indicators.py": "def rsi(x):\n    return x / 2\n",
        }
        changed = {
            "strategy.py": "def signal(x):\n    return x >= 10\n",
            "indicators.py": "def rsi(x):\n    return x / 2\n",
        }
        self.assertNotEqual(strategy_ast_hash(original), strategy_ast_hash(changed))


if __name__ == "__main__":
    unittest.main()
