<#
Pull the directional PAPER-test logs from the droplet to this machine (daily, indefinitely).

The droplet's two scorer timers (directional-paper-{high,low}) append every trade's full detail to
~/weather-alpha/data/directional_paper/{high,low}_log.parquet (+ paper_state.json) and KEEP appending
there (the droplet is the writer). This just COPIES the current files down (overwriting the local
copy) so you can review without SSH -- no rotation/delete (unlike the live_log pull), because the
droplet remains the canonical growing log.

Closed-laptop safe: a missed run self-heals next time (it just re-copies the latest files). Run via a
daily Scheduled Task AFTER the 14:00 UTC settle (set to 11:00 ET / ~15:00 UTC), so the morning pull
already reflects the prior night's SETTLED P&L (running at 8:30am would pull before the settle).

Config: WA_HOST (default weather-alpha@137.184.128.37); remote dir is home-relative.
#>
$ErrorActionPreference = 'Stop'
$WaHost   = if ($env:WA_HOST) { $env:WA_HOST } else { 'weather-alpha@137.184.128.37' }
$remote   = 'weather-alpha/data/directional_paper'
$localDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'data\directional_paper'
New-Item -ItemType Directory -Force -Path $localDir | Out-Null

# does the remote dir have anything yet? (captures begin the day after deploy)
$listing = & ssh -o BatchMode=yes -o ConnectTimeout=20 $WaHost "ls -1 $remote/ 2>/dev/null" 2>$null
if (-not $listing) {
    Write-Host "no directional-paper logs on the droplet yet (first captures land ~1 day after deploy) -> $localDir"
    exit 0
}
# copy the parquets + the state json down (overwrite local; droplet keeps appending)
& scp -o BatchMode=yes "${WaHost}:$remote/*.parquet" $localDir 2>$null
& scp -o BatchMode=yes "${WaHost}:$remote/paper_state.json" $localDir 2>$null
Write-Host "pulled directional-paper logs -> $localDir"
Get-ChildItem $localDir | Select-Object Name, Length, LastWriteTime | Format-Table -AutoSize
