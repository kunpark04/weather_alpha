#requires -Version 7
<#
  Launcher for the multi-city Kalshi backfill, run by the 'WeatherAlphaBackfill' Scheduled
  Task (auto-restart on failure; triggers at logon + startup). Loops the resumable backfill
  until it writes data/backfill/_DONE (a clean pass with zero failed days), so it truly runs
  "until completion" even across transient API errors. A crash of this whole process is caught
  by the Task's restart-on-failure; the backfill resumes from existing day-zips either way.

  Sets the READ-ONLY Kalshi key into the env (never trades). Logs to logs/backfill.log.
#>
$ErrorActionPreference = 'Continue'
$PSNativeCommandUseErrorActionPreference = $false   # a non-zero python exit must NOT throw — keep looping

$proj = 'C:\Users\kunpa\Downloads\Projects\Kalshi\weather-alpha'
$py   = Join-Path $proj '.venv\Scripts\python.exe'
$keys = 'C:\Users\kunpa\Downloads\Projects\Authentication\kalshi-api-keys'
$done = Join-Path $proj 'data\backfill\_DONE'
$log  = Join-Path $proj 'logs\backfill.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null

$env:KALSHI_KEY_ID          = (Get-Content (Join-Path $keys 'readonly-key-id') -Raw).Trim()
$env:KALSHI_PRIVATE_KEY_PATH = (Join-Path $keys 'readonly-private-key.pem')

function Log($m) { "[{0}] {1}" -f (Get-Date -Format s), $m | Tee-Object -FilePath $log -Append }

Set-Location $proj
Log "launcher start (py=$py)"
for ($i = 1; $i -le 500; $i++) {
    Log "pass $i starting"
    & $py (Join-Path $proj 'scripts\backfill_cities.py') --days 365 *>> $log
    if (Test-Path $done) { Log "pass ${i}: _DONE present -> fully complete, exiting."; break }
    Log "pass ${i}: ended without _DONE (failures remain); retry in 60s"
    Start-Sleep -Seconds 60
}
Log "launcher exiting"
