[CmdletBinding()]
param(
    [ValidateSet(24, 48, 72)]
    [int]$Hours = 24,

    [ValidateRange(10, 3600)]
    [int]$IntervalSeconds = 60,

    [int]$ProcessId = 0,

    [string]$StateFile = (Join-Path $HOME ".quantdinger_gold_signal_state.json")
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
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
Write-Host "This monitor does not stop or restart the bot."

while ((Get-Date) -lt $deadline) {
    $sampleTime = (Get-Date).ToUniversalTime().ToString("o")
    $alive = $true
    try {
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
        throw "GOLD process $ProcessId exited before the $Hours-hour observation completed. See $csvPath"
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

Write-Host "Completed the $Hours-hour observation. The GOLD process is still running."
Write-Host "Stop the bot manually with Ctrl+C in its own terminal."
Write-Host "Review the monitoring CSV: $csvPath"
