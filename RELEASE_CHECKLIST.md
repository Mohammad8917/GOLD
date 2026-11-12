# GOLD readiness and release checklist

## Scope and safety boundary

GOLD is an XAU/USD signal notifier for Telegram only. It reads Twelve Data OHLC candles and sends signal/lifecycle notifications. It must never connect to a broker or exchange, place an order, close a real position, or store trading credentials.

## Review chain

The work is intentionally stacked so each stage can be reviewed independently:

1. PR #7 — feature/signal-ttl-parity-warmup → main
   - Live/backtest signal-path parity fixture and comparison.
   - Timeframe-scaled one-day signal TTL and historical warm-up.
2. PR #8 — feature/twelvedata-quota-management → PR #7 branch
   - Twelve Data 429 classification, minute/daily pause, and once-daily 80% quota warning.
3. PR #9 — feature/telegram-signal-lifecycle → PR #8 branch
   - TP1/TP2/SL Telegram lifecycle messages, durable recovery, Windows stability monitor, and manual live-validation workflows.

All three PRs have been merged into `main` in dependency order: PR #7 (222eb8b), PR #8 (761c7d4), and PR #9 (23cf967). The merged code passed the post-merge `main` CI run #165; the follow-up release-checklist documentation commit also passed `main` CI run #166.

## Verified automated checks

| Stage | GitHub Actions | Offline tests per OS | Mutation audit per OS | WAL crash tests |
|---|---|---:|---:|---|
| PR #7 | Signal Bot run #134 | 132 | 12/12 killed | WAL crash run #62 passed on Ubuntu + Windows |
| PR #8 | Signal Bot run #159 | 146 | 15/15 killed | Shared WAL crash coverage from run #62 |
| PR #9 | Signal Bot run #164; post-merge main runs #165 and #166 | 159 | 21/21 killed | Shared WAL crash coverage from run #62 |

Tested heads: PR #7 `1b3e176d15752e67c22ac5e066110cb3d6e43def`; PR #8 `249c771d2f61d816c497507718b78f840654e7e4`; PR #9 `5682e1fb90742381e7422f32c41075c69d8a4e8d`. Each signal-test run passed on Ubuntu and Windows with zero surviving mutations. PR #9's Windows PowerShell parser check for stability_monitor.ps1 also passed. Its static safety contract rejects exchange SDK imports, common order-placement calls, exchange API hosts, and any HTTP POST outside the Telegram sender. WAL crash run #62 executed 3 subprocess crash-boundary tests per OS and passed.

These checks validate offline code paths and selected crash boundaries. They do not prove strategy profitability, provider account entitlement, live Telegram delivery, or long-duration Windows stability.

## Remaining real-environment acceptance steps

### 1. Twelve Data real-feed backtest and parity

- Add a repository Actions secret named GOLD_TWELVE_DATA_API_KEY.
- Run the manual workflow GOLD real-data backtest and parity (manual) from the Actions tab after the workflow is available on the default branch.
- Default backtest window is approximately two years at 1h; choose a realistic cost_per_side.
- Review the JSON/CSV artifact: candle coverage, gaps, trades, net profit, max drawdown, win rate, profit factor, open positions, and holdout results.
- Review the real-feed parity fixture. A mismatch is a stop condition; do not change strategy logic merely to force parity.

### 2. Actual Telegram connectivity

- Add repository Actions secrets GOLD_TELEGRAM_BOT_TOKEN and GOLD_TELEGRAM_CHAT_ID.
- Run GOLD Telegram connectivity test (manual) after it is available on the default branch.
- Confirm that the one test message arrives in the intended chat/channel.
- No Twelve Data key is needed for this test; no trade is executed.

### 3. Windows 11 24–72-hour staging

- Configure the local .env on the Windows 11 host; never commit it.
- Run exactly one bot instance.
- Follow tools/gold_signal_bot/STABILITY_TEST.md and collect the CSV from reports/stability/.
- Require the process to remain alive, state/WAL JSON to remain readable, memory to remain explainably stable, and no delivery/history errors to remain unresolved.
- Review bot logs and actual Telegram messages alongside the monitor CSV.

### 4. Post-merge operational acceptance

- The source code is already merged into `main`; do not treat this as proof of live operational readiness.
- Run the real-data backtest/parity workflow and the Telegram connectivity workflow, then review their artifacts and confirm the Telegram test arrives.
- Complete the 24–72-hour Windows 11 staging run before relying on continuous notifications.
- Keep the notifier in signal-only mode; never add exchange/broker credentials or order execution.

## Secret handling

Real values are deliberately absent from Git. Use local .env for Windows operation and GitHub Actions repository secrets for manual workflows. Never paste API keys, Telegram tokens, or chat IDs into chat, source code, logs, fixtures, or commits. Generated reports/ are gitignored.