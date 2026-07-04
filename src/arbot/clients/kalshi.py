"""Kalshi trade API v2 client (REST polling).

Auth: request signing with an RSA private key (PSS/SHA-256) per Kalshi's
API-key scheme — sign `timestamp_ms + METHOD + path` and send the
KALSHI-ACCESS-* headers. Public market data works unsigned.

Price normalization: Kalshi quotes in cents; we convert to Decimal
dollars. The orderbook endpoint returns YES bids and NO bids; asks are
derived (ask YES at p == bid NO at 1-p).
"""

from __future__ import annotations

import base64
import datetime as dt
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

from ..models import BookLevel, MarketBook, OrderResult, OrderStatus, Outcome, Platform

CENTS = Decimal(100)

_STATUS_MAP = {
    "resting": OrderStatus.OPEN,
    "pending": OrderStatus.PENDING,
    "executed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
}


def _load_private_key(path: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


class KalshiClient:
    platform = "kalshi"

    def __init__(
        self,
        base_url: str,
        api_key_id: str = "",
        private_key_path: str = "",
        http: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key = _load_private_key(private_key_path) if private_key_path else None
        self.http = http or httpx.AsyncClient(timeout=10)

    # ---- auth ----

    def _headers(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        # Signature covers the path without query string.
        url_path = "/trade-api/v2" + path
        headers = self._headers(method, url_path)
        resp = await self.http.request(method, self.base_url + path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # ---- market data ----

    @staticmethod
    def parse_orderbook(ticker: str, payload: dict) -> MarketBook:
        """Parse a GET /markets/{ticker}/orderbook response.

        Payload shape: {"orderbook": {"yes": [[price_cents, count], ...],
        "no": [[price_cents, count], ...]}} where both lists are BIDS.
        """
        ob = payload.get("orderbook") or {}
        yes_bids_raw = ob.get("yes") or []
        no_bids_raw = ob.get("no") or []

        def to_levels(raw, descending: bool) -> list[BookLevel]:
            levels = [BookLevel(Decimal(p) / CENTS, Decimal(q)) for p, q in raw]
            return sorted(levels, key=lambda l: l.price, reverse=descending)

        yes_bids = to_levels(yes_bids_raw, descending=True)
        no_bids = to_levels(no_bids_raw, descending=True)
        # A resting NO bid at price p is willingness to sell YES at 1-p.
        yes_asks = sorted(
            (BookLevel(Decimal(1) - l.price, l.size) for l in no_bids), key=lambda l: l.price
        )
        no_asks = sorted(
            (BookLevel(Decimal(1) - l.price, l.size) for l in yes_bids), key=lambda l: l.price
        )
        return MarketBook(
            platform=Platform.KALSHI,
            market_id=ticker,
            yes_asks=yes_asks,
            yes_bids=yes_bids,
            no_asks=no_asks,
            no_bids=no_bids,
        )

    async def get_book(self, market_id: str) -> MarketBook:
        data = await self._request("GET", f"/markets/{market_id}/orderbook")
        return self.parse_orderbook(market_id, data)

    async def list_markets(self, status: str = "open", limit: int = 200) -> list[dict]:
        data = await self._request("GET", "/markets", params={"status": status, "limit": limit})
        return data.get("markets", [])

    # ---- trading ----

    async def place_limit(
        self, market_id: str, outcome: Outcome, price: Decimal, size: Decimal
    ) -> OrderResult:
        body = {
            "ticker": market_id,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": outcome.value,
            "type": "limit",
            "count": int(size),
            f"{outcome.value}_price": int(price * CENTS),
        }
        data = await self._request("POST", "/portfolio/orders", json=body)
        return self._parse_order(data.get("order", {}))

    async def place_market_sell(
        self, market_id: str, outcome: Outcome, size: Decimal
    ) -> OrderResult:
        body = {
            "ticker": market_id,
            "client_order_id": str(uuid.uuid4()),
            "action": "sell",
            "side": outcome.value,
            "type": "market",
            "count": int(size),
        }
        data = await self._request("POST", "/portfolio/orders", json=body)
        return self._parse_order(data.get("order", {}))

    async def cancel(self, order_id: str) -> None:
        await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_order(self, order_id: str) -> OrderResult:
        data = await self._request("GET", f"/portfolio/orders/{order_id}")
        return self._parse_order(data.get("order", {}))

    @staticmethod
    def _parse_order(order: dict) -> OrderResult:
        count = Decimal(order.get("initial_count") or order.get("count") or 0)
        remaining = Decimal(order.get("remaining_count") or 0)
        filled = count - remaining
        status = _STATUS_MAP.get(order.get("status", ""), OrderStatus.PENDING)
        if status is OrderStatus.OPEN and filled > 0:
            status = OrderStatus.PARTIAL
        price_cents = order.get("yes_price") or order.get("no_price") or 0
        return OrderResult(
            order_id=order.get("order_id", ""),
            status=status,
            filled_size=filled,
            avg_price=Decimal(price_cents) / CENTS,
        )

    async def close(self) -> None:
        await self.http.aclose()
