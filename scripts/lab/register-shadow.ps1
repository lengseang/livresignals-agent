<#
Registers the per-user Windows scheduled task LIVR-Shadow: the shadow scanner (docs/loop/designs/P2-shadow-scanner.md).
Every candidate card at stage validated/shadow/promoted (candidate lane) or rejected (rejected lane), plus the BTC and
equal-weight benchmarks, runs 24/7 as an isolated PAPER book with a hash-chained forward record. Market data only
(the data-stream mirror); nothing here can place an order or touch the VPS.

It starts at logon, restarts on failure; a restart re-primes (a few minutes to ~1 h) and records the downtime as a gap.
It runs from a DEDICATED checkout ($Runtime) so switching branches in the dev checkout never changes it.

Before the first run, fetch the daily 1m files that bridge the last monthly file and today:
  .venv\Scripts\python -m trading.stream.history --kinds klines_1m --days 40
Run once, in PowerShell, from the agent repo root:
  powershell -ExecutionPolicy Bypass -File scripts\lab\register-shadow.ps1
Remove with:
  Unregister-ScheduledTask -TaskName LIVR-Shadow -Confirm:$false
#>
param(
  [string]$Repo    = (Resolve-Path "$PSScriptRoot\..\..").Path,
  [string]$Runtime = "D:\1111-Works\LIVRsignals\livr-shadow-runtime",
  [string]$Branch  = "origin/loop/P2c-shadow-runner"
)
$ErrorActionPreference = "Stop"
$py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "venv python not found at $py" }

git -C $Repo fetch -q origin
if (-not (Test-Path $Runtime)) { git -C $Repo worktree add -q $Runtime $Branch } else { git -C $Runtime checkout -q --detach $Branch }

$data = Join-Path $Repo "data\local"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$cards = Join-Path $Runtime "research\candidates\candidates.json"
$cmd = "set PYTHONPATH=$Runtime\src&& cd /d $Repo&& `"$py`" -u -m trading.shadow.run --cards `"$cards`" >> `"$data\LIVR-Shadow.log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
Register-ScheduledTask -TaskName "LIVR-Shadow" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "registered LIVR-Shadow. Start now without logging off:  Start-ScheduledTask LIVR-Shadow"
Write-Host "Forward scorecards: data\local\shadow\scorecards.json (also on the terminal league, Forward column)"
