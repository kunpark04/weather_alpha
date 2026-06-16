# `rust/` — Weather-Alpha LIVE engine

The **LIVE trading engine** for the Option-A directional strategy, rewritten from the Python engine
code into a modular Rust cargo workspace. It is the live (real-order-capable) path; the **paper
shadow stays in Python** (`scripts/directional_paper.py` + the `weather-alpha-paper-cap@*` timers),
which mirrors this engine's executable rule so the paper results predict live behavior.

## Model — Option A, true live replication
A resident daemon wakes at **each city's own local anchor** (high **17:00** / low **22:00**, DST-correct),
fetches the **live** Kalshi book at that instant, and applies the executable per-city rule:

- favorite = highest-mid bucket; **enter iff** its mid ∈ `[0.93, 0.95]`;
- **daily cap of 3 entries per market per event-date**, first-come as anchors fire east→west — **no
  cross-city look-ahead** (the batch backtest's "top-3 by confidence" is *not* live-executable);
- each entry stakes one of 3 equal slots = `stake_fraction/daily_cap` = **16.67%** of bankroll;
- P&L booked at the achievable fill (VWAP from walking the live depth ladder), net of the real fee.

This is the same rule the Python paper bot now uses, so paper ≈ live.

## Functional decomposition (one concern per crate)
| crate | role |
|---|---|
| `wa-fees` | Kalshi fee `ceil(7%·N·P·(1−P)·100)` (port of `fees.py`) |
| `wa-book` | orderbook types, `mid`/favorite/band, depth-feasibility ladder walk (pure) |
| `wa-algo` | the executable per-city Option-A decision (pure) |
| `wa-schedule` | per-city local-anchor scheduler (chrono-tz; no off-anchor catch-up) |
| `wa-state` | per-market `Book` persistence (bankroll / HWM / drawdown halts / daily cap / open positions) |
| `wa-kalshi` | REST client: RSA-PSS auth, `/markets`, `/markets/{t}/orderbook`, `/portfolio/*`, orders (rustls, no OpenSSL) |
| `wa-exec` | paper/live execution routing + kill switch + settlement P&L |
| `wa-engine` | the resident daemon binary that wires it together |

Pure crates carry unit tests asserting parity with the Python reference (`cargo test` → 39 tests).
The Kalshi market data path is keyless (public); only `/portfolio/*` + orders are signed.

## Run
```
cargo test                                   # parity tests
cargo run -p wa-engine -- --once --dry-run   # evaluate every city NOW, print + log, place nothing
cargo run -p wa-engine -- --once --market high --dry-run
# resident: cargo run -p wa-engine -- --paper        (mode in wa.toml; --live arms real orders)
```
Flags: `--config <wa.toml>` · `--once` (one pass, exits; defaults to dry-run) · `--market high|low|both`
· `--paper`|`--live`|`--dry-run`. Config + 20 cities live in [`wa.toml`](wa.toml).

## Build & deploy (the droplet)
The droplet has no Rust; build a Linux binary in WSL and copy it over.
```
# from the repo root, on Windows:
powershell -File deploy/build-engine.ps1      # -> rust/dist/wa-engine (Linux x86_64)
# or inside WSL:  bash deploy/build-engine.sh
scp rust/dist/wa-engine weather-alpha@HOST:weather-alpha/bin/
```
WSL (Ubuntu 24.04 / glibc 2.39) matches the droplet, so the default **GNU** build is portable. A
fully-static **musl** binary needs `sudo apt install musl-tools` once, then `TARGET=x86_64-unknown-linux-musl`.

**Arming live is a deliberate, separate step** (the unit ships disabled + `--paper`):
install `deploy/wa-engine.service`, edit `--paper` → `--live`, ensure `secrets/kalshi-rw.env`, then
`systemctl --user enable --now wa-engine`. Do this only after the Python paper shadow validates the edge.
