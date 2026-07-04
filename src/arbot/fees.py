"""Fee models for Kalshi and Polymarket.

Kalshi (general markets): fee = ceil_to_cent(rate * C * P * (1 - P)),
where C is contract count and P the price in dollars. The published
general rate is 0.07; some markets differ, so the rate is configurable.

Polymarket CLOB: fee = rate * min(p, 1 - p) * shares. The base fee rate
has been 0 on markets to date, but the formula is live in the protocol,
so we model it and default the rate from config.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

CENT = Decimal("0.01")


def kalshi_fee(price: Decimal, contracts: Decimal, rate: Decimal = Decimal("0.07")) -> Decimal:
    """Kalshi taker fee in dollars, rounded up to the next cent."""
    if contracts <= 0:
        return Decimal("0.00")
    raw = rate * contracts * price * (Decimal(1) - price)
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def polymarket_fee(price: Decimal, shares: Decimal, rate: Decimal = Decimal("0")) -> Decimal:
    """Polymarket CLOB fee in dollars: rate * min(p, 1-p) * shares."""
    if shares <= 0 or rate == 0:
        return Decimal("0.00")
    raw = rate * min(price, Decimal(1) - price) * shares
    return raw.quantize(CENT, rounding=ROUND_CEILING)


def fee_for(platform: str, price: Decimal, size: Decimal, *, kalshi_rate: Decimal, polymarket_rate: Decimal) -> Decimal:
    if platform == "kalshi":
        return kalshi_fee(price, size, kalshi_rate)
    if platform == "polymarket":
        return polymarket_fee(price, size, polymarket_rate)
    raise ValueError(f"unknown platform: {platform}")
