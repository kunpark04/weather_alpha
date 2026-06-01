"""Kill switch — arm / disarm / status, from any terminal at any time.

The bot checks for the kill-switch file BEFORE EVERY order (engine cycle start
AND before each leg). Arming it halts new entries immediately, even mid-cycle.
It does NOT cancel orders already resting on the exchange — use the Kalshi UI or
`KalshiClient.cancel_order` for that; this stops the bot from placing MORE.

Usage (run from project root) — --config is REQUIRED and MUST be the RUNNING bot's config,
because each deploy uses its own kill-switch file (data/live/, data/paper/, ...). Omitting it
used to silently target data/KILL_SWITCH, which a deployed bot never checks (emergency no-op):
    python scripts/kill.py --config config/live.yaml            # ARM -> live bot stops trading
    python scripts/kill.py --config config/live.yaml --status   # report without changing
    python scripts/kill.py --config config/live.yaml --disarm   # allow trading again
    python scripts/kill.py --config config/paper.yaml           # the paper bot, etc.

The path is read from the given config (paths.kill_switch), so it matches exactly what THAT
bot checks. Safe to run while the bot is live.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from weather_alpha.config import load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Arm/disarm the trading kill switch.")
    ap.add_argument("--disarm", action="store_true", help="remove the kill-switch file (allow trading)")
    ap.add_argument("--status", action="store_true", help="report state without changing it")
    ap.add_argument("--reason", default="manual", help="reason recorded in the file (for the audit trail)")
    ap.add_argument("--config", required=True,
                    help="REQUIRED — the running bot's config (e.g. config/live.yaml); the "
                         "kill-switch path is read from it so it MUST match the live bot")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, require_live_creds=False)   # #1: arm the switch even from a credless shell
    path = Path(cfg.paths.kill_switch)
    armed = path.exists()

    if args.status:
        if armed:
            print(f"ARMED — {path}")
            try:
                print(path.read_text(encoding="utf-8").strip())
            except OSError:
                pass
        else:
            print(f"disarmed — no file at {path}")
        return 0

    if args.disarm:
        if armed:
            path.unlink()
            print(f"DISARMED — removed {path}. Trading allowed again.")
        else:
            print(f"already disarmed — no file at {path}")
        return 0

    # Default action: ARM.
    if armed:
        print(f"already ARMED — {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    path.write_text(f"ARMED {stamp}\nreason: {args.reason}\n", encoding="utf-8")
    print(f"ARMED — wrote {path}. Bot will halt before the next order.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
