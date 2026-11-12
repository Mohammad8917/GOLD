# Twelve Data quota handling

The notifier is signal-only and never connects to a broker or submits orders.

## Credentials

Copy the repository-root `.env.example` to `.env` and set the three required values locally:

- `GOLD_TWELVE_DATA_API_KEY`
- `GOLD_TELEGRAM_BOT_TOKEN`
- `GOLD_TELEGRAM_CHAT_ID`

The root `.gitignore` excludes `.env` and explicitly allows only `.env.example`. Never put real values in source files, tests, fixtures, logs, commits, or chat messages.

## Quota behavior

- Normal market-data polling uses the configured Twelve Data XAU/USD time-series endpoint.
- HTTP 429 is treated as quota exhaustion, not as an ordinary network error. The bot queries `GET /api_usage` once after the 429 to inspect `daily_usage` and `plan_daily_limit`. Twelve Data documents that this usage endpoint itself costs one API credit, so it is deliberately not polled on every normal cycle.
- If a 429 occurs while daily usage is at least 95% of the plan's daily limit, continuous mode treats it as a daily-limit event and pauses until just after 00:00 UTC. Otherwise it treats it as a per-minute event and pauses until the next minute boundary (or the provider's `Retry-After` value when the usage endpoint is unavailable).
- If the usage endpoint cannot classify the error, the bot uses the provider's error message as a fallback and otherwise conservatively waits for the next minute. It never rotates keys or accounts.
- HTTP/code 401 and 403 are treated as credential/permission failures, not quota exhaustion. Network timeouts and connection errors retain bounded exponential retry behavior.
- The pause is interruptible with Ctrl+C / SIGTERM. `--once` reports the error and exits rather than sleeping.

## Pre-emptive warning\n\n- The notifier checks `/api_usage` at most once per UTC day during normal operation; this check costs one API credit.\n- At 80% or more of the daily plan limit, it sends one Telegram warning showing used credits, remaining credits, and the 00:00 UTC reset.\n- The check date and pending warning are stored in a separate atomic sidecar file next to the configured state file. A pending warning is retried without re-querying usage; a stale pending warning is discarded after the UTC daily reset.\n\n## Important\n
The daily limit and per-minute limit are account-plan properties, not hard-coded constants. The implementation reads the live usage values returned by Twelve Data. A successful quota check does not guarantee market-data availability or trading performance.
