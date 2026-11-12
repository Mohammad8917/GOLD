"""Regression tests for the version-independent strategy source fingerprint."""
from __future__ import annotations

import unittest

from gold_signal.strategy_version import (
    STRATEGY_SOURCE_HASH,
    STRATEGY_VERSION,
    STRATEGY_VERSION_HISTORY,
    current_strategy_source_hash,
    strategy_source_hash,
)


class StrategyVersionTests(unittest.TestCase):
    def test_committed_strategy_and_indicator_source_matches_fingerprint(self):
        self.assertTrue(STRATEGY_VERSION.strip())
        self.assertRegex(STRATEGY_VERSION, r"-v[1-9][0-9]*$")
        self.assertEqual(STRATEGY_VERSION, STRATEGY_VERSION_HISTORY[-1][0])
        self.assertEqual(STRATEGY_SOURCE_HASH, STRATEGY_VERSION_HISTORY[-1][1])
        self.assertEqual(
            len({version for version, _ in STRATEGY_VERSION_HISTORY}),
            len(STRATEGY_VERSION_HISTORY),
        )
        self.assertEqual(
            current_strategy_source_hash(),
            STRATEGY_SOURCE_HASH,
            "Strategy/indicator source changed: deliberately bump STRATEGY_VERSION "
            "and update STRATEGY_SOURCE_HASH together in strategy_version.py.",
        )

    def test_comments_and_whitespace_do_not_change_fingerprint(self):
        original = {
            "strategy.py": "def signal(x):\n    return x > 10\n",
            "indicators.py": "def rsi(x):\n    return x / 2\n",
        }
        cosmetic = {
            "strategy.py": "# leading comment\n\ndef signal ( x ) :  # trailing comment\n\n    return x>10  # inline\n",
            "indicators.py": "def rsi( x ):\n\treturn ( x/2 )\n",
        }
        self.assertEqual(strategy_source_hash(original), strategy_source_hash(cosmetic))

    def test_logic_change_changes_fingerprint(self):
        original = {
            "strategy.py": "def signal(x):\n    return x > 10\n",
            "indicators.py": "def rsi(x):\n    return x / 2\n",
        }
        changed = {
            "strategy.py": "def signal(x):\n    return x >= 10\n",
            "indicators.py": "def rsi(x):\n    return x / 2\n",
        }
        self.assertNotEqual(strategy_source_hash(original), strategy_source_hash(changed))

    def test_string_literal_change_changes_fingerprint(self):
        original = {"strategy.py": 'def label():\n    return "BUY"\n'}
        changed = {"strategy.py": 'def label():\n    return "SELL"\n'}
        self.assertNotEqual(strategy_source_hash(original), strategy_source_hash(changed))

    def test_docstring_change_changes_fingerprint(self):
        original = {"strategy.py": '"""strategy docs"""\ndef signal():\n    return True\n'}
        changed = {"strategy.py": '"""updated strategy docs"""\ndef signal():\n    return True\n'}
        self.assertNotEqual(strategy_source_hash(original), strategy_source_hash(changed))


if __name__ == "__main__":
    unittest.main()
