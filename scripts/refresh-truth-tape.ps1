#requires -Version 7
<#
.SYNOPSIS
  Keep the local truth tape (data\weather\backfill) current with recent settled days, then leave the
  droplet lean -- same zip / move / delete convention as the orderbook pull.

.DESCRIPTION
  1. FETCH on the droplet: source the read-only Kalshi key and run backfill_cities.py --days N
     --series ... (authed live-tier; resumable; writes data/backfill/<series>/<date>.zip on the droplet).
  2. MOVE: pull those zips into the local tape and DELETE them from the droplet, byte-verifying each
     local copy before the remote delete -- by reusing scripts/pull-orderbook-zips.ps1 in move-mode.

  Because the droplet copies are deleted, the next run RE-FETCHES the --days window (the cost of the
  lean-droplet convention). Tune the load with TAPE_DAYS / TAPE_SERIES. The local tape ACCUMULATES
  (only the droplet side is pruned); load_city reads it via the data\backfill junction.

  Config: WA_HOST (or OB_HOST), WA_REMOTE_PROJ (default projects/weather-alpha), TAPE_DAYS (default 7),
  TAPE_SERIES (default all 20), WA_BACKFILL_LOCAL (default ..\data\weather\backfill). The droplet must
  have secrets/kalshi-rw.env (used READ-ONLY here -- backfill only GETs).
#>
$ErrorActionPreference = 'Stop'

$RemoteHost = if ($env:WA_HOST) { $env:WA_HOST } elseif ($env:OB_HOST) { $env:OB_HOST } else { throw 'set WA_HOST (or OB_HOST)' }
$RemoteProj = if ($env:WA_REMOTE_PROJ) { $env:WA_REMOTE_PROJ } else { 'projects/weather-alpha' }
$Days       = if ($env:TAPE_DAYS) { $env:TAPE_DAYS } else { '7' }
$Series     = if ($env:TAPE_SERIES) { $env:TAPE_SERIES } else {
    'KXHIGHCHI KXHIGHTHOU KXHIGHNY KXHIGHMIA KXHIGHAUS KXHIGHDEN KXHIGHPHIL KXHIGHLAX KXHIGHTLV KXHIGHTNOLA KXHIGHTSEA KXHIGHTSFO KXHIGHTDC KXHIGHTATL KXHIGHTMIN KXHIGHTPHX KXHIGHTBOS KXHIGHTDAL KXHIGHTOKC KXHIGHTSATX' }
$LocalTape  = if ($env:WA_BACKFILL_LOCAL) { $env:WA_BACKFILL_LOCAL } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\data\weather\backfill')) }

$SshOpt = @('-o','BatchMode=yes','-o','ConnectTimeout=20','-o','ControlMaster=no','-o','ControlPath=none')

# 1) FETCH recent settled days on the droplet. Single physical line (no CRLF to remote bash -- L7);
#    sources the RW key (used read-only -- backfill only GETs). `$PY` is the remote venv python.
$bash = "cd '$RemoteProj' && set -a && . secrets/kalshi-rw.env && set +a && if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi && `"`$PY`" scripts/backfill_cities.py --days $Days --series $Series"
Write-Host "[refresh-truth-tape] fetching last $Days day(s) for $((($Series -split ' ').Count)) series on the droplet..."
ssh @SshOpt $RemoteHost $bash
if ($LASTEXITCODE -ne 0) { throw "remote backfill_cities failed (exit $LASTEXITCODE)" }

# 2) MOVE the zips into the local tape + delete from the droplet (reuse the byte-verify-then-delete mover).
$env:OB_HOST       = $RemoteHost
$env:OB_REMOTE_DIR = "$RemoteProj/data/backfill"
$env:OB_LOCAL_DIR  = $LocalTape
$env:OB_MOVE       = '1'
Write-Host "[refresh-truth-tape] moving new day-zips -> $LocalTape (deleting from droplet)..."
& (Join-Path $PSScriptRoot 'pull-orderbook-zips.ps1')
Write-Host "[refresh-truth-tape] done."
