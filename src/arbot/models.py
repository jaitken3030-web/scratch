"""Core data model.

All prices are Decimal dollars per share in [0, 1]; sizes are Decimal
contract counts. Kalshi cent prices and Polymarket string prices are
normalized at the client boundary so everything downstream speaks one
currency.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class Platform(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Outcome(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass
class MarketBook:
    """Order book for one binary market on one platform.

    Levels are sorted best-first (asks ascending, bids descending).
    """

    platform: Platform
    market_id: str
    yes_asks: list[BookLevel] = field(default_factory=list)
    yes_bids: list[BookLevel] = field(default_factory=list)
    no_asks: list[BookLevel] = field(default_factory=list)
    no_bids: list[BookLevel] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def asks(self, outcome: Outcome) -> list[BookLevel]:
        return self.yes_asks if outcome is Outcome.YES else self.no_asks

    def bids(self, outcome: Outcome) -> list[BookLevel]:
        return self.yes_bids if outcome is Outcome.YES else self.no_bids

    def best_ask(self, outcome: Outcome) -> BookLevel | None:
        levels = self.asks(outcome)
        return levels[0] if levels else None

    def best_bid(self, outcome: Outcome) -> BookLevel | None:
        levels = self.bids(outcome)
        return levels[0] if levels else None

    def depth(self, outcome: Outcome) -> Decimal:
        return sum((lvl.size for lvl in self.asks(outcome)), Decimal(0))


@dataclass(frozen=True)
class Pair:
    """A manually approved Kalshi <-> Polymarket market pairing."""

    pair_id: str
    kalshi_ticker: str
    polymarket_condition_id: str
    polymarket_yes_token_id: str
    polymarket_no_token_id: str
    notes: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class Leg:
    """One side of an arbitrage: buy `outcome` on `platform`."""

    platform: Platform
    market_id: str  # kalshi ticker or polymarket token id
    outcome: Outcome
    price: Decimal  # limit price (worst level touched)
    size: Decimal
    cost: Decimal  # size-weighted cost of shares, ex-fees
    fee: Decimal


@dataclass(frozen=True)
class Opportunity:
    """A computed two-leg arbitrage with guaranteed $1 payout per pair."""

    pair_id: str
    direction: str  # e.g. "kalshi_yes/poly_no"
    legs: tuple[Leg, Leg]
    size: Decimal  # matched contracts across both legs
    total_cost: Decimal  # shares + fees, both legs
    edge_bps: Decimal  # (payout - total_cost) / payout in bps
    est_profit: Decimal
    ts: float = field(default_factory=time.time)


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass
class OrderResult:
    order_id: str
    status: OrderStatus
    filled_size: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
