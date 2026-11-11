# Historical XAU/USD backtest

This runner evaluates the current GOLD signal rules against historical **spot XAU/USD** OHLC candles from Twelve Data. It does not connect to a broker/exchange, place orders, or send Telegram messages.

## Run on Windows 11

From the repository root, activate the project's virtual environment and install its existing requirements:

```powershell
..venv-gold\Scripts\Activate.ps1
pip install -r tools\gold_signal_bot\requirements.txt
```

Add your Twelve Data key to the root `.env` file (never commit or share it):

```dotenv
GOLD_TWELVE_DATA_API_KEY=your_private_key
GOLD_TIMEFRAME=15min
GOLD_RISK_ATR_MULTIPLIER=1.5
GOLD_TP1_R=1.0
GOLD_TP2_R=2.0
```

Run a one-year 15-minute test (adjust the dates to the period you want):

```powershell
python tools\gold_signal_bot\backtest_historical.py --start 2025-11-11 --end 2026-11-11 --timeframe 15min --out reports\xauusd_1y
```

Optional cost model: pass `--cost-per-side 0.20` to deduct an estimated 0.20 XAU/USD price units per side (entry and exit). This is only an example; use a realistic spread/slippage estimate for the actual broker/price feed you intend to compare against. Default zero costs are optimistic and must not be presented as live performance.

The script downloads date-bounded chunks plus 200 pre-test warm-up candles for indicator seeding, sorts/deduplicates candles, discards still-open candles, and refuses to simulate across unexplained intraday data gaps. If 200 warm-up candles are unavailable, it stops rather than calculating indicators from an incomplete seed. It also requires the first and last returned candles to be reasonably close to the requested test-window boundaries. Expected weekend/maintenance closures are treated separately; daily bars may naturally skip weekends. It saves:
- `reports\xauusd_1y.json`: aggregate metrics and methodology.
- `reports\xauusd_1y.csv`: completed trade-by-trade results.

## Backtest assumptions and limitations

- The 200 candles before the requested start date are used only to warm up EMA/RSI/ATR; signals before the requested test window are excluded.
- Entry is the signal candle's closing price; this is a bar-close model, not a guaranteed executable fill.
- The strategy uses EMA20/EMA50, RSI14, ATR14, ATR stop, TP1=1R and TP2=2R defaults.
- One position at a time. A signal while a position is open is counted as skipped.
- Half the position is taken off at TP1 and half at TP2. If stop and target are both touched within one OHLC candle, stop is assumed first (conservative ambiguity handling).
- Gap-through stop/target fills use the candle open when it is worse than the level.
- Positions still open at the end are excluded from completed-trade win rate and P/L.
- No overnight financing, commission, spread, variable slippage, latency, or broker-specific execution is modeled unless a simple fixed per-side cost is supplied.
- The feed is Twelve Data's XAU/USD spot series. Results can differ from a broker's CFD/spot quote. Data coverage, account entitlement, and historical depth depend on the provider plan.
- This is an in-sample historical simulation, not proof of future profitability. A robust evaluation should reserve a later out-of-sample period and run a live paper forward-test.

The report includes raw candle count, data gaps, signal count, completed trades, overlap-skipped signals, win rate, net R, profit factor, average R, drawdown in R, exit counts, and a moving-block-bootstrap Monte Carlo sensitivity summary. Monte Carlo output is withheld below 30 completed trades and is not a forecast or proof of edge. An unexplained intraday gap stops the run rather than silently allowing a position to cross missing price action. Review expected session gaps and compare the CSV against the chart before trusting the metrics.
