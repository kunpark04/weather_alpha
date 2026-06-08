#requires -Version 7
<#
.SYNOPSIS
  Pull the resident bots' forward STATE (per-mode live_log history + positions snapshots) from the
  always-on droplet to THIS machine's repo data/ dir, so the history lives ONLY on local.

.DESCRIPTION
  The resident bots write ISOLATED state per mode (config/{paper,live}.yaml):
    data/paper/{live_log.parquet, positions.json} , data/live/{live_log.parquet, positions.json}
  (NOT the legacy top-level data/live_log.parquet). This syncs each mode in WA_MODES (default
  "paper live"), handling the two file kinds' different natures safely:

    live_log.parquet  -> atomically `mv` to a dated shard on the droplet (the bot recreates a fresh
                         log on its next append, race-free), pull it, byte-verify locally, THEN delete
                         the shard from the droplet. Net: the log history ends up ONLY here.
                         scripts/merge_state.py folds shards into data/<mode>/live_log.parquet (dedup).
    positions.json    -> COPIED down (overwrite local data/<mode>/positions.json). The live one is LEFT
                         on the droplet — it is the running bot's Book; deleting it breaks settlement.

  Staged files are mode-tagged "<mode>__<name>" in one staging dir so a single tar/scp/rm covers every
  mode. Verification-before-delete: a shard is removed from the droplet only after its byte size is
  confirmed locally, so a failed/partial pull never deletes the only copy.

  Closed-laptop safe: while this machine sleeps the droplet buffers (logs grow, Books stay current);
  nothing is deleted until verified here. Register with -StartWhenAvailable to catch up on next wake
  (deploy/README.md -> "Pull bot state").

  Config: WA_HOST (or OB_HOST), WA_REMOTE_PROJ (default projects/weather-alpha), WA_MODES
  (default "paper live"), WA_LOCAL_DATA (default repo data/). Needs passwordless key SSH.
#>
$ErrorActionPreference = 'Stop'

$RemoteHost = if ($env:WA_HOST) { $env:WA_HOST } elseif ($env:OB_HOST) { $env:OB_HOST } else { throw 'set WA_HOST (or OB_HOST), e.g. fa@137.184.128.37 or an ssh config alias' }
$RemoteProj = if ($env:WA_REMOTE_PROJ) { $env:WA_REMOTE_PROJ } else { 'projects/weather-alpha' }
$Modes      = if ($env:WA_MODES)       { $env:WA_MODES }       else { 'paper live' }                    # space-separated
$LocalData  = if ($env:WA_LOCAL_DATA)  { $env:WA_LOCAL_DATA }  else { (Resolve-Path (Join-Path $PSScriptRoot '..\data')).Path }
$Inbox      = Join-Path $LocalData 'state_inbox'
$RemoteTar  = '.state-pull.tar.gz'

$SshOpt = @('-o','BatchMode=yes','-o','ConnectTimeout=20','-o','ControlMaster=no','-o','ControlPath=none')
function Invoke-Ssh([string]$cmd) { ssh @SshOpt $RemoteHost $cmd }

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')

# 1) ONE ssh, single physical line (no CRLF to remote bash -- lesson L7): for each mode, rotate
#    live_log -> dated shard and snapshot positions.json into a shared staging dir (files tagged
#    "<mode>__<name>"); tar the staging dir; list staged files (name<TAB>size). Bash uses only
#    double quotes + $(...), kept inside a PS single-quoted template so PowerShell never touches it.
$tmpl = 'cd "__PROJ__" && mkdir -p data/.state-pull && for m in __MODES__; do if [ -s data/$m/live_log.parquet ]; then mv -f data/$m/live_log.parquet data/.state-pull/${m}__live_log-__STAMP__.parquet; fi; if [ -s data/$m/positions.json ]; then cp -f data/$m/positions.json data/.state-pull/${m}__positions.json; fi; done; cd data/.state-pull && { if [ -n "$(ls -A)" ]; then tar czf ~/__TAR__ .; fi; } ; find . -maxdepth 1 -type f ! -name "*.tar.gz" -printf "%P\t%s\n"'
$cmd = $tmpl.Replace('__PROJ__', $RemoteProj).Replace('__MODES__', $Modes).Replace('__STAMP__', $stamp).Replace('__TAR__', $RemoteTar)

$listing = Invoke-Ssh $cmd
if ($LASTEXITCODE -ne 0) { throw "ssh rotate/stage failed (exit $LASTEXITCODE) -- check WA_HOST / SSH key / WA_REMOTE_PROJ" }

$staged = [ordered]@{}
foreach ($line in ($listing -split "`n")) {
    $line = $line.TrimEnd("`r"); if (-not $line.Trim()) { continue }
    $name, $sz = $line -split "`t", 2
    if ($name) { $staged[$name] = [int64]$sz }
}
if ($staged.Count -eq 0) { Write-Host "nothing staged on the droplet for modes [$Modes] -- nothing to pull"; return }

# 2) ONE scp: pull the tar; extract to a temp dir.
$localTar = Join-Path ([IO.Path]::GetTempPath()) 'wa-state-pull.tar.gz'
scp @SshOpt -q "${RemoteHost}:$RemoteTar" $localTar
if ($LASTEXITCODE -ne 0) { throw "scp of state archive failed (exit $LASTEXITCODE)" }
$exDir = Join-Path ([IO.Path]::GetTempPath()) ("wa-state-" + $stamp)
New-Item -ItemType Directory -Force -Path $exDir | Out-Null
tar xzf $localTar -C $exDir
if ($LASTEXITCODE -ne 0) { throw "local extract failed (exit $LASTEXITCODE)" }

# 3) verify each staged file (byte-exact) BEFORE any remote delete; route by "<mode>__<name>".
$verified = @()
foreach ($name in $staged.Keys) {
    $ex = Join-Path $exDir $name
    if (-not (Test-Path -LiteralPath $ex) -or (Get-Item -LiteralPath $ex).Length -ne $staged[$name]) {
        Write-Warning "size mismatch/missing after extract: $name (left on droplet)"; continue
    }
    $mode, $rest = $name -split '__', 2
    if (-not $rest) { Write-Warning "unexpected staged file ignored: $name"; continue }
    if ($rest -like 'live_log-*.parquet') {
        $dst = Join-Path $Inbox $mode                                    # data/state_inbox/<mode>/  (merge_state folds these in)
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -LiteralPath $ex -Destination (Join-Path $dst $rest) -Force
    } elseif ($rest -eq 'positions.json') {
        $dst = Join-Path $LocalData $mode                                # data/<mode>/positions.json (overwrite local snapshot)
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -LiteralPath $ex -Destination (Join-Path $dst 'positions.json') -Force
    } else {
        Write-Warning "unexpected staged file ignored: $name"; continue
    }
    $verified += $name
}

# 4) ONE ssh: delete the VERIFIED staged files from the droplet's staging dir (+ the temp tar). The LIVE
#    data/<mode>/positions.json and the freshly-recreated data/<mode>/live_log.parquet are untouched.
$rm = @("~/'$RemoteTar'")
foreach ($name in $verified) { $rm += "'$RemoteProj/data/.state-pull/$name'" }
Invoke-Ssh ("rm -f " + ($rm -join ' ')) | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning "remote cleanup rm exited $LASTEXITCODE (will retry next run)" }

try { [IO.File]::Delete($localTar) } catch { }
try { Remove-Item -Recurse -Force -LiteralPath $exDir } catch { }

# 5) fold the pulled shard(s) into the canonical per-mode local logs (dedup + archive shards).
$py = if ($env:WA_PYTHON) { $env:WA_PYTHON } else { 'python' }
$env:WA_DATA_DIR = $LocalData                         # keep merge's data dir aligned with the pull's
& $py (Join-Path $PSScriptRoot 'merge_state.py')
if ($LASTEXITCODE -ne 0) { Write-Warning "merge_state.py exited $LASTEXITCODE -- shards remain in $Inbox for the next run" }

$logs = @($verified | Where-Object { $_ -like '*__live_log-*' }).Count
$pos  = @($verified | Where-Object { $_ -like '*__positions.json' }).Count
Write-Host "state pull complete -> $LocalData ($logs log shard(s) moved off droplet; $pos positions snapshot(s) refreshed; modes [$Modes])"
