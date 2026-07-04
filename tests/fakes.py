"""Scriptable fake exchange clients for executor and paper-trading tests."""

from __future__ import annotations

import itertools
from decimal import Decimal

from arbot.models import MarketBook, OrderResult, OrderStatus, Outcome


class FakeExchange:
    """Implements the ExchangeClient protocol with scripted fill behavior.

    fill_after_polls: how many get_order polls before a limit order fills.
    Use a large number (or float('inf')) to simulate an order that never fills.
    """

    def __init__(
        self,
        platform: str,
        books: dict[str, MarketBook] | None = None,
        fill_after_polls: int = 1,
        market_sell_price: Decimal = Decimal("0.30"),
    ):
        self.platform = platform
        self.books = books or {}
        self.fill_after_polls = fill_after_polls
        self.market_sell_price = market_sell_price
        self._seq = itertools.count(1)
        self.orders: dict[str, dict] = {}
        self.canceled: list[str] = []
        self.market_sells: list[tuple[str, Outcome, Decimal]] = []

    async def get_book(self, market_id: str) -> MarketBook:
        return self.books[market_id]

    async def place_limit(self, market_id, outcome, price, size) -> OrderResult:
        oid = f"{self.platform}-{next(self._seq)}"
        self.orders[oid] = {"price": price, "size": size, "polls": 0}
        return OrderResult(order_id=oid, status=OrderStatus.OPEN)

    async def get_order(self, order_id) -> OrderResult:
        o = self.orders[order_id]
        o["polls"] += 1
        if o["polls"] >= self.fill_after_polls:
            return OrderResult(
                order_id=order_id, status=OrderStatus.FILLED,
                filled_size=o["size"], avg_price=o["price"],
            )
        return OrderResult(order_id=order_id, status=OrderStatus.OPEN)

    async def cancel(self, order_id) -> None:
        self.canceled.append(order_id)

    async def place_market_sell(self, market_id, outcome, size) -> OrderResult:
        self.market_sells.append((market_id, outcome, size))
        oid = f"{self.platform}-mkt-{next(self._seq)}"
        return OrderResult(
            order_id=oid, status=OrderStatus.FILLED,
            filled_size=size, avg_price=self.market_sell_price,
        )
