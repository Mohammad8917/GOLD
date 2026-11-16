# Historical XAUUSD dataset

This directory is the project-side registration point for the locally held historical tick dataset.

## Dataset manifest

- Instrument: XAUUSD
- Provider declared by the acquisition pipeline: Dukascopy via dukascopy-node
- Data type: tick (timestamp, askPrice, bidPrice)
- Requested interval: 2025-11-14 inclusive to 2026-11-14 exclusive
- Local source repository: `GOLD-HISTORICAL-DATA`
- Source filename: `xauusd-tick-2025-11-14-2026-11-14.csv`
- Expected project-local destination: `data/historical/raw/XAUUSD_FULLYEAR_2025-11-14_TO_2026-11-14/xauusd-tick-2025-11-14-2026-11-14.csv`
- Expected size: `2,957,270,652` bytes
- Expected SHA-256: `6263CE3490B989118D6F45F7CF12098BF75D7B65DF206A6BB4CF02F35097BA67`
- Expected data rows: `92,443,061`
- Columns: `timestamp,askPrice,bidPrice`
- Timestamp unit: Unix milliseconds

## Validation status

- Structural CSV validation: PASSED (92,443,061 valid records; no malformed rows or invalid timestamps/prices in the recorded audit).
- Anomaly review: PENDING.
- Source authenticity: NOT VERIFIED independently.
- Historical completeness: NOT VERIFIED.
- Production/backtest approval: NOT GRANTED.

The audit recorded 40,807 spreads above 5, 4,463 spreads above 10, and 296 gaps longer than 60 seconds. These observations require review; they must not be silently filtered or treated as confirmed market defects.

## Integration boundary

The live signal service currently consumes closed XAU/USD OHLC candles from Twelve Data. This file is bid/ask tick data from a separately declared source. It is **not** a live-feed replacement and must not be fed directly into the current strategy.

The intended next step is a separate, tested tick-to-OHLC conversion pipeline with explicit decisions for price basis (bid/ask/mid), timezone/session calendar, spread-quality labels, gap handling, candle boundaries, and parity checks. Preserve the raw file unchanged.

## Storage and Git policy

Keep the raw CSV outside ordinary Git history. GitHub blocks ordinary Git files above 100 MiB, and the expected file is about 2.75 GiB. Git LFS limits depend on plan; the documented Free/Pro per-file limit is 2 GB, below this dataset's size. Store the raw file in the designated local data directory or suitable object storage, and version only this manifest and small derived metadata in this repository.

The raw dataset is not included in this Git commit. Its presence on a particular machine must be verified by file size and SHA-256 before any processing.
