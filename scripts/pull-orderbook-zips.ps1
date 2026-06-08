#requires -Version 7
<#
.SYNOPSIS
  Pull settled-day orderbook zips from the always-on logger host to THIS Windows machine — BATCHED.

.DESCRIPTION
  Same job + env-var interface as before, but minimizes SSH round-trips. The old version opened a
  fresh connection PER OPERATION (stat + scp + rm for every zip => ~3 connections/zip, ~13 for 4
  zips), and each handshake to the droplet costs real seconds. Windows OpenSSH has NO ControlMaster
  multiplexing (it actually BREAKS the connection if configured), so the portable fix is to batch:

    1) one ssh  -> list every remote *.zip WITH its byte size      (find -printf)
    2) one ssh  -> tar+gzip the still-needed zips into ONE archive on the host
    3) one scp  -> pull that single archive (reliable binary transfer)
    4) one ssh  -> delete the verified zips (move mode) + the temp archive

  => ~4 connections TOTAL no matter how many zips (was 1 + 3N). Touches only *.zip; the live raw
  .jsonl folders are never read or deleted. Each zip's byte size is verified locally BEFORE any
  remote delete, so a failed/partial pull never deletes the remote copy.

  Modes:
    default (copy)   leave each zip on the host; only the temp archive is removed.
    OB_MOVE set      after the archive is pulled, extracted, and every zip byte-verified locally,
                     delete those zips from the host (one ssh) so they end up ONLY here.

  Config via OB_HOST (required), OB_REMOTE_DIR, OB_LOCAL_DIR, OB_MOVE. Needs passwordless key SSH.
#>
$ErrorActionPreference = 'Stop'

$RemoteHost = if ($env:OB_HOST)       { $env:OB_HOST }       else { throw 'set OB_HOST, e.g. weather-alpha@your-logger-host (or an ssh config alias)' }
$RemoteDir  = if ($env:OB_REMOTE_DIR) { $env:OB_REMOTE_DIR } else { 'data/orderbook' }  # path on host (rel = from $HOME; flat layout)
$LocalDir   = if ($env:OB_LOCAL_DIR)  { $env:OB_LOCAL_DIR }  else { Join-Path $HOME 'weather-alpha-data\orderbook' }
$Move       = [bool]$env:OB_MOVE
$RemoteTar  = '.ob-pull.tar.gz'                              # temp archive INSIDE $RemoteDir (never the host $HOME)

# Explicitly DISABLE multiplexing: Windows OpenSSH can't do ControlMaster, and a stray ControlMaster
# block in ~/.ssh/config will otherwise break every connection ("Connection closed"). BatchMode so a
# missing key fails fast instead of prompting; a sane connect timeout.
$SshOpt = @('-o','BatchMode=yes','-o','ConnectTimeout=20','-o','ControlMaster=no','-o','ControlPath=none')

function Invoke-Ssh([string]$cmd) { ssh @SshOpt $RemoteHost $cmd }
function Test-LocalGood([string]$rel, [int64]$size) {
    $lp = Join-Path $LocalDir ($rel -replace '/', '\')
    (Test-Path -LiteralPath $lp) -and ((Get-Item -LiteralPath $lp).Length -eq $size)
}

# 1) one ssh: list every remote zip with its byte size (tab-separated).
$listing = Invoke-Ssh "find '$RemoteDir' -name '*.zip' -type f -printf '%P`t%s`n'"
if ($LASTEXITCODE -ne 0) { throw "ssh listing failed (exit $LASTEXITCODE) — check OB_HOST / SSH key / OB_REMOTE_DIR" }

$remote = [ordered]@{}
foreach ($line in ($listing -split "`n")) {
    $line = $line.TrimEnd("`r"); if (-not $line.Trim()) { continue }
    $rel, $sz = $line -split "`t", 2
    if ($rel) { $remote[$rel] = [int64]$sz }
}
if ($remote.Count -eq 0) { Write-Host 'no remote zips — nothing to do'; return }

# which zips do we still need? (absent locally, or a size mismatch)
$needed = @($remote.Keys | Where-Object { -not (Test-LocalGood $_ $remote[$_]) } | Sort-Object)

# 2+3) tar the needed zips on the host into ONE archive, pull it, extract — one ssh + one scp.
if ($needed.Count -gt 0) {
    $fileArgs = ($needed | ForEach-Object { "'$_'" }) -join ' '
    Invoke-Ssh "cd '$RemoteDir' && tar czf '$RemoteTar' $fileArgs" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "remote tar failed (exit $LASTEXITCODE)" }

    $localTar = Join-Path ([IO.Path]::GetTempPath()) 'ob-pull.tar.gz'
    scp @SshOpt -q "${RemoteHost}:$RemoteDir/$RemoteTar" $localTar
    if ($LASTEXITCODE -ne 0) { throw "scp of archive failed (exit $LASTEXITCODE)" }

    New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null
    tar xzf $localTar -C $LocalDir
    if ($LASTEXITCODE -ne 0) { throw "local extract failed (exit $LASTEXITCODE)" }
    try { [IO.File]::Delete($localTar) } catch { }
    Write-Host "pulled $($needed.Count) zip(s) in one archive"
} else {
    Write-Host 'all remote zips already present locally'
}

# 4) verify every remote zip now has a byte-matching local copy.
$verified = @($remote.Keys | Where-Object { Test-LocalGood $_ $remote[$_] })
foreach ($r in $remote.Keys) { if ($r -notin $verified) { Write-Warning "size mismatch/missing locally: $r (left on host)" } }

# delete on host: verified zips (MOVE mode only) + always the temp archive if we made one — one ssh.
$targets = @()
if ($Move)              { $targets += ($verified | ForEach-Object { "'$RemoteDir/$_'" }) }
if ($needed.Count -gt 0){ $targets += "'$RemoteDir/$RemoteTar'" }
if ($targets.Count -gt 0) {
    Invoke-Ssh ("rm -f " + ($targets -join ' ')) | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Warning "remote cleanup rm exited $LASTEXITCODE" }
}

$summary = if ($Move) { "$($needed.Count) pulled, $($verified.Count) verified & removed from host" }
           else        { "$($needed.Count) pulled (copy mode; host keeps zips)" }
Write-Host "pull complete -> $LocalDir ($summary)"
