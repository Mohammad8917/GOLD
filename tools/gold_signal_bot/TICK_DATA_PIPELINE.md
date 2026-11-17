# One-year XAU/USD tick conversion

This stage converts the locally verified tick CSV into derived UTC OHLC data.
It is deliberately separate from the Twelve Data live market-data adapter and
does not feed the live signal service or the existing Twelve Data backtest.

## Input contract

- CSV columns, in order: \`timestamp,askPrice,bidPrice\`
- Timestamp: Unix milliseconds
- Input order: strictly increasing; duplicate or out-of-order timestamps fail
- Prices: finite, positive, and ask must be greater than or equal to bid
- The raw file is read only; this tool never edits it

## Conversion policy

- Candle boundaries are epoch-aligned UTC intervals with half-open windows
  \`[start, end)\`.
- One streaming pass creates 1min, 5min, 15min and 1h files.
- Each output bar retains bid OHLC, ask OHLC, and midpoint OHLC. Midpoint is
  calculated per tick before OHLC aggregation; it is not an average of bid/ask
  candle highs or lows.
- No price basis is selected for strategy use in this stage.
- No wide-spread ticks are filtered. Each bar records spread mean/max and tick
  counts above 5 and 10 price units.
- Empty intervals are not synthesized. Missing bucket counts, within-bar
  tick-gap counts above 1s, 5s and 60s, and dataset-wide inter-tick gap metrics
  (including gaps crossing candle boundaries) are reported. First/last tick
  timestamps are included in the report.
- Conversion fails closed on malformed records, crossed/non-positive prices,
  duplicate/out-of-order timestamps, or optional date-bound violations.
- Existing outputs are not overwritten unless \`--overwrite\` is explicitly set.

## Windows PowerShell command

Run from the root of the GOLD repository after the converter PR is reviewed
and approved. The raw CSV remains in its existing location.

\`\`\`powershell
py -3.13 tools\\gold_signal_bot\\convert_ticks_to_ohlc.py --input "data\\historical\\raw\\XAUUSD_FULLYEAR_2025-11-14_TO_2026-11-14\\xauusd-tick-2025-11-14-2026-11-14.csv" --output-dir "data\\historical\\derived\\XAUUSD_FULLYEAR_2025-11-14_TO_2026-11-14" --expected-start "2025-11-14T00:00:00Z" --expected-end-exclusive "2026-11-14T00:00:00Z"
\`\`\`

Expected outputs:

- \`xauusd-tick-2025-11-14-2026-11-14_1min_ohlc.csv\`
- \`xauusd-tick-2025-11-14-2026-11-14_5min_ohlc.csv\`
- \`xauusd-tick-2025-11-14-2026-11-14_15min_ohlc.csv\`
- \`xauusd-tick-2025-11-14-2026-11-14_1h_ohlc.csv\`
- \`xauusd-tick-2025-11-14-2026-11-14_conversion_report.json\`

The report records input bytes/record count, bar counts, missing buckets,
spread/gap metrics, and explicit non-approval for strategy/backtest use.

## What this stage does not prove

The converter does not independently prove source authenticity or full market
session completeness. It does not repair gaps, remove anomalies, infer a
holiday calendar, or establish equivalence with Twelve Data candles. It does
not connect to the strategy or modify backtest execution assumptions.

Before integration, review the generated report and derived files, compare
overlapping OHLC bars with Twelve Data, inspect spread/gap anomalies, and define
a realistic bid/ask-aware backtest execution model. Keep the raw input and its
verified SHA-256 unchanged.
