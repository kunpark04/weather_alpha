"""Kalshi fee model.

Kalshi's per-side fee schedule (as documented and reflected in live_predict.ipynb):
    fee_cents = ceil(7 * N * P * (1 - P))   # N contracts, P price in dollars

Maximizes around P = 0.5 (~$1.75 / 100 contracts) and shrinks toward the edges.
The ceil-to-nearest-cent is per-trade, not per-contract — so we apply it once
on the total leg.
"""

from __future__ import annotations

import math


def trade_fee_cents(price_dollars: float, contracts: int, fee_rate: float = 0.07) -> int:
    """Per-trade fee in cents. `price_dollars` ∈ [0, 1]."""
    if contracts <= 0:
        return 0
    p = max(0.0, min(1.0, float(price_dollars)))
    raw = fee_rate * contracts * p * (1.0 - p) * 100.0   # 100x to express in cents
    return int(math.ceil(raw))


def net_ev_cents(p_model: float, price_dollars: float, contracts: int = 1,
                 fee_rate: float = 0.07) -> float:
    """Expected value of buying `contracts` units at `price_dollars`, net of fees, in cents.

    For a YES side bet: payout = $1 if outcome occurs, else $0.
        EV_gross_cents = contracts * 100 * (p_model - price)
        EV_net_cents   = EV_gross_cents - trade_fee_cents(price, contracts)
    """
    gross_cents = contracts * 100.0 * (p_model - price_dollars)
    return gross_cents - trade_fee_cents(price_dollars, contracts, fee_rate)


def break_even_edge_cents(price_dollars: float, contracts: int = 1,
                          fee_rate: float = 0.07) -> float:
    """The minimum gross edge per contract (in cents) needed to clear fees."""
    if contracts <= 0:
        return 0.0
    return trade_fee_cents(price_dollars, contracts, fee_rate) / contracts
