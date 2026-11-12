# Windows 11 staging stability test (24–72 hours)

این آزمون باید روی همان Windows 11 اجرا شود که میزبان ربات خواهد بود؛ CI ابری جایگزین آن نیست. ربات فقط داده XAU/USD را از Twelve Data می‌خواند و هشدار تلگرام می‌فرستد؛ هیچ سفارش یا معامله‌ای اجرا نمی‌کند.

## 1) نصب و آماده‌سازی

از ریشه مخزن در PowerShell:

```powershell
py -3.11 --version
py -3.11 -m venv .venv-gold
.\.venv-gold\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r tools\gold_signal_bot\requirements.txt
Copy-Item .env.example .env
notepad .env
```

اگر `.env` از قبل وجود دارد، آن را overwrite نکن؛ دستی ویرایش کن. مقادیر واقعی را فقط در `.env` محلی بگذار:

```dotenv
GOLD_TWELVE_DATA_API_KEY=...
GOLD_TELEGRAM_BOT_TOKEN=...
GOLD_TELEGRAM_CHAT_ID=...
GOLD_TIMEFRAME=15min
GOLD_POLL_SECONDS=45
GOLD_LOG_LEVEL=INFO
```

Python 3.11 برای اولین اجرای محلی پیشنهاد می‌شود؛ CI نسخه‌های 3.11، 3.12، 3.13 و 3.14 را روی Windows و Ubuntu آزموده است. مطمئن شو فایل محرمانه Git-track نشده:

```powershell
git check-ignore .env
git status --short
```

## 2) پیش‌آزمون‌ها

با محیط مجازی فعال:

```powershell
python -m unittest discover -s tools\gold_signal_bot -p "test_*.py" -v
python tools\gold_signal_bot\mutation_audit.py
python tools\gold_signal_bot\gold_signal_bot.py --test-telegram
python tools\gold_signal_bot\gold_signal_bot.py --once
```

همه دستورات باید بدون خطا تمام شوند و پیام تست تلگرام باید دریافت شود. `--once` ممکن است به‌علت نبود سیگنال معتبر پیامی نفرستد؛ این به‌تنهایی خطا نیست. تا رفع خطاهای پیش‌آزمون، آزمون ۷۲ ساعته را شروع نکن.

## 3) فایل‌هایی که باید مانیتور شوند

مسیر پیش‌فرض state (مگر اینکه `GOLD_SIGNAL_STATE_FILE` سفارشی کرده باشی):

- Cursor/state: `$HOME\.quantdinger_gold_signal_state.json`
- Delivery WAL: `$HOME\.quantdinger_gold_signal_state.json.outbox.json`
- Lock sidecar: `$HOME\.quantdinger_gold_signal_state.json.lock`؛ باقی‌ماندن فایل پس از خروج طبیعی است، چون قفل توسط سیستم‌عامل مدیریت می‌شود.
- CSV مانیتور: `reports\stability\stability-72h-YYYYMMDD-HHMMSS.csv`
- Log ربات: به‌صورت پیش‌فرض فایل جداگانه ساخته نمی‌شود؛ INFO/WARNING/ERROR در پنجره PowerShell چاپ می‌شود. در مرحله بعد آن را در `bot-72h.log` ذخیره می‌کنیم.

در WAL وضعیت‌های `PENDING`, `SENDING`, `UNKNOWN`, `FAILED`, `SENT` ثبت می‌شوند. `UNKNOWN` نتیجه مبهم ارسال است؛ recovery ممکن است پیام را با برچسب `[RECOVERY RETRY]` دوباره بفرستد. Exactly-once تضمین نمی‌شود و پیام تکراری ممکن است.

## 4) اجرای آزمون ۷۲ ساعته

PowerShell شماره ۱ (ربات)، از ریشه مخزن:

```powershell
New-Item -ItemType Directory -Force .\reports\stability | Out-Null
.\.venv-gold\Scripts\Activate.ps1
python -u tools\gold_signal_bot\gold_signal_bot.py 2>&1 | Tee-Object -FilePath .\reports\stability\bot-72h.log -Append
```

پنجره را باز نگه دار. باید پیام شروع با XAU/USD و signal-only mode دیده شود. فقط یک نمونه ربات اجرا کن.

PowerShell شماره ۲ (مانیتور)، از ریشه مخزن:

```powershell
.\.venv-gold\Scripts\Activate.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\gold_signal_bot\stability_monitor.ps1 -Hours 72 -IntervalSeconds 60
```

برای ۲۴ یا ۴۸ ساعت، مقدار `-Hours` را عوض کن. برای مسیر state سفارشی، همان مسیر را به `-StateFile "C:\path\gold-state.json"` بده. مانیتور process، حافظه، JSONهای state/WAL و تعداد رکوردهای تحویل را هر ۶۰ ثانیه ثبت می‌کند؛ اگر بیش از یک process پیدا کند، process متوقف شود یا JSON خراب باشد، آزمون fail می‌شود.

## 5) معیارهای پایش و قبولی

هر چند ساعت این موارد را بررسی کن:

1. پنجره ربات و `bot-72h.log`: خطاهای شبکه ممکن است با پیام retry در چرخه بعدی بازیابی شوند؛ خطاهای مداوم، history/missed-candle یا Telegram unresolved قابل قبول نیستند.
2. State JSON: باید قابل parse باشد و `last_processed_candle` با ورود کندل‌های بسته جدید جلو برود. در ساعات بسته بودن بازار ممکن است تغییر نکند.
3. Outbox/WAL JSON: باید قابل parse باشد. رکورد `SENT` باید `telegram_message_id` معتبر داشته باشد. در پایان هیچ رکورد حل‌نشده `PENDING/SENDING/UNKNOWN/FAILED` باقی نماند.
4. CSV مانیتور: همه ردیف‌ها باید `ProcessAlive=True`, `StateJsonValid=True`, `OutboxJsonValid=True` داشته باشند. حافظه نباید رشد پیوسته و بی‌توضیح داشته باشد.
5. پیام‌های تلگرام را با زمان و وضعیت ثبت‌شده در WAL تطبیق بده. نبود سیگنال در بازار کم‌تحرک به‌تنهایی شکست نیست.

فرمان‌های خواندن وضعیت:

```powershell
Get-Process python | Select-Object Id, StartTime, WorkingSet64, PrivateMemorySize64
Get-Content "$HOME\.quantdinger_gold_signal_state.json" -Raw | ConvertFrom-Json
Get-Content "$HOME\.quantdinger_gold_signal_state.json.outbox.json" -Raw | ConvertFrom-Json
Get-Content .\reports\stability\bot-72h.log -Tail 100
```

در پایان ۷۲ ساعت، مانیتور باید بدون exception تکمیل شود؛ سپس Ctrl+C را در پنجره ربات بزن. CSV، log و state/WAL را محلی نگه دار و پیش از اشتراک‌گذاری هر خروجی، اطلاعات محرمانه را حذف کن.

## 6) شکست/توقف فوری

اگر process ناخواسته بسته شد، JSON خراب شد، cursor بدون دلیل عقب رفت، history/missed-candle error تکرار شد، ارسال تلگرام پیوسته fail شد یا رکوردهای delivery حل‌نشده باقی ماندند، آزمون را متوقف و علت را بررسی کن. فایل `.lock` باقی‌مانده به‌تنهایی نشانه قفل گیرکرده نیست.

این آزمون پایداری در بازه مشاهده‌شده را ارزیابی می‌کند؛ سودآوری، کیفیت قیمت قابل معامله نزد کارگزار، slippage، uptime آینده یا exactly-once delivery را ثابت نمی‌کند. هیچ کلید صرافی/کارگزاری یا سفارش‌گذاری اضافه نکن.
