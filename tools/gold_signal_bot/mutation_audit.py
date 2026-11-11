"""Small, deterministic mutation audit for critical GOLD notifier behavior.

Each mutation is applied only to an isolated temporary copy. The repository
working tree is never edited by this runner. A mutation is killed only when its
specific regression test fails; a surviving mutant makes this command fail.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one mutation point, found {count}")
    return source.replace(old, new, 1)


MUTATIONS = [
    {
        "label": "BUY cross direction inverted",
        "path": "gold_signal/strategy.py",
        "old": "close > fast[i] and fast[i] > slow[i]",
        "new": "close < fast[i] and fast[i] > slow[i]",
        "test": "test_gold_signal_bot.SignalGenerationTests.test_detects_buy_setup",
    },
    {
        "label": "BUY trend EMA ordering inverted",
        "path": "gold_signal/strategy.py",
        "old": "fast[i] > slow[i] and 50 <= momentum[i] <= 70",
        "new": "fast[i] < slow[i] and 50 <= momentum[i] <= 70",
        "test": "test_gold_signal_bot.SignalGenerationTests.test_detects_buy_setup",
    },
    {
        "label": "BUY RSI upper-bound filter removed",
        "path": "gold_signal/strategy.py",
        "old": "50 <= momentum[i] <= 70",
        "new": "0 <= momentum[i] <= 100",
        "test": "test_gold_signal_bot.SignalGenerationTests.test_rejects_buy_cross_when_rsi_exceeds_upper_bound",
    },
    {
        "label": "SELL cross direction inverted",
        "path": "gold_signal/strategy.py",
        "old": "close < fast[i] and fast[i] < slow[i]",
        "new": "close > fast[i] and fast[i] < slow[i]",
        "test": "test_gold_signal_bot.SignalGenerationTests.test_detects_sell_setup",
    },
    {
        "label": "SELL trend EMA ordering inverted",
        "path": "gold_signal/strategy.py",
        "old": "fast[i] < slow[i] and 30 <= momentum[i] <= 50",
        "new": "fast[i] > slow[i] and 30 <= momentum[i] <= 50",
        "test": "test_gold_signal_bot.SignalGenerationTests.test_detects_sell_setup",
    },
    {
        "label": "SELL RSI lower-bound filter removed",
        "path": "gold_signal/strategy.py",
        "old": "30 <= momentum[i] <= 50",
        "new": "0 <= momentum[i] <= 100",
        "test": "test_gold_signal_bot.SignalGenerationTests.test_rejects_sell_cross_when_rsi_below_lower_bound",
    },
    {
        "label": "OHLC high invariant removed",
        "path": "gold_signal/market_data.py",
        "old": "h < max(o, close, low) or low > min(o, close)",
        "new": "low > min(o, close)",
        "test": "test_gold_signal_bot.CandleValidationTests.test_rejects_invalid_ohlc_relationship",
    },
    {
        "label": "stale-candle rejection disabled",
        "path": "gold_signal/market_data.py",
        "old": "if age_seconds > max_age and not _is_expected_gold_pause(latest_end, current):",
        "new": "if False and age_seconds > max_age and not _is_expected_gold_pause(latest_end, current):",
        "test": "test_gold_signal_bot.CandleValidationTests.test_rejects_stale_data_during_open_session",
    },
    {
        "label": "Retry-After ignored",
        "path": "gold_signal/market_data.py",
        "old": "delay = min(30.0, max(0.0, float(retry_after)))",
        "new": "delay = min(30.0, max(0.0, base_delay * (2 ** attempt)))",
        "test": "test_gold_signal_bot.RetryTests.test_retries_rate_limit_using_retry_after",
    },
]

if os.name == "nt":
    MUTATIONS.append({
        "label": "Windows OS lock acquisition removed",
        "path": "gold_signal/state.py",
        "old": "msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)",
        "new": "pass  # deliberate mutation: OS lock removed",
        "test": "test_gold_signal_bot.StateAndLockTests.test_process_lock_rejects_second_instance",
    })
else:
    MUTATIONS.append({
        "label": "POSIX OS lock acquisition removed",
        "path": "gold_signal/state.py",
        "old": "fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
        "new": "pass  # deliberate mutation: OS lock removed",
        "test": "test_gold_signal_bot.StateAndLockTests.test_process_lock_rejects_second_instance",
    })


def isolated_copy(destination: Path) -> None:
    shutil.copytree(ROOT / "gold_signal", destination / "gold_signal")
    shutil.copy2(ROOT / "gold_signal_bot.py", destination / "gold_signal_bot.py")
    for test_file in ROOT.glob("test_*.py"):
        shutil.copy2(test_file, destination / test_file.name)


def run_mutation(mutation: dict[str, str]) -> bool:
    with tempfile.TemporaryDirectory(prefix="gold-mutation-") as folder:
        sandbox = Path(folder)
        isolated_copy(sandbox)
        source_path = sandbox / mutation["path"]
        source = source_path.read_text(encoding="utf-8")
        mutated = replace_once(source, mutation["old"], mutation["new"], mutation["label"])
        source_path.write_text(mutated, encoding="utf-8")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(sandbox) + os.pathsep + env.get("PYTHONPATH", "")
        command = [sys.executable, "-m", "unittest", mutation["test"], "-v"]
        result = subprocess.run(
            command, cwd=sandbox, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45,
        )
        killed = result.returncode != 0
        state = "KILLED" if killed else "SURVIVED"
        print(f"[{state}] {mutation['label']} -> {mutation['test']}")
        if not killed:
            print(result.stdout)
        return killed


def main() -> int:
    outcomes = [run_mutation(mutation) for mutation in MUTATIONS]
    killed = sum(outcomes)
    survived = len(outcomes) - killed
    print(f"Mutation audit: {killed}/{len(outcomes)} killed; {survived} survived")
    return 0 if survived == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
