#!/usr/bin/env bash
# Build the Weather-Alpha LIVE engine (Rust) as a Linux x86_64 binary INSIDE WSL (native Linux),
# run the parity tests, and stage the binary in rust/dist/ for scp to the droplet.
#
# The droplet (Ubuntu 24.04 / glibc 2.39) matches this WSL env, so the default GNU dynamic build is
# portable as-is. A fully-static musl binary also works after a one-time `sudo apt install musl-tools`
# (then run with TARGET=x86_64-unknown-linux-musl).
#
# Usage (from the repo root, inside WSL):  bash deploy/build-engine.sh
set -euo pipefail
TARGET="${TARGET:-x86_64-unknown-linux-gnu}"
SRC="$(cd "$(dirname "$0")/../rust" && pwd)"
OUT="${OUT:-$SRC/dist}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$HOME/wa-target}"
source "$HOME/.cargo/env" 2>/dev/null || true

echo ">> rustup target add $TARGET (idempotent)"; rustup target add "$TARGET" >/dev/null 2>&1 || true
echo ">> cargo test --workspace (parity tests)";   (cd "$SRC" && cargo test --workspace --quiet)
echo ">> cargo build --release --target $TARGET";   (cd "$SRC" && cargo build --release --target "$TARGET")

BIN="$CARGO_TARGET_DIR/$TARGET/release/wa-engine"
mkdir -p "$OUT"
cp "$BIN" "$OUT/wa-engine"
file "$OUT/wa-engine"
echo ">> staged: $OUT/wa-engine"
echo "   deploy:  scp '$OUT/wa-engine' weather-alpha@HOST:weather-alpha/bin/"
echo "   arm:     edit deploy/wa-engine.service (--paper -> --live), install it, enable --now"
