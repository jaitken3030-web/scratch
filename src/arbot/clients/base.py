"""Exchange client interface. The executor and tests depend only on this."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..models import MarketBook, OrderResult, Outcome


@runtime_checkable
class ExchangeClient(Protocol):
    platform: str

    async def get_book(self, market_id: str) -> MarketBook:
        """Fetch the current order book for one market/token."""
        ...

    async def place_limit(
        self, market_id: str, outcome: Outcome, price: Decimal, size: Decimal
    ) -> OrderResult:
        """Place a buy limit order. Returns immediately with order id/status."""
        ...

    async def place_market_sell(
        self, market_id: str, outcome: Outcome, size: Decimal
    ) -> OrderResult:
        """Unwind: sell `size` contracts at market."""
        ...

    async def cancel(self, order_id: str) -> None:
        ...

    async def get_order(self, order_id: str) -> OrderResult:
        ...
