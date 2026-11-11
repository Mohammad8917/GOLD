"""Historical XAU/USD backtest for the GOLD signal-only strategy.

Uses Twelve Data spot XAU/USD candles, the repository's EMA/RSI/ATR code and
conservative OHLC exit handling. No broker, exchange, or order API is used.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from gold_signal.config import TIMEFRAMES
from gold_signal.indicators import atr, ema, rsi
from gold_signal.market_data import _is_expected_gold_pause

API_URL = "https://api.twelvedata.com/time_series"
WARMUP_BARS = 200


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_history_continuity(bars: list[dict[str, Any]], timeframe: str) -> None:
    """Fail closed on unexplained intraday data gaps before simulating trades."""
    seconds = TIMEFRAMES[timeframe][1]
    for left, right in zip(bars, bars[1:]):
        gap_seconds = (right["time"] - left["time"]).total_seconds()
        if gap_seconds <= seconds * 1.5:
            continue
        if timeframe == "1day":
            # Permit only the ordinary Friday-to-Monday calendar gap.
            expected_pause = left["time"].weekday() == 4 and right["time"].weekday() == 0
        else:
            left_end = left["time"] + timedelta(seconds=seconds)
            expected_pause = _is_expected_gold_pause(left_end, right["time"])
        if not expected_pause:
            raise RuntimeError(
                f"Unexpected historical data gap of {int(gap_seconds)} seconds "
                f"after {left['time'].isoformat()}; refusing to backtest across missing candles."
            )



def validate_test_window_coverage(
    bars: list[dict[str, Any]], start: date, end: date, timeframe: str
) -> None:
    """Reject provider responses that silently cover only part of the requested dates."""
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    interval = TIMEFRAMES[timeframe][1]
    first_test = next((bar for bar in bars if bar["time"] >= start_utc), None)
    if first_test is None:
        raise RuntimeError("No candles were returned inside the requested backtest window.")
    tolerance_days = 7 if timeframe == "1day" else 3
    if first_test["time"] - start_utc > timedelta(days=tolerance_days):
        raise RuntimeError(
            f"Historical data starts too late: first test candle is {first_test['time'].isoformat()}, "
            f"more than {tolerance_days} days after the requested start."
        )
    last_closed_end = bars[-1]["time"] + timedelta(seconds=interval)
    if end_exclusive - last_closed_end > timedelta(days=tolerance_days):
        raise RuntimeError(
            f"Historical data ends too early: last candle closes at {last_closed_end.isoformat()}, "
            f"more than {tolerance_days} days before the requested end."
        )


def fetch_history(api_key: str, timeframe: str, start: date, end: date,
                  session: requests.Session) -> list[dict[str, Any]]:
    """Download bounded date chunks so no request exceeds Twelve Data's 5k cap."""
    seconds = TIMEFRAMES[timeframe][1]
    # Keep each theoretical chunk below 4,000 bars, leaving room for provider
    # session details and avoiding silent truncation at the 5,000-row API cap.
    chunk_days = max(1, int(4000 * seconds / 86400 * 0.70))
    cursor = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    final = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    candles: dict[datetime, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    while cursor < final:
        chunk_start = cursor
        chunk_end = min(final, cursor + timedelta(days=chunk_days))
        params = {
            "symbol": "XAU/USD", "interval": timeframe, "timezone": "UTC",
            "format": "ISO", "order": "asc",
            "start_date": cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            "end_date": chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
            "apikey": api_key,
        }
        response = session.get(API_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") == "error" or payload.get("code"):
            raise RuntimeError(f"Twelve Data error: {str(payload)[:400]}")
        values = payload.get("values")
        if not isinstance(values, list):
            raise RuntimeError(f"No historical values returned: {str(payload)[:300]}")
        if len(values) >= 4000:
            raise RuntimeError("Provider returned at least 4,000 rows for one chunk; reduce chunk size and rerun to avoid silent truncation.")
        for item in values:
            stamp = parse_time(str(item["datetime"]))
            bar = {"time": stamp, **{k: float(item[k]) for k in ("open", "high", "low", "close")}}
            o, h, low, close = (bar[k] for k in ("open", "high", "low", "close"))
            if not all(math.isfinite(v) for v in (o, h, low, close)) or low <= 0 or h < max(o, low, close) or low > min(o, close):
                raise RuntimeError(f"Invalid OHLC candle at {stamp.isoformat()}")
            if chunk_start <= stamp < chunk_end and stamp + timedelta(seconds=seconds) <= now:
                previous = candles.get(stamp)
                if previous is not None and any(previous[k] != bar[k] for k in ("open", "high", "low", "close")):
                    raise RuntimeError(f"Conflicting duplicate candle at {stamp.isoformat()}")
                candles[stamp] = bar
        cursor = chunk_end
        time.sleep(0.15)  # modest pacing across historical requests
    result = [candles[k] for k in sorted(candles)]
    if len(result) < 100:
        raise RuntimeError(f"Only {len(result)} closed candles downloaded; at least 100 are required.")
    validate_history_continuity(result, timeframe)
    validate_test_window_coverage(result, start, end, timeframe)
    return result


def select_test_window_signals(signals: list[dict[str, Any]], start: date) -> list[dict[str, Any]]:
    """Exclude warm-up-period entries while retaining their bars for indicator seeding."""
    start_utc = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    return [signal for signal in signals if signal["signal_time"] >= start_utc]


def generate_signals(bars: list[dict[str, Any]], atr_mult: float,
                     tp1_r: float, tp2_r: float) -> list[dict[str, Any]]:
    closes = [b["close"] for b in bars]
    fast, slow = ema(closes, 20), ema(closes, 50)
    momentum, volatility = rsi(closes, 14), atr(bars, 14)
    found = []
    for i in range(1, len(bars)):
        if any(x is None for x in (fast[i], fast[i-1], slow[i], momentum[i], volatility[i])):
            continue
        side = None
        if closes[i-1] <= fast[i-1] and closes[i] > fast[i] and fast[i] > slow[i] and 50 <= momentum[i] <= 70:
            side = "BUY"
        elif closes[i-1] >= fast[i-1] and closes[i] < fast[i] and fast[i] < slow[i] and 30 <= momentum[i] <= 50:
            side = "SELL"
        if not side:
            continue
        risk = float(volatility[i]) * atr_mult
        if not math.isfinite(risk) or risk <= 0:
            continue
        entry = closes[i]
        sign = 1 if side == "BUY" else -1
        found.append({
            "signal_time": bars[i]["time"], "side": side, "entry": entry,
            "stop": entry - sign * risk, "tp1": entry + sign * risk * tp1_r,
            "tp2": entry + sign * risk * tp2_r, "risk": risk, "signal_index": i,
        })
    return found


def simulate(bars: list[dict[str, Any]], signals: list[dict[str, Any]],
             cost_per_side: float) -> tuple[list[dict[str, Any]], int]:
    """One position at a time; half off at TP1, half at TP2; stop-first ambiguity."""
    trades: list[dict[str, Any]] = []
    busy_until = -1
    skipped_overlap = 0
    for sig in signals:
        idx = sig["signal_index"]
        if idx <= busy_until:
            skipped_overlap += 1
            continue
        sign, risk = (1 if sig["side"] == "BUY" else -1), sig["risk"]
        remaining = 1.0
        # Charge entry once for the full position; charge each partial exit once.
        realized_r = -cost_per_side / risk
        tp1_done = False
        exit_reason = "OPEN_AT_END"
        exit_time = ""
        exit_index = len(bars) - 1
        for j in range(idx + 1, len(bars)):
            bar = bars[j]
            # Entry cost was charged once above; each exit leg pays one exit cost.
            stop_hit = bar["low"] <= sig["stop"] if sign == 1 else bar["high"] >= sig["stop"]
            tp1_hit = bar["high"] >= sig["tp1"] if sign == 1 else bar["low"] <= sig["tp1"]
            tp2_hit = bar["high"] >= sig["tp2"] if sign == 1 else bar["low"] <= sig["tp2"]
            if stop_hit:
                fill = min(bar["open"], sig["stop"]) if sign == 1 else max(bar["open"], sig["stop"])
                realized_r += remaining * (sign * (fill - sig["entry"]) - cost_per_side) / risk
                exit_reason, exit_time, exit_index = "SL", bar["time"].isoformat(), j
                break
            if tp1_hit and not tp1_done:
                # Favorable opening gaps receive the better executable bar-open price.
                tp1_fill = max(bar["open"], sig["tp1"]) if sign == 1 else min(bar["open"], sig["tp1"])
                realized_r += 0.5 * (sign * (tp1_fill - sig["entry"]) / risk - cost_per_side / risk)
                remaining = 0.5
                tp1_done = True
            if tp2_hit:
                fill = max(bar["open"], sig["tp2"]) if sign == 1 else min(bar["open"], sig["tp2"])
                realized_r += remaining * (sign * (fill - sig["entry"]) - cost_per_side) / risk
                exit_reason, exit_time, exit_index = "TP2", bar["time"].isoformat(), j
                break
        if exit_reason == "OPEN_AT_END":
            # Keep the position open through the end; later signals cannot overlap it.
            busy_until = len(bars) - 1
            continue
        trades.append({
            "signal_time": sig["signal_time"].isoformat(), "side": sig["side"],
            "entry": sig["entry"], "stop": sig["stop"], "tp1": sig["tp1"], "tp2": sig["tp2"],
            "exit_time": exit_time, "exit_reason": exit_reason,
            "net_r": round(realized_r, 6), "bars_held": exit_index - idx,
            "tp1_hit": tp1_done,
        })
        busy_until = exit_index
    return trades, skipped_overlap



def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def monte_carlo_risk_metrics(
    trade_returns: list[float], *, simulations: int = 1000,
    block_size: int = 5, seed: int = 42, minimum_trades: int = 30,
) -> dict[str, Any]:
    """Circularity-free moving-block bootstrap of chronological trade R returns.

    Blocks preserve short runs of adjacent outcomes. Results are a sensitivity
    analysis, not a forecast or evidence of profitability.
    """
    if simulations < 100 or block_size < 1 or minimum_trades < 1:
        raise ValueError("Require at least 100 simulations and positive block/minimum sizes")
    if not all(math.isfinite(value) for value in trade_returns):
        raise ValueError("Trade returns must all be finite")
    count = len(trade_returns)
    if count < minimum_trades:
        return {
            "status": "insufficient_sample", "trade_count": count,
            "minimum_trades": minimum_trades,
            "note": "Monte Carlo results withheld because the completed-trade sample is too small.",
        }
    rng = random.Random(seed)
    net_results: list[float] = []
    drawdowns: list[float] = []
    block = min(block_size, count)
    for _ in range(simulations):
        sampled: list[float] = []
        while len(sampled) < count:
            take = min(block, count - len(sampled))
            start_index = rng.randrange(0, count - take + 1)
            sampled.extend(trade_returns[start_index:start_index + take])
        equity = peak = max_drawdown = 0.0
        for value in sampled:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        net_results.append(equity)
        drawdowns.append(max_drawdown)
    return {
        "status": "computed", "method": "moving_block_bootstrap",
        "trade_count": count, "simulations": simulations,
        "block_size_trades": block, "seed": seed,
        "probability_of_net_loss_percent": round(
            100.0 * sum(value < 0 for value in net_results) / simulations, 2
        ),
        "net_r_percentiles": {
            "p05": round(_percentile(net_results, 0.05), 4),
            "p50": round(_percentile(net_results, 0.50), 4),
            "p95": round(_percentile(net_results, 0.95), 4),
        },
        "max_drawdown_r_percentiles": {
            "p50": round(_percentile(drawdowns, 0.50), 4),
            "p95": round(_percentile(drawdowns, 0.95), 4),
        },
        "note": "Resampling sensitivity only; not a forecast, not out-of-sample validation, and not proof of a trading edge.",
    }


def summarize(trades: list[dict[str, Any]], signals: list[dict[str, Any]],
              skipped: int, bars: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    returns = [float(t["net_r"]) for t in trades]
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equity = peak = max_dd = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    gaps = 0
    # Report data discontinuities; weekends/market closures are not automatically errors.
    if len(bars) > 1:
        seconds = TIMEFRAMES[os.getenv("GOLD_TIMEFRAME", "15min")][1]
        gaps = sum((b["time"] - a["time"]).total_seconds() > seconds * 1.5
                   for a, b in zip(bars, bars[1:]))
    return {
        "symbol": "XAU/USD", "timeframe": os.getenv("GOLD_TIMEFRAME", "15min"),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "first_candle": bars[0]["time"].isoformat(), "last_candle": bars[-1]["time"].isoformat(),
        "candles": len(bars), "data_time_gaps_over_1_5_intervals": gaps,
        "signals_generated": len(signals), "completed_trades": len(trades),
        "signals_skipped_while_position_open": skipped,
        "unfinished_positions_excluded": len(signals) - len(trades) - skipped,
        "wins": len(wins), "losses": len(losses),
        "win_rate_percent": round(100 * len(wins) / len(trades), 2) if trades else 0.0,
        "net_profit_r": round(sum(returns), 4),
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else (None if not gross_win else "infinite"),
        "average_trade_r": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "max_drawdown_r": round(max_dd, 4),
        "monte_carlo_block_bootstrap": monte_carlo_risk_metrics(returns),
        "exit_sl": sum(t["exit_reason"] == "SL" for t in trades),
        "exit_tp2": sum(t["exit_reason"] == "TP2" for t in trades),
        "methodology": "Entry at signal candle close; 50% exits at TP1 and 50% at TP2; conservative stop-first if SL and target touch in same OHLC bar; one open position; unfinished trades excluded.",
    }



def evaluate_chronological_holdout(
    bars: list[dict[str, Any]], signals: list[dict[str, Any]],
    cost_per_side: float, *, development_fraction: float = 0.70,
    minimum_bars: int = 100,
) -> dict[str, Any]:
    """Evaluate fixed rules on a chronological development/untouched holdout split."""
    if not 0.5 <= development_fraction < 0.9:
        raise ValueError("development_fraction must be between 0.5 and 0.9")
    test_bars = bars
    if len(test_bars) < minimum_bars:
        return {
            "status": "insufficient_history", "test_candles": len(test_bars),
            "minimum_candles": minimum_bars,
        }
    split_index = int(len(test_bars) * development_fraction)
    if split_index <= 0 or split_index >= len(test_bars):
        return {"status": "invalid_split", "test_candles": len(test_bars)}
    windows = (
        ("development", test_bars[:split_index], signals),
        ("holdout", test_bars[split_index:], signals),
    )
    output: dict[str, Any] = {
        "status": "computed", "method": "chronological_fixed-rule_holdout",
        "development_fraction": development_fraction,
        "split_time": test_bars[split_index]["time"].isoformat(),
        "warning": (
            "The holdout is only independent if it is not used to tune rules or parameters. "
            "Each segment is simulated independently; positions still open at a segment boundary are excluded. "
            "This is a fixed-rule chronological holdout, not walk-forward optimization."
        ),
    }
    for name, window_bars, _ in windows:
        start_time, end_time = window_bars[0]["time"], window_bars[-1]["time"]
        index_by_time = {bar["time"]: index for index, bar in enumerate(window_bars)}
        window_signals = []
        for signal in signals:
            signal_time = signal["signal_time"]
            if signal_time in index_by_time:
                window_signals.append({**signal, "signal_index": index_by_time[signal_time]})
        trades, skipped = simulate(window_bars, window_signals, cost_per_side)
        output[name] = summarize(
            trades, window_signals, skipped, window_bars,
            start_time.date(), end_time.date(),
        )
    return output


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    parser = argparse.ArgumentParser(description="Historical XAU/USD backtest; no trading execution.")
    parser.add_argument("--start", required=True, help="UTC start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="UTC end date YYYY-MM-DD")
    parser.add_argument("--timeframe", default=os.getenv("GOLD_TIMEFRAME", "15min"), choices=sorted(TIMEFRAMES))
    parser.add_argument("--atr-multiplier", type=float, default=float(os.getenv("GOLD_RISK_ATR_MULTIPLIER", "1.5")))
    parser.add_argument("--tp1-r", type=float, default=float(os.getenv("GOLD_TP1_R", "1.0")))
    parser.add_argument("--tp2-r", type=float, default=float(os.getenv("GOLD_TP2_R", "2.0")))
    parser.add_argument("--cost-per-side", type=float, default=0.0, help="Estimated spread/slippage cost in XAU/USD price units per side; 0 means costs omitted.")
    parser.add_argument("--out", default="gold_backtest")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    numeric = (args.atr_multiplier, args.tp1_r, args.tp2_r, args.cost_per_side)
    if (end < start or not all(math.isfinite(v) for v in numeric)
            or args.atr_multiplier <= 0 or args.tp1_r <= 0
            or args.tp2_r <= args.tp1_r or args.cost_per_side < 0):
        parser.error("Require end >= start, finite positive ATR/TP1, TP2 > TP1, and finite nonnegative costs.")
    key = os.getenv("GOLD_TWELVE_DATA_API_KEY", "").strip()
    if not key:
        parser.error("Set GOLD_TWELVE_DATA_API_KEY in the project-root .env; do not paste it into chat or commit it.")
    os.environ["GOLD_TIMEFRAME"] = args.timeframe
    try:
        warmup_start = start - timedelta(seconds=WARMUP_BARS * TIMEFRAMES[args.timeframe][1])
        bars = fetch_history(key, args.timeframe, warmup_start, end, requests.Session())
        start_utc = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        warmup_count = sum(bar["time"] < start_utc for bar in bars)
        if warmup_count < WARMUP_BARS:
            raise RuntimeError(
                f"Only {warmup_count} of {WARMUP_BARS} required warm-up candles are available before {start.isoformat()}; "
                "choose a later test start or a provider plan with deeper history."
            )
        all_signals = generate_signals(bars, args.atr_multiplier, args.tp1_r, args.tp2_r)
        signals = select_test_window_signals(all_signals, start)
        trades, skipped = simulate(bars, signals, args.cost_per_side)
        report = summarize(trades, signals, skipped, bars, start, end)
        report["warmup_bars_requested"] = WARMUP_BARS
        report["warmup_candles_downloaded"] = warmup_count
        report["first_test_candle"] = next(
            (bar["time"].isoformat() for bar in bars if bar["time"] >= start_utc),
            None,
        )
        test_start = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        test_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        test_window_bars = [bar for bar in bars if test_start <= bar["time"] < test_end]
        report["chronological_holdout"] = evaluate_chronological_holdout(
            test_window_bars, signals, args.cost_per_side
        )
        report["estimated_cost_per_side_price_units"] = args.cost_per_side
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        with out.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as handle:
            fields = list(trades[0]) if trades else ["signal_time", "side", "entry", "stop", "tp1", "tp2", "exit_time", "exit_reason", "net_r", "bars_held", "tp1_hit"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(trades)
        print(json.dumps(report, indent=2))
        print(f"Saved: {out.with_suffix('.json')} and {out.with_suffix('.csv')}")
        return 0
    except Exception as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
