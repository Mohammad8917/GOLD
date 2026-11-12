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
        "label": "flat-market RSI forced to 100",
        "path": "gold_signal/indicators.py",
        "old": "return 50.0",
        "new": "return 100.0",
        "test": "test_gold_signal_bot.IndicatorTests.test_rsi_flat_market_is_neutral",
    },
    {
        "label": "outage history-gap guard disabled",
        "path": "gold_signal/service.py",
        "old": "if cursor < first_time:",
        "new": "if False and cursor < first_time:",
        "test": "test_gold_signal_bot.RunOnceTests.test_history_gap_fails_closed_instead_of_silently_skipping",
    },
    {
        "label": "BUY cross direction inverted",
        "path": "gold_signal/strategy.py",
        "old": "close > fast[i]",
        "new": "close < fast[i]",
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
        "old": "close < fast[i]",
        "new": "close > fast[i]",
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
        "label": "daily quota near-limit boundary misclassified as minute",
        "path": "gold_signal/quota.py",
        "old": "daily_used >= daily_threshold",
        "new": "daily_used > daily_threshold",
        "test": "tests.test_quota_management.QuotaManagementTests.test_daily_usage_near_95_percent_threshold_is_classified_as_daily",
    },
    {
        "label": "pre-emptive quota warning threshold raised above 80 percent",
        "path": "gold_signal/quota_warning.py",
        "old": "used / limit >= DAILY_WARNING_RATIO",
        "new": "used / limit > DAILY_WARNING_RATIO",
        "test": "tests.test_quota_management.QuotaManagementTests.test_daily_warning_at_80_percent_is_sent_only_once_per_utc_day",
    },
    {
        "label": "failed daily usage check repeats on every poll cycle",
        "path": "gold_signal/quota_warning.py",
        "old": "        try:\n            usage = inspect_api_usage",
        "new": "        state[\"last_usage_check_date\"] = today\n        _write_state(path, state)\n        try:\n            usage = inspect_api_usage",
        "test": "tests.test_quota_management.QuotaManagementTests.test_failed_daily_usage_check_is_retried_on_next_poll_cycle",
    },
    {
        "label": "exchange order execution call introduced",
        "path": "gold_signal/service.py",
        "old": "LOG = logging.getLogger(\"gold_signal_bot\")",
        "new": "def create_order():\n    pass\n\nLOG = logging.getLogger(\"gold_signal_bot\")",
        "test": "tests.test_signal_only_contract.SignalOnlyArchitectureTests.test_runtime_has_no_exchange_sdk_or_order_execution_calls",
    },
    {
        "label": "impossible lifecycle OHLC range accepted",
        "path": "gold_signal/lifecycle.py",
        "old": "if min(stop, tp1, tp2, high, low) <= 0 or high < low:",
        "new": "if False:",
        "test": "tests.test_signal_lifecycle.SignalLifecycleTests.test_lifecycle_rejects_impossible_ohlc_range",
    },
    {
        "label": "lifecycle stop-first rule disabled",
        "path": "gold_signal/lifecycle.py",
        "old": "if stop_hit:",
        "new": "if False and stop_hit:",
        "test": "tests.test_signal_lifecycle.SignalLifecycleTests.test_stop_first_when_stop_and_target_touch_same_candle",
    },
    {
        "label": "TP1 lifecycle notification disabled",
        "path": "gold_signal/lifecycle.py",
        "old": "if tp1_hit and not already_tp1:",
        "new": "if False and tp1_hit and not already_tp1:",
        "test": "tests.test_signal_lifecycle.SignalLifecycleTests.test_tp1_notifies_half_position_and_keeps_signal_active",
    },
    {
        "label": "TP2 lifecycle notification disabled",
        "path": "gold_signal/lifecycle.py",
        "old": "if tp2_hit:",
        "new": "if False and tp2_hit:",
        "test": "tests.test_signal_lifecycle.SignalLifecycleTests.test_tp2_on_one_candle_sends_tp1_then_tp2_in_order",
    },
    {
        "label": "Telegram connectivity test accepts missing message ID",
        "path": "gold_signal/cli.py",
        "old": "            if type(message_id) is not int or message_id <= 0:\\n                raise RuntimeError(\"Telegram test returned success without a valid message_id\")",
        "new": "            if False:\\n                raise RuntimeError(\"Telegram test returned success without a valid message_id\")",
        "test": "tests.test_telegram_cli.TelegramCliTests.test_connectivity_test_fails_if_telegram_response_has_no_message_id",
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
    shutil.copytree(ROOT / "tests", destination / "tests")


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
