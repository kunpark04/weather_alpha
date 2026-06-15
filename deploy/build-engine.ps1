<#
Build the Rust LIVE engine via WSL (a native x86_64-Linux build) from this Windows host.
Produces rust/dist/wa-engine (a Linux binary for the droplet). Requires WSL with the Rust toolchain
(rustup + the target); see deploy/build-engine.sh for the one-time setup.
#>
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
Push-Location $repo
try {
    # wsl inherits the Windows cwd as its /mnt path, so the relative script path resolves.
    wsl -e bash -lc "bash deploy/build-engine.sh"
} finally {
    Pop-Location
}
