"""Arbitrage math.

For an approved pair, buying YES on one platform and NO on the other
pays out exactly $1 per matched contract at resolution (assuming the
resolution criteria truly match — see README on resolution risk). An
arb exists when the combined ask-side cost of both legs, including
both platforms' fees, is below $1 by at least `min_edge_bps`.

Sizing walks both real ask ladders level by level, so the reported
size is actually fillable at the reported prices, and is further
capped by max $ per arb, a max fraction of visible book depth, and
any exposure headroom the caller passes in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal

from .config import FeeConfig, SizingConfig
from .fees import kalshi_fee, polymarket_fee
from .models import BookLevel, Leg, MarketBook, Opportunity, Outcome, Pair, Platform

BPS = Decimal(10000)


@dataclass(frozen=True)
class DirectionQuote:
    """Result of evaluating one direction of a pair."""

    pair_id: str
    direction: str
    opportunity: Opportunity | None
    skip_reason: str | None
    # Edge at top-of-book for one contract, before size caps. Logged even
    # when no tradable opportunity exists, so "edges seen" is meaningful.
    top_edge_bps: Decimal | None


def _marginal_fee(platform: Platform, price: Decimal, fees: FeeConfig) -> Decimal:
    """Unrounded per-contract fee used for level acceptance during the walk."""
    if platform is Platform.KALSHI:
        return fees.kalshi_rate * price * (Decimal(1) - price)
    return fees.polymarket_rate * min(price, Decimal(1) - price)


def _leg_fee(platform: Platform, price: Decimal, size: Decimal, fees: FeeConfig) -> Decimal:
    """Actual (rounded) fee for a chunk. Rounding per level is slightly
    conservative versus per-order rounding, which is the safe direction."""
    if platform is Platform.KALSHI:
        return kalshi_fee(price, size, fees.kalshi_rate)
    return polymarket_fee(price, size, fees.polymarket_rate)


@dataclass
class _Ladder:
    platform: Platform
    market_id: str
    outcome: Outcome
    levels: list[BookLevel]
    max_take: Decimal  # depth cap for this side
    idx: int = 0
    used_in_level: Decimal = Decimal(0)
    taken: Decimal = Decimal(0)
    chunks: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (price, size)

    def current(self) -> BookLevel | None:
        if self.idx >= len(self.levels) or self.taken >= self.max_take:
            return None
        return self.levels[self.idx]

    def available_here(self) -> Decimal:
        lvl = self.levels[self.idx]
        return min(lvl.size - self.used_in_level, self.max_take - self.taken)

    def take(self, qty: Decimal) -> None:
        price = self.levels[self.idx].price
        if self.chunks and self.chunks[-1][0] == price:
            self.chunks[-1] = (price, self.chunks[-1][1] + qty)
        else:
            self.chunks.append((price, qty))
        self.used_in_level += qty
        self.taken += qty
        if self.used_in_level >= self.levels[self.idx].size:
            self.idx += 1
            self.used_in_level = Decimal(0)


def _build_leg(ladder: _Ladder, fees: FeeConfig) -> Leg:
    cost = sum((p * q for p, q in ladder.chunks), Decimal(0))
    fee = sum((_leg_fee(ladder.platform, p, q, fees) for p, q in ladder.chunks), Decimal(0))
    worst_price = ladder.chunks[-1][0]
    return Leg(
        platform=ladder.platform,
        market_id=ladder.market_id,
        outcome=ladder.outcome,
        price=worst_price,
        size=ladder.taken,
        cost=cost,
        fee=fee,
    )


def evaluate_direction(
    pair: Pair,
    direction: str,
    kalshi_book: MarketBook,
    kalshi_outcome: Outcome,
    poly_book: MarketBook,
    poly_outcome: Outcome,
    fees: FeeConfig,
    sizing: SizingConfig,
    min_edge_bps: Decimal,
    exposure_headroom_usd: Decimal | None = None,
) -> DirectionQuote:
    """Evaluate one direction (e.g. Kalshi YES + Polymarket NO)."""
    k_asks = kalshi_book.asks(kalshi_outcome)
    p_asks = poly_book.asks(poly_outcome)
    if not k_asks or not p_asks:
        return DirectionQuote(pair.pair_id, direction, None, "empty_book", None)

    threshold = Decimal(1) - min_edge_bps / BPS

    # Top-of-book edge for one contract (diagnostic, pre-caps).
    top_cost = (
        k_asks[0].price
        + p_asks[0].price
        + _marginal_fee(Platform.KALSHI, k_asks[0].price, fees)
        + _marginal_fee(Platform.POLYMARKET, p_asks[0].price, fees)
    )
    top_edge_bps = (Decimal(1) - top_cost) * BPS

    poly_market_id = (
        pair.polymarket_yes_token_id if poly_outcome is Outcome.YES else pair.polymarket_no_token_id
    )

    k_depth_cap = sizing.max_book_depth_pct * kalshi_book.depth(kalshi_outcome)
    p_depth_cap = sizing.max_book_depth_pct * poly_book.depth(poly_outcome)

    kl = _Ladder(Platform.KALSHI, pair.kalshi_ticker, kalshi_outcome, k_asks, k_depth_cap)
    pl = _Ladder(Platform.POLYMARKET, poly_market_id, poly_outcome, p_asks, p_depth_cap)

    budget = sizing.max_usd_per_arb
    if exposure_headroom_usd is not None:
        budget = min(budget, exposure_headroom_usd)

    spent = Decimal(0)
    while True:
        ka, pa = kl.current(), pl.current()
        if ka is None or pa is None:
            break
        unit_cost = (
            ka.price
            + pa.price
            + _marginal_fee(Platform.KALSHI, ka.price, fees)
            + _marginal_fee(Platform.POLYMARKET, pa.price, fees)
        )
        if unit_cost > threshold:
            break
        qty = min(kl.available_here(), pl.available_here())
        if spent + unit_cost * qty > budget:
            qty = ((budget - spent) / unit_cost).quantize(Decimal(1), rounding=ROUND_FLOOR)
            if qty <= 0:
                break
        kl.take(qty)
        pl.take(qty)
        spent += unit_cost * qty
        if spent >= budget:
            break

    size = kl.taken
    if size <= 0:
        if top_edge_bps > 0:
            reason = "below_min_edge" if top_edge_bps < min_edge_bps else "budget_or_depth_exhausted"
        else:
            reason = "no_edge"
        return DirectionQuote(pair.pair_id, direction, None, reason, top_edge_bps)

    leg_k = _build_leg(kl, fees)
    leg_p = _build_leg(pl, fees)
    total_cost = leg_k.cost + leg_k.fee + leg_p.cost + leg_p.fee
    payout = size  # $1 per matched contract
    edge_bps = (payout - total_cost) / payout * BPS
    if edge_bps < min_edge_bps:
        # Fee rounding on small sizes can eat a marginal edge.
        return DirectionQuote(pair.pair_id, direction, None, "edge_lost_to_fee_rounding", top_edge_bps)

    opp = Opportunity(
        pair_id=pair.pair_id,
        direction=direction,
        legs=(leg_k, leg_p),
        size=size,
        total_cost=total_cost,
        edge_bps=edge_bps,
        est_profit=payout - total_cost,
    )
    return DirectionQuote(pair.pair_id, direction, opp, None, top_edge_bps)


def evaluate_pair(
    pair: Pair,
    kalshi_book: MarketBook,
    poly_book: MarketBook,
    fees: FeeConfig,
    sizing: SizingConfig,
    min_edge_bps: Decimal,
    exposure_headroom_usd: Decimal | None = None,
) -> list[DirectionQuote]:
    """Evaluate both directions: (K yes + P no) and (K no + P yes)."""
    return [
        evaluate_direction(
            pair, "kalshi_yes/poly_no", kalshi_book, Outcome.YES, poly_book, Outcome.NO,
            fees, sizing, min_edge_bps, exposure_headroom_usd,
        ),
        evaluate_direction(
            pair, "kalshi_no/poly_yes", kalshi_book, Outcome.NO, poly_book, Outcome.YES,
            fees, sizing, min_edge_bps, exposure_headroom_usd,
        ),
    ]
