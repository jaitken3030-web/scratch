"""Data layer + orchestration.

BookStore polls order books for every whitelisted pair and keeps the
latest best bid/ask + depth. Scanner runs the evaluate -> record ->
execute cycle against the store each tick.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from .arb import evaluate_pair
from .config import Config
from .executor import ArbExecutor
from .ledger import Ledger
from .models import MarketBook, Pair, Platform
from .safety import SafetyManager

log = logging.getLogger("arbot.scanner")


class BookStore:
    """Latest order books, keyed by (platform, market_id)."""

    def __init__(self, kalshi_client, poly_client):
        self.kalshi = kalshi_client
        self.poly = poly_client
        self.books: dict[tuple[str, str], MarketBook] = {}

    async def refresh_pair(self, pair: Pair) -> None:
        k_book, yes_book, no_book = await asyncio.gather(
            self.kalshi.get_book(pair.kalshi_ticker),
            self.poly.get_book(pair.polymarket_yes_token_id),
            self.poly.get_book(pair.polymarket_no_token_id),
        )
        self.books[("kalshi", pair.kalshi_ticker)] = k_book
        self.books[("polymarket", pair.polymarket_yes_token_id)] = yes_book
        self.books[("polymarket", pair.polymarket_no_token_id)] = no_book

    def kalshi_book(self, pair: Pair) -> MarketBook | None:
        return self.books.get(("kalshi", pair.kalshi_ticker))

    def poly_book(self, pair: Pair) -> MarketBook | None:
        """Combine the YES-token and NO-token ladders into one MarketBook."""
        yes = self.books.get(("polymarket", pair.polymarket_yes_token_id))
        no = self.books.get(("polymarket", pair.polymarket_no_token_id))
        if yes is None or no is None:
            return None
        return MarketBook(
            platform=Platform.POLYMARKET,
            market_id=pair.polymarket_condition_id,
            yes_asks=yes.yes_asks,
            yes_bids=yes.yes_bids,
            no_asks=no.yes_asks,   # NO token's own ladder
            no_bids=no.yes_bids,
            ts=min(yes.ts, no.ts),
        )

    def depths(self, pair: Pair) -> dict[tuple[str, str], Decimal]:
        """Ask-side depth per leg market, for thinner-leg-first ordering."""
        out: dict[tuple[str, str], Decimal] = {}
        k = self.kalshi_book(pair)
        if k:
            from .models import Outcome

            out[("kalshi", pair.kalshi_ticker)] = k.depth(Outcome.YES)
        for token in (pair.polymarket_yes_token_id, pair.polymarket_no_token_id):
            b = self.books.get(("polymarket", token))
            if b:
                from .models import Outcome

                out[("polymarket", token)] = b.depth(Outcome.YES)
        return out


class Scanner:
    def __init__(
        self,
        cfg: Config,
        pairs: list[Pair],
        store: BookStore,
        executor: ArbExecutor,
        ledger: Ledger,
        safety: SafetyManager,
    ):
        self.cfg = cfg
        self.pairs = pairs
        self.store = store
        self.executor = executor
        self.ledger = ledger
        self.safety = safety

    async def scan_pair(self, pair: Pair) -> None:
        try:
            await self.store.refresh_pair(pair)
        except Exception as e:
            log.warning("book refresh failed for %s: %s", pair.pair_id, e)
            return

        k_book = self.store.kalshi_book(pair)
        p_book = self.store.poly_book(pair)
        if k_book is None or p_book is None:
            return

        headroom = self.safety.pair_headroom_usd(
            pair.kalshi_ticker,
            (pair.polymarket_yes_token_id, pair.polymarket_no_token_id),
        )
        quotes = evaluate_pair(
            pair, k_book, p_book,
            fees=self.cfg.fees, sizing=self.cfg.sizing,
            min_edge_bps=self.cfg.min_edge_bps,
            exposure_headroom_usd=headroom,
        )
        for q in quotes:
            if q.opportunity is None:
                # Only persist interesting skips (positive top-of-book edge),
                # otherwise the ledger fills with "no_edge" noise.
                if q.top_edge_bps is not None and q.top_edge_bps > 0:
                    self.ledger.record_opportunity(q)
                continue

            opp = q.opportunity
            if headroom <= 0:
                self.ledger.record_opportunity(q, skip_reason="exposure_cap")
                continue
            allowed, why = await self.safety.trading_allowed()
            if not allowed:
                self.ledger.record_opportunity(q, skip_reason=f"halted:{why}")
                continue

            opp_id = self.ledger.record_opportunity(opp, taken=True)
            log.info(
                "ARB %s %s: size=%s edge=%sbps est_profit=$%s",
                opp.pair_id, opp.direction, opp.size, round(opp.edge_bps, 1), opp.est_profit,
            )
            await self.executor.execute(opp, opp_id, depths=self.store.depths(pair))

    async def run_forever(self) -> None:
        log.info(
            "scanner starting: %d pairs, min_edge=%sbps, dry_run=%s",
            len(self.pairs), self.cfg.min_edge_bps, self.cfg.execution.dry_run,
        )
        while True:
            allowed, why = await self.safety.trading_allowed()
            if not allowed:
                log.warning("trading halted: %s", why)
                await asyncio.sleep(max(self.cfg.poll_interval_s, 5))
                continue
            await asyncio.gather(*(self.scan_pair(p) for p in self.pairs))
            await asyncio.sleep(self.cfg.poll_interval_s)
