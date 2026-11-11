# GOLD notifier architecture (stage 1)

## Scope and invariants

This package is a standalone XAU/USD **signal notifier**. It reads spot candles
from Twelve Data and may publish an analytical alert to Telegram. It must never
connect to a broker/exchange, hold trading credentials, submit orders, or manage
positions.

The initial EMA20/EMA50 + RSI14 entry filter and ATR14 risk geometry are
intentionally unchanged by this structural refactor.

## Module boundaries

| Module | Responsibility | Allowed dependencies |
|---|---|---|
| `config.py` | Environment configuration and validation | Python standard library |
| `market_data.py` | Twelve Data HTTP, timestamp parsing, OHLC/session validation | config |
| `indicators.py` | EMA, RSI, ATR calculations | Python standard library |
| `strategy.py` | Pure BUY/SELL/none decision and SL/TP geometry | indicators |
| `telegram.py` | Message formatting and Telegram delivery | config, requests |
| `state.py` | Durable de-duplication state and process lock | Python standard library |
| `service.py` | One complete fetch → evaluate → deduplicate → notify cycle | market_data, strategy, state, telegram |
| `cli.py` | Process entry, dotenv loading, arguments, polling and shutdown | config, service, telegram |
| `gold_signal_bot.py` | Backward-compatible script/import facade | package public API |

Dependency direction is inward and explicit: calculation/strategy modules do not
import the CLI or the application service. Importing the compatibility facade
does not load the user's `.env`; environment loading occurs only when the CLI
is invoked.

## Design rules

1. Keep indicator calculations deterministic and free of I/O.
2. Keep strategy decisions independent of HTTP, Telegram, filesystem, and CLI.
3. Keep provider parsing/validation separate from strategy calculations.
4. Keep notification delivery separate from signal decision-making.
5. Persist a candle as delivered only after Telegram confirms success.
6. Do not blindly retry an ambiguous Telegram POST.
7. Preserve the existing `python tools\\gold_signal_bot\\gold_signal_bot.py`
   command and the `gold_signal_bot` import facade during the migration.
8. No exchange/broker client or order-execution abstraction is permitted in this
   signal-only project.

## Test layers

- Unit tests: configuration, parsing, indicators, strategy geometry, state rules.
- Contract tests: module boundaries and compatibility facade.
- Component tests: provider and Telegram adapters with deterministic HTTP fakes.
- Process tests: OS-level lock contention.
- Later stages: historical/out-of-sample strategy validation and Windows
  forward-running. Passing unit tests is not evidence of predictive edge.

## Stage 1 acceptance

- [ ] Responsibilities are separated into focused modules.
- [ ] Legacy command and imports remain compatible.
- [ ] Importing modules has no dotenv/network side effects.
- [ ] Test suite runs without credentials or live external calls.
- [ ] CI checks package imports, compilation, and test discovery without
      enforcing an arbitrary test-method count.
