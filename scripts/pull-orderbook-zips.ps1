#requires -Version 7
<#
.SYNOPSIS
  Pull settled-day orderbook zips from the always-on logger host to THIS Windows machine.

.DESCRIPTION
  Runs on your LOCAL / analysis box (Windows) — NOT the logger host. The logger writes
  <series>/<date>.zip on the host once a day settles; this fetches each new zip down to you.
  See deploy/README.md "Pull settled-day zips to your local machine".

  Uses native OpenSSH (ssh.exe / scp.exe, built into Win10/11) — no rsync needed.

  Modes:
    default (copy)   leave each zip on the host as a backup; copy only zips we don't have.
    OB_MOVE set      MOVE: after a zip is copied AND its byte size matches the host's, delete
                     it from the host (ssh rm), so it ends up ONLY here. The remote delete
                     happens only after the local size is verified, so a failed/partial copy
                     never deletes the remote copy.
  Either mode touches only *.zip — the live raw .jsonl folders are never copied or deleted,
  so the running logger is undisturbed. A missed run self-heals next time.

  Needs passwordless SSH from here to the host (key-based; for an unattended Task the key
  must have no passphrase or be served by a persistent agent). Configure via the vars below
  or the OB_HOST / OB_REMOTE_DIR / OB_LOCAL_DIR / OB_MOVE environment variables.
#>
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false   # we check ssh/scp/stat exit codes via $LASTEXITCODE ourselves (don't auto-throw per-file)

$RemoteHost = if ($env:OB_HOST)       { $env:OB_HOST }       else { throw 'set OB_HOST, e.g. fa@your-logger-host (or an ssh config alias)' }
$RemoteDir  = if ($env:OB_REMOTE_DIR) { $env:OB_REMOTE_DIR } else { 'projects/weather-alpha/data/orderbook' }  # path on host (rel = from $HOME)
$LocalDir   = if ($env:OB_LOCAL_DIR)  { $env:OB_LOCAL_DIR }  else { Join-Path $HOME 'weather-alpha-data\orderbook' }
$Move       = [bool]$env:OB_MOVE      # any non-empty value -> delete each zip from the host after verifying it here

# List every .zip on the host as paths relative to $RemoteDir (GNU find on the Linux host).
$remoteZips = ssh $RemoteHost "find '$RemoteDir' -name '*.zip' -type f -printf '%P\n'"
if ($LASTEXITCODE -ne 0) { throw "ssh listing failed (exit $LASTEXITCODE) — check OB_HOST / SSH key / OB_REMOTE_DIR" }

$pulled = 0; $removed = 0
foreach ($rel in $remoteZips) {
    $rel = $rel.Trim()
    if (-not $rel) { continue }
    $localPath = Join-Path $LocalDir ($rel -replace '/', '\')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $localPath) | Out-Null

    # Remote byte size — decides whether a copy is needed and gates any delete.
    $sizeStr = (ssh $RemoteHost "stat -c %s '$RemoteDir/$rel'" | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $sizeStr -notmatch '^\d+$') { Write-Warning "stat failed for $rel — skipping"; continue }
    $remoteSize = [int64]$sizeStr

    $haveGood = (Test-Path -LiteralPath $localPath) -and ((Get-Item -LiteralPath $localPath).Length -eq $remoteSize)
    if (-not $haveGood) {
        scp "${RemoteHost}:$RemoteDir/$rel" $localPath
        if ($LASTEXITCODE -ne 0) { Write-Warning "scp failed for $rel — left on remote"; continue }
        $pulled++; Write-Host "pulled $rel"
        $haveGood = (Test-Path -LiteralPath $localPath) -and ((Get-Item -LiteralPath $localPath).Length -eq $remoteSize)
    }

    if ($Move) {
        if ($haveGood) {
            ssh $RemoteHost "rm -f '$RemoteDir/$rel'"
            if ($LASTEXITCODE -eq 0) { $removed++; Write-Host "  removed remote $rel" }
            else { Write-Warning "  copied OK but remote rm failed for $rel" }
        } else {
            Write-Warning "size mismatch for $rel — left on remote (not deleted)"
        }
    }
}
$summary = if ($Move) { "$pulled new, $removed removed from host" } else { "$pulled new" }
Write-Host "pull complete -> $LocalDir ($summary)"
