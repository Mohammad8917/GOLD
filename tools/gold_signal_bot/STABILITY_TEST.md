# Windows 11 staging stability test

This is a **local staging test**, not a GitHub-hosted CI test. It must run on the Windows 11 machine that will later host the notifier. The bot remains signal-only; do not add exchange or broker credentials.

## Before starting

1. Use the reviewed GOLD version after it has been approved and merged.
2. Install Python 3.11+ and the project requirements as described in the root README.
3. Configure the root `.env` with the Twelve Data key and Telegram bot token/chat ID. Never commit `.env`.
4. Run the bot in one PowerShell window from the repository root:

   ```powershell
   .\.venv-gold\Scripts\Activate.ps1
   python tools\gold_signal_bot\gold_signal_bot.py
   ```

5. Do not launch a second bot instance against the same state file.

## Monitor for 24, 48, or 72 hours

Open a second PowerShell window at the repository root and run:

```powershell
powershell -ExecutionPolicy Bypass -File tools\gold_signal_bot\stability_monitor.ps1 -Hours 24
```

Use `-Hours 48` or `-Hours 72` for longer runs. The monitor discovers the single running `gold_signal_bot.py` process automatically. If more than one is running, stop and resolve that before continuing.

If the bot uses a custom `GOLD_SIGNAL_STATE_FILE`, pass the same path explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File tools\gold_signal_bot\stability_monitor.ps1 -Hours 24 -StateFile "C:\path\to\gold-state.json"
```

The monitor does not stop or restart the bot. When the observation period ends, stop the bot manually with Ctrl+C in its own terminal.

## Evidence to review

The monitor writes a timestamped CSV under `reports\stability\` with:
- process alive status, working-set and private memory, CPU time;
- state/WAL file sizes and JSON readability;
- outbox record count, active signal count, and pending/ambiguous delivery count.

A run is **not accepted** if the bot exits unexpectedly, state/WAL JSON becomes unreadable, memory grows continuously without explanation, delivery remains stuck in PENDING/SENDING/UNKNOWN/FAILED, or logs show repeated missed-candle/history errors. Review the bot logs and Telegram messages alongside the CSV; do not treat a process that merely stayed alive as proof of correct signal generation.

This monitor is diagnostic. It does not prove strategy profitability, real broker fills, exactly-once Telegram delivery, or future reliability. Telegram's sendMessage endpoint has no idempotency key, so a visible recovery duplicate remains possible after an ambiguous send.
