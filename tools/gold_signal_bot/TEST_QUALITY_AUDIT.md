# GOLD notifier — test quality and mutation audit

Audit date: 2026-11-11  
Branch: `feature/gold-mutation-quality`

## Scope and interpretation

The ordinary suite contains 66 discovered tests. The mutation audit below deliberately targets ten high-risk regressions across signal decisions, candle validation, retry timing, and process locking. A mutant is considered killed only when the named focused test fails against an isolated temporary copy of the source. Mutation execution does not edit the checked-out repository files.

**Important:** 10/10 selected mutants killed is not a 100% mutation score for the whole project. The other tests have not each been subjected to an exhaustive mutation set. Synthetic deterministic candles are suitable for repeatable unit tests, but they are not a substitute for historical market-data validation.

## Seven-question review of critical tests

| Test | 1. Capability | 2. Input realism | 3. Assertion precision | 4. Mutant killed | 5. Mock dependence | 6. BUY/SELL coverage | 7. Error hidden? | Decision |
|---|---|---|---|---|---|---|---|---|
| `test_detects_buy_setup` | BUY trend + EMA20 cross + RSI gate + complete signal contract | Deterministic XAU/USD-like price path; synthetic, not historical | Exact side, symbol, entry/time, EMA/RSI/ATR reference values, SL/TP geometry and all keys | Yes: cross inversion and EMA trend inversion | None in strategy calculation | Explicit BUY | No broad exception handling | Keep; add historical fixture later |
| `test_detects_sell_setup` | SELL trend + EMA20 cross + RSI gate + complete signal contract | Deterministic XAU/USD-like price path; synthetic, not historical | Exact side, symbol, entry/time, EMA/RSI/ATR values and risk/target geometry | Yes: SELL cross and EMA trend inversions | None in strategy calculation | Explicit SELL | No broad exception handling | Keep; add historical fixture later |
| `test_rejects_buy_cross_when_rsi_exceeds_upper_bound` | Reject BUY-shaped cross when RSI is above 70 | Existing price path with controlled final-candle shock; deterministic synthetic | Must return exactly `None` | Yes: broadening BUY RSI filter to 0..100 | None | Negative BUY | No broad exception handling | Keep |
| `test_rejects_sell_cross_when_rsi_below_lower_bound` | Reject SELL-shaped cross when RSI is below 30 | Existing price path with controlled final-candle shock; deterministic synthetic | Must return exactly `None` | Yes: broadening SELL RSI filter to 0..100 | None | Negative SELL | No broad exception handling | Keep |
| `test_rejects_invalid_ohlc_relationship` | Reject candle whose high is below open | 65-bar price series with one deliberately invalid OHLC candle | Requires RuntimeError containing OHLC | Yes: removal of high invariant | None | Not applicable | Exception is asserted, not swallowed | Keep |
| `test_rejects_stale_data_during_open_session` | Reject old latest candle during open session | 65 bars and explicit fixed UTC now, eight intervals old | Requires RuntimeError containing stale | Yes: stale check disabled | None | Not applicable | Exception is asserted, not swallowed | Keep |
| `test_retries_rate_limit_using_retry_after` | Honor HTTP 429 Retry-After delay and retry | Deterministic HTTP 429 then HTTP 200; Retry-After=0 | Two GETs, exact returned response, exactly one sleep(0.0) | Yes: Retry-After ignored | HTTP and sleep mocked; business retry decision is exercised | Not applicable | No broad exception handling | Keep; add HTTP-date Retry-After case |
| `test_process_lock_rejects_second_instance` | Block a second OS process from owning the same lock | Separate spawned OS process and pipe | Child must report blocked and exit cleanly | Yes: OS lock acquisition removed | No lock mock; actual OS locking is exercised | Not applicable | Child failure/hang is surfaced | Keep |

## Mutation results

| Mutation | Focused test | Ubuntu | Windows |
|---|---|---:|---:|
| Invert BUY close/EMA20 cross | `test_detects_buy_setup` | Killed | Killed |
| Invert BUY EMA20/EMA50 ordering | `test_detects_buy_setup` | Killed | Killed |
| Remove BUY RSI upper bound | `test_rejects_buy_cross_when_rsi_exceeds_upper_bound` | Killed | Killed |
| Invert SELL close/EMA20 cross | `test_detects_sell_setup` | Killed | Killed |
| Invert SELL EMA20/EMA50 ordering | `test_detects_sell_setup` | Killed | Killed |
| Remove SELL RSI lower bound | `test_rejects_sell_cross_when_rsi_below_lower_bound` | Killed | Killed |
| Remove OHLC high invariant | `test_rejects_invalid_ohlc_relationship` | Killed | Killed |
| Disable stale-candle rejection | `test_rejects_stale_data_during_open_session` | Killed | Killed |
| Ignore Retry-After | `test_retries_rate_limit_using_retry_after` | Killed | Killed |
| Remove OS lock acquisition | `test_process_lock_rejects_second_instance` | Killed | Killed |
| **Total** | **10 deliberate mutants** | **10/10 killed** | **10/10 killed** |

CI run: `43007193959`  
Both platforms also discovered and passed all **66** ordinary tests. The mutation runner applies changes only inside temporary directories.

## Findings and remaining gaps

1. The BUY and SELL tests were previously weak: they asserted only non-null and side. They now assert all signal fields, numerical indicator references, risk geometry, target ordering, and candle identity.
2. There were no explicit negative tests for an otherwise valid cross with RSI outside the permitted band. Two deterministic negative tests were added and both RSI-filter mutants are killed.
3. The main guard in `test_gold_signal_bot.py` was located before later test classes. Direct execution could invoke unittest before those classes were defined. It has been moved to the end.
4. Indicator test names were clarified to reflect exact reference-value assertions.
5. Provider and Telegram tests use mocked HTTP boundaries intentionally. They validate our adapter behavior, not Twelve Data or Telegram's live service contract.
6. Most market fixtures remain synthetic. A later stage should add versioned, timestamped historical XAU/USD fixtures and independent indicator reference calculations.
7. Boundary tests for exact RSI 30/50/70, EMA equality, provider clock/DST behavior, malformed state recovery, crash-during-atomic-write, and more retry response variants remain to be designed.
8. The selected mutation set is intentionally small and high-value. Expand it to all critical branches and all test modules before calling the full suite mutation-adequate.

## Git and safety notes

- Mutation runner copies source into a temporary directory; it never mutates the repository checkout.
- No API credentials, broker/exchange connection, order execution, or real Telegram message is used.
- The audit branch is separate; no base branch or open architecture PR is merged.
- CI checkout cleanliness does not reveal the user's local Windows working-tree status. Verify locally with `git status --short` before any commit.
