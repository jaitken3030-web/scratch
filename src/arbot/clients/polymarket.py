"""Polymarket CLOB client.

Market data (order books) is plain REST against the CLOB. Placing live
orders requires EIP-712 wallet signing; rather than reimplement that,
live trading delegates to the official `py-clob-client` package
(installed via the `polymarket-live` extra). In dry-run mode — the
default — the signing client is never needed.

Book semantics: each binary market has two ERC-1155 tokens (YES / NO),
each with its own book of bids and asks. `get_book(token_id)` returns a
MarketBook where the requested token's ladder is stored on the YES side;
callers address legs by token id, so pass the NO token id to buy NO.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from ..models import BookLevel, MarketBook, OrderResult, OrderStatus, Outcome, Platform

_STATUS_MAP = {
    "live": OrderStatus.OPEN,
    "matched": OrderStatus.FILLED,
    "delayed": OrderStatus.PENDING,
    "unmatched": OrderStatus.OPEN,
    "canceled": OrderStatus.CANCELED,
}


class PolymarketClient:
    platform = "polymarket"

    def __init__(
        self,
        clob_url: str,
        gamma_url: str = "https://gamma-api.polymarket.com",
        api_key: str = "",
        api_secret: str = "",
        api_passphrase: str = "",
        private_key: str = "",
        http: httpx.AsyncClient | None = None,
    ):
        self.clob_url = clob_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.private_key = private_key
        self.http = http or httpx.AsyncClient(timeout=10)
        self._signer = None  # lazy py_clob_client instance for live mode

    # ---- market data ----

    @staticmethod
    def parse_book(token_id: str, payload: dict) -> MarketBook:
        """Parse a GET /book?token_id=... response.

        Payload: {"bids": [{"price": "0.45", "size": "100"}, ...],
                  "asks": [...]} — one ladder for this token.
        """

        def to_levels(raw: list[dict], descending: bool) -> list[BookLevel]:
            levels = [BookLevel(Decimal(str(l["price"])), Decimal(str(l["size"]))) for l in raw]
            return sorted(levels, key=lambda l: l.price, reverse=descending)

        return MarketBook(
            platform=Platform.POLYMARKET,
            market_id=token_id,
            yes_asks=to_levels(payload.get("asks") or [], descending=False),
            yes_bids=to_levels(payload.get("bids") or [], descending=True),
        )

    async def get_book(self, market_id: str) -> MarketBook:
        resp = await self.http.get(f"{self.clob_url}/book", params={"token_id": market_id})
        resp.raise_for_status()
        return self.parse_book(market_id, resp.json())

    async def list_markets(self, limit: int = 200) -> list[dict]:
        """Sampling of active CLOB markets (for the pair suggester)."""
        resp = await self.http.get(f"{self.clob_url}/sampling-markets", params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data if isinstance(data, list) else [])

    # ---- trading (live mode only; requires py-clob-client) ----

    def _get_signer(self):
        if self._signer is None:
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
            except ImportError as e:
                raise RuntimeError(
                    "Live Polymarket trading requires py-clob-client: "
                    "pip install 'arbot[polymarket-live]'"
                ) from e
            creds = ApiCreds(
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
            self._signer = ClobClient(
                self.clob_url, key=self.private_key, chain_id=137, creds=creds
            )
        return self._signer

    async def place_limit(
        self, market_id: str, outcome: Outcome, price: Decimal, size: Decimal
    ) -> OrderResult:
        import asyncio

        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        signer = self._get_signer()
        args = OrderArgs(token_id=market_id, price=float(price), size=float(size), side=BUY)
        # py_clob_client is synchronous; run it off the event loop.
        resp = await asyncio.to_thread(lambda: signer.post_order(signer.create_order(args)))
        return OrderResult(
            order_id=str(resp.get("orderID", "")),
            status=OrderStatus.OPEN if resp.get("success") else OrderStatus.REJECTED,
        )

    async def place_market_sell(
        self, market_id: str, outcome: Outcome, size: Decimal
    ) -> OrderResult:
        import asyncio

        from py_clob_client.clob_types import MarketOrderArgs
        from py_clob_client.order_builder.constants import SELL

        signer = self._get_signer()
        args = MarketOrderArgs(token_id=market_id, amount=float(size), side=SELL)
        resp = await asyncio.to_thread(
            lambda: signer.post_order(signer.create_market_order(args))
        )
        return OrderResult(
            order_id=str(resp.get("orderID", "")),
            status=OrderStatus.FILLED if resp.get("success") else OrderStatus.REJECTED,
        )

    async def cancel(self, order_id: str) -> None:
        import asyncio

        signer = self._get_signer()
        await asyncio.to_thread(signer.cancel, order_id)

    async def get_order(self, order_id: str) -> OrderResult:
        import asyncio

        signer = self._get_signer()
        data = await asyncio.to_thread(signer.get_order, order_id)
        size_matched = Decimal(str(data.get("size_matched", 0)))
        original = Decimal(str(data.get("original_size", 0)))
        status = _STATUS_MAP.get(str(data.get("status", "")).lower(), OrderStatus.PENDING)
        if status is OrderStatus.OPEN and 0 < size_matched < original:
            status = OrderStatus.PARTIAL
        return OrderResult(
            order_id=order_id,
            status=status,
            filled_size=size_matched,
            avg_price=Decimal(str(data.get("price", 0))),
        )

    async def close(self) -> None:
        await self.http.aclose()
