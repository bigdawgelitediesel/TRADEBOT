"""Kalshi fee schedule.

Standard taker fee: 7% of expected profit, i.e. fee = 0.07 * C * P * (1 - P),
rounded UP to the next cent, where P is the contract price in dollars and C the
contract count. Same formula whichever side you buy.
"""
from __future__ import annotations

import math

FEE_RATE = 0.07


def taker_fee_cents(count: int, price_cents: int) -> int:
    p = price_cents / 100.0
    fee_dollars = FEE_RATE * count * p * (1 - p)
    return int(math.ceil(round(fee_dollars * 100, 6)))


def fee_per_contract_cents(price_cents: int) -> float:
    """Unrounded per-contract fee; use for EV math, `taker_fee_cents` for actual cost."""
    p = price_cents / 100.0
    return FEE_RATE * p * (1 - p) * 100
