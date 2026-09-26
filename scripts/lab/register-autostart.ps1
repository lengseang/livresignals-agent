<#
Registers two per-user Windows scheduled tasks that start at logon and restart on failure:
  LIVR-Recorder : live market-data recorder (paper, market-data mirror only)
  LIVR-Lab      : lab runner, polls data/local/lab/queue every 30 min, 6 workers

Both run from a DEDICATED checkout ($Runtime) so switching branches in the dev checkout
can never change or stop them. Nothing here touches real money or the VPS.

Run once, in PowerShell, from the agent repo root:
  powershell -ExecutionPolicy Bypass -File scripts\lab\register-autostart.ps1
Remove with:
  Unregister-ScheduledTask -TaskName LIVR-Recorder,LIVR-Lab -Confirm:$false
#>
param(
  [string]$Repo    = (Resolve-Path "$PSScriptRoot\..\..").Path,
  [string]$Runtime = "D:\1111-Works\LIVRsignals\livr-runtime",
  [string]$Branch  = "origin/loop/L1d-autostart",
  [int]$Workers    = 6
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

function Register($name, $pyArgs) {
  $cmd = "set PYTHONPATH=$Runtime\src&& cd /d $Repo&& `"$py`" -u $pyArgs >> `"$data\$name.log`" 2>&1"
  $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd"
  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
  Write-Host "registered $name"
}
Register "LIVR-Recorder" "-m trading.stream.run_ingest --out data/local/ticks"
Register "LIVR-Lab"      "-m trading.lab.runner --root data/local/lab --workers $Workers --every 30"
Write-Host "Start now without logging off:  Start-ScheduledTask LIVR-Recorder; Start-ScheduledTask LIVR-Lab"
