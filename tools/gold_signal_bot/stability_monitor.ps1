[CmdletBinding()]
param(
    [ValidateSet(1, 2, 4, 24, 48, 72)]
    [int]$Hours = 24,

    [ValidateRange(10, 3600)]
    [int]$IntervalSeconds = 60,

    [int]$ProcessId = 0,

    [string]$StateFile = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:ConfiguredStateFile = $env:GOLD_SIGNAL_STATE_FILE

$script:TelegramBotToken = $env:GOLD_TELEGRAM_BOT_TOKEN
$script:TelegramChatId = $env:GOLD_TELEGRAM_CHAT_ID
$envFile = Join-Path $repoRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*GOLD_TELEGRAM_BOT_TOKEN\s*=\s*(.*?)\s*$') {
            if (-not $script:TelegramBotToken) { $script:TelegramBotToken = $Matches[1].Trim().Trim('"').Trim("'") }
        }
        elseif ($line -match '^\s*GOLD_TELEGRAM_CHAT_ID\s*=\s*(.*?)\s*$') {
            if (-not $script:TelegramChatId) { $script:TelegramChatId = $Matches[1].Trim().Trim('"').Trim("'") }
        }
        elseif ($line -match '^\s*GOLD_SIGNAL_STATE_FILE\s*=\s*(.*?)\s*$') {
            if (-not $script:ConfiguredStateFile) { $script:ConfiguredStateFile = $Matches[1].Trim().Trim('"').Trim("'") }
        }
    }
}

if (-not $StateFile) {
    if ($script:ConfiguredStateFile) {
        $StateFile = $script:ConfiguredStateFile
    }
    else {
        $StateFile = Join-Path $HOME ".quantdinger_gold_signal_state.json"
    }
}
if (-not [System.IO.Path]::IsPathRooted($StateFile)) {
    $StateFile = Join-Path $repoRoot $StateFile
}
$StateFile = [System.IO.Path]::GetFullPath($StateFile)

function Send-WatchdogTelegramAlert {
    param([Parameter(Mandatory = $true)][string]$Message)

    if (-not $script:TelegramBotToken -or -not $script:TelegramChatId) {
        Write-Warning "Independent Telegram alert skipped: bot token or chat ID is unavailable."
        return $false
    }
    try {
        $uri = "https://api.telegram.org/bot$($script:TelegramBotToken)/sendMessage"
        $null = Invoke-RestMethod -Uri $uri -Method Post -Body @{
            chat_id = $script:TelegramChatId
            text = $Message
            disable_notification = "false"
        } -TimeoutSec 10
        return $true
    }
    catch {
        # Do not print the exception: it may contain the bot-token URL.
        Write-Warning "Independent Telegram alert failed; see local PowerShell/network diagnostics."
        return $false
    }
}

$restartFailureTimes = New-Object 'System.Collections.Generic.List[datetime]'
$restartDelays = @(5, 10, 30, 60)
$restartNumber = 0
$watchdogStopped = $false
$reportDirectory = Join-Path $repoRoot "reports\stability"
New-Item -ItemType Directory -Force -Path $reportDirectory | Out-Null
$csvPath = Join-Path $reportDirectory ("stability-{0}h-{1}.csv" -f $Hours, (Get-Date -Format "yyyyMMdd-HHmmss"))

if ($ProcessId -le 0) {
    $candidates = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -match "^python(w)?\.exe$" -and
                $_.CommandLine -match "gold_signal_bot\.py"
            }
    )
    if ($candidates.Count -ne 1) {
        throw "Expected exactly one running GOLD bot process; found $($candidates.Count). Start one bot instance, then pass -ProcessId if needed."
    }
    $ProcessId = [int]$candidates[0].ProcessId
}

$process = Get-Process -Id $ProcessId -ErrorAction Stop
$startedAt = Get-Date
$deadline = $startedAt.AddHours($Hours)
$outboxPath = "$StateFile.outbox.json"

Write-Host "Monitoring GOLD PID $ProcessId for $Hours hours."
Write-Host "State:  $StateFile"
Write-Host "Outbox: $outboxPath"
Write-Host "CSV:    $csvPath"
Write-Host "Watchdog enabled: bounded restart policy (5/10/30/60 seconds; max 5 failures per 10 minutes)."
$currentStartedAt = $process.StartTime

while ((Get-Date) -lt $deadline) {
    $sampleTime = (Get-Date).ToUniversalTime().ToString("o")
    $alive = $true
    try {
        if ($ProcessId -le 0) { throw "No valid GOLD process ID is available after a failed restart." }
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
    }
    catch {
        $alive = $false
        $process = $null
    }

    $stateBytes = 0
    $outboxBytes = 0
    $outboxRecords = 0
    $activeSignals = 0
    $pendingDeliveries = 0
    $stateParseOk = $true
    $outboxParseOk = $true

    if (Test-Path -LiteralPath $StateFile) {
        $stateBytes = (Get-Item -LiteralPath $StateFile).Length
        try {
            Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json | Out-Null
        }
        catch {
            $stateParseOk = $false
        }
    }

    if (Test-Path -LiteralPath $outboxPath) {
        $outboxBytes = (Get-Item -LiteralPath $outboxPath).Length
        try {
            $wal = Get-Content -LiteralPath $outboxPath -Raw | ConvertFrom-Json
            $records = $wal.records
            if ($null -eq $records) {
                $records = $wal
            }
            $entries = @($records.PSObject.Properties | ForEach-Object { $_.Value })
            $outboxRecords = $entries.Count
            foreach ($entry in $entries) {
                if ($entry.status -eq "SENT" -and $entry.lifecycle_status -in @("ACTIVE", "TP1_HIT")) {
                    $activeSignals++
                }
                if ($entry.status -in @("PENDING", "SENDING", "UNKNOWN", "FAILED")) {
                    $pendingDeliveries++
                }
                if ($entry.lifecycle_notice.status -in @("PENDING", "SENDING", "UNKNOWN", "FAILED")) {
                    $pendingDeliveries++
                }
                if (@($entry.pending_lifecycle_events).Count -gt 0) {
                    $pendingDeliveries++
                }
            }
        }
        catch {
            $outboxParseOk = $false
        }
    }

    $row = [pscustomobject]@{
        TimestampUtc = $sampleTime
        ProcessId = $ProcessId
        ProcessAlive = $alive
        WorkingSetMB = if ($process) { [math]::Round($process.WorkingSet64 / 1MB, 2) } else { 0 }
        PrivateMemoryMB = if ($process) { [math]::Round($process.PrivateMemorySize64 / 1MB, 2) } else { 0 }
        CpuSeconds = if ($process) { [math]::Round($process.CPU, 2) } else { 0 }
        StateBytes = $stateBytes
        OutboxBytes = $outboxBytes
        OutboxRecords = $outboxRecords
        ActiveSignals = $activeSignals
        PendingDeliveries = $pendingDeliveries
        StateJsonValid = $stateParseOk
        OutboxJsonValid = $outboxParseOk
    }

    if (Test-Path -LiteralPath $csvPath) {
        $row | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Append -Encoding UTF8
    }
    else {
        $row | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
    }

    if (-not $alive) {
        $failureTime = Get-Date
        if (($failureTime - $currentStartedAt).TotalMinutes -ge 10) {
            $restartFailureTimes.Clear()
            $restartNumber = 0
        }
        while ($restartFailureTimes.Count -gt 0 -and
               ($failureTime - $restartFailureTimes[0]).TotalMinutes -ge 10) {
            $restartFailureTimes.RemoveAt(0)
        }
        $restartFailureTimes.Add($failureTime)

        if ($restartFailureTimes.Count -ge 5) {
            $alert = "🚨 GOLD watchdog STOPPED: 5 process failures within 10 minutes. No further automatic restarts. Check Windows logs and the state/WAL files."
            Send-WatchdogTelegramAlert -Message $alert | Out-Null
            Write-Warning "GOLD watchdog stopped after five failures in ten minutes. No more restarts will be attempted."
            $watchdogStopped = $true
            break
        }

        $delayIndex = [math]::Min($restartNumber, $restartDelays.Count - 1)
        $delaySeconds = $restartDelays[$delayIndex]
        $restartNumber++
        Write-Warning "GOLD process exited. Restart attempt $restartNumber in $delaySeconds seconds."
        Start-Sleep -Seconds $delaySeconds

        $botScript = Join-Path $PSScriptRoot "gold_signal_bot.py"
        $previousStateFile = [Environment]::GetEnvironmentVariable("GOLD_SIGNAL_STATE_FILE", "Process")
        try {
            # The child must use exactly the same state/WAL paths as the monitor.
            $env:GOLD_SIGNAL_STATE_FILE = $StateFile
            $process = Start-Process -FilePath "python" -ArgumentList @("`"$botScript`"") -WorkingDirectory $repoRoot -PassThru
            $ProcessId = [int]$process.Id
            $currentStartedAt = Get-Date
            Write-Host "Watchdog restarted GOLD bot with PID $ProcessId using StateFile $StateFile"
            Send-WatchdogTelegramAlert -Message "🔄 GOLD watchdog restarted the signal bot (PID $ProcessId). State/WAL path preserved." | Out-Null
        }
        catch {
            $ProcessId = 0
            $currentStartedAt = Get-Date
            Write-Warning "GOLD process could not be started; the watchdog will count this as another failure."
        }
        finally {
            if ($null -eq $previousStateFile) {
                Remove-Item Env:GOLD_SIGNAL_STATE_FILE -ErrorAction SilentlyContinue
            }
            else {
                $env:GOLD_SIGNAL_STATE_FILE = $previousStateFile
            }
        }
    }
    if (-not $stateParseOk -or -not $outboxParseOk) {
        throw "State or WAL JSON became unreadable. Stop the test and inspect the files; see $csvPath"
    }

    Write-Host ("{0} | WorkingSet={1} MB | Private={2} MB | ActiveSignals={3} | Pending={4}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $row.WorkingSetMB, $row.PrivateMemoryMB, $activeSignals, $pendingDeliveries)

    $remaining = ($deadline - (Get-Date)).TotalSeconds
    if ($remaining -gt 0) {
        Start-Sleep -Seconds ([math]::Min($IntervalSeconds, [math]::Ceiling($remaining)))
    }
}

if ($watchdogStopped) {
    Write-Warning "The monitoring loop stopped because the automatic-restart safety cap was reached."
}
else {
    Write-Host "Completed the $Hours-hour observation."
    Write-Host "Stop the bot manually with Ctrl+C in its own terminal."
}
Write-Host "Review the monitoring CSV: $csvPath"
