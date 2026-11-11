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
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from gold_signal.config import TIMEFRAMES
from gold_signal.indicators import atr, ema, rsi

API_URL = "https://api.twelvedata.com/time_series"


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    return result


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
        "exit_sl": sum(t["exit_reason"] == "SL" for t in trades),
        "exit_tp2": sum(t["exit_reason"] == "TP2" for t in trades),
        "methodology": "Entry at signal candle close; 50% exits at TP1 and 50% at TP2; conservative stop-first if SL and target touch in same OHLC bar; one open position; unfinished trades excluded.",
    }


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
        bars = fetch_history(key, args.timeframe, start, end, requests.Session())
        signals = generate_signals(bars, args.atr_multiplier, args.tp1_r, args.tp2_r)
        trades, skipped = simulate(bars, signals, args.cost_per_side)
        report = summarize(trades, signals, skipped, bars, start, end)
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
