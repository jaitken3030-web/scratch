"""Order execution.

dry_run=True (default): log intended trades and record them in the
ledger; nothing touches an exchange.

Live: place the thinner/cheaper leg first as a limit order. Once it
fills, place the second leg. If the second leg does not fill within
`second_leg_timeout_s`, cancel it, unwind the first leg's fill with a
market order, alert, and write an incident to the audit log.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from .config import ExecutionConfig, FeeConfig
from .fees import fee_for
from .ledger import Ledger
from .models import Leg, Opportunity, OrderResult, OrderStatus
from .safety import SafetyManager

log = logging.getLogger("arbot.executor")

_TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED}


@dataclass
class ExecutionReport:
    opportunity_id: int
    executed: bool
    dry_run: bool
    hedged_size: Decimal = Decimal(0)
    unwound_size: Decimal = Decimal(0)
    incident: str | None = None
    order_ids: list[int] = field(default_factory=list)


class ArbExecutor:
    def __init__(
        self,
        clients: dict[str, object],  # platform -> ExchangeClient
        ledger: Ledger,
        safety: SafetyManager,
        exec_cfg: ExecutionConfig,
        fees: FeeConfig,
    ):
        self.clients = clients
        self.ledger = ledger
        self.safety = safety
        self.cfg = exec_cfg
        self.fees = fees

    def _order_legs(
        self, opp: Opportunity, depths: dict[tuple[str, str], Decimal] | None
    ) -> tuple[Leg, Leg]:
        """Thinner book first; tie-break on cheaper price."""

        def key(leg: Leg):
            depth = None
            if depths is not None:
                depth = depths.get((leg.platform.value, leg.market_id))
            return (depth if depth is not None else Decimal("Infinity"), leg.price)

        first, second = sorted(opp.legs, key=key)
        return first, second

    async def execute(
        self,
        opp: Opportunity,
        opportunity_id: int,
        depths: dict[tuple[str, str], Decimal] | None = None,
    ) -> ExecutionReport:
        first, second = self._order_legs(opp, depths)
        report = ExecutionReport(opportunity_id=opportunity_id, executed=False, dry_run=self.cfg.dry_run)

        if self.cfg.dry_run:
            for leg in (first, second):
                oid = self.ledger.record_order(
                    opportunity_id=opportunity_id, leg=leg, action="buy",
                    order_type="limit", status="dry_run", dry_run=True,
                )
                report.order_ids.append(oid)
                # Simulated fill at the computed level price, so paper
                # positions/P&L are visible via `arbot report --include-dry-run`.
                self.ledger.record_fill(oid, leg.price, leg.size, leg.fee)
                log.info(
                    "[DRY RUN] would buy %s %s on %s: %s @ %s (fee %s)",
                    leg.size, leg.outcome.value, leg.platform.value,
                    leg.market_id, leg.price, leg.fee,
                )
            self.ledger.audit(
                "dry_run_arb",
                {
                    "opportunity_id": opportunity_id, "pair_id": opp.pair_id,
                    "direction": opp.direction, "size": opp.size,
                    "edge_bps": opp.edge_bps, "est_profit": opp.est_profit,
                },
            )
            report.executed = True
            report.hedged_size = opp.size
            return report

        allowed, why = await self.safety.trading_allowed()
        if not allowed:
            log.warning("execution blocked: %s", why)
            report.incident = f"blocked: {why}"
            return report

        # ---- leg 1 ----
        client1 = self.clients[first.platform.value]
        oid1 = self.ledger.record_order(
            opportunity_id=opportunity_id, leg=first, action="buy",
            order_type="limit", status="submitting",
        )
        report.order_ids.append(oid1)
        res1 = await client1.place_limit(first.market_id, first.outcome, first.price, first.size)
        self.ledger.update_order(oid1, res1.status.value, res1.order_id)
        res1 = await self._await_fill(client1, res1, self.cfg.second_leg_timeout_s)
        filled1 = res1.filled_size

        if filled1 <= 0:
            await self._safe_cancel(client1, res1.order_id)
            self.ledger.update_order(oid1, "canceled")
            self.ledger.audit("leg1_no_fill", {"opportunity_id": opportunity_id})
            report.incident = "leg1_no_fill"
            return report

        fee1 = fee_for(
            first.platform.value, res1.avg_price or first.price, filled1,
            kalshi_rate=self.fees.kalshi_rate, polymarket_rate=self.fees.polymarket_rate,
        )
        self.ledger.record_fill(oid1, res1.avg_price or first.price, filled1, fee1)
        if res1.status is not OrderStatus.FILLED:
            # Partially filled by deadline: cancel the rest, hedge what we have.
            await self._safe_cancel(client1, res1.order_id)
            self.ledger.update_order(oid1, "partial")
        else:
            self.ledger.update_order(oid1, "filled")

        # ---- leg 2 (hedge) ----
        hedge_qty = filled1
        client2 = self.clients[second.platform.value]
        leg2 = Leg(
            platform=second.platform, market_id=second.market_id, outcome=second.outcome,
            price=second.price, size=hedge_qty,
            cost=second.price * hedge_qty, fee=Decimal(0),
        )
        oid2 = self.ledger.record_order(
            opportunity_id=opportunity_id, leg=leg2, action="buy",
            order_type="limit", status="submitting",
        )
        report.order_ids.append(oid2)
        res2 = await client2.place_limit(leg2.market_id, leg2.outcome, leg2.price, hedge_qty)
        self.ledger.update_order(oid2, res2.status.value, res2.order_id)
        res2 = await self._await_fill(client2, res2, self.cfg.second_leg_timeout_s)
        filled2 = res2.filled_size

        if filled2 > 0:
            fee2 = fee_for(
                second.platform.value, res2.avg_price or leg2.price, filled2,
                kalshi_rate=self.fees.kalshi_rate, polymarket_rate=self.fees.polymarket_rate,
            )
            self.ledger.record_fill(oid2, res2.avg_price or leg2.price, filled2, fee2)

        if filled2 >= hedge_qty:
            self.ledger.update_order(oid2, "filled")
            report.executed = True
            report.hedged_size = hedge_qty
            self.ledger.audit(
                "arb_executed",
                {"opportunity_id": opportunity_id, "pair_id": opp.pair_id, "size": hedge_qty},
            )
            return report

        # ---- second leg failed to fill in time: cancel + unwind ----
        await self._safe_cancel(client2, res2.order_id)
        self.ledger.update_order(oid2, "canceled")
        unhedged = hedge_qty - filled2
        await self.safety.alert_unhedged(
            opp.pair_id,
            {
                "opportunity_id": opportunity_id,
                "unhedged_size": str(unhedged),
                "leg": f"{first.platform.value}:{first.market_id}:{first.outcome.value}",
            },
        )

        unwind_leg = Leg(
            platform=first.platform, market_id=first.market_id, outcome=first.outcome,
            price=Decimal(0), size=unhedged, cost=Decimal(0), fee=Decimal(0),
        )
        oid3 = self.ledger.record_order(
            opportunity_id=opportunity_id, leg=unwind_leg, action="sell",
            order_type="market", status="submitting",
        )
        report.order_ids.append(oid3)
        try:
            res3 = await client1.place_market_sell(first.market_id, first.outcome, unhedged)
            self.ledger.update_order(oid3, res3.status.value, res3.order_id)
            if res3.filled_size > 0:
                fee3 = fee_for(
                    first.platform.value, res3.avg_price, res3.filled_size,
                    kalshi_rate=self.fees.kalshi_rate, polymarket_rate=self.fees.polymarket_rate,
                )
                self.ledger.record_fill(oid3, res3.avg_price, res3.filled_size, fee3)
                # Realized loss on the round trip.
                buy_price = res1.avg_price or first.price
                loss = (res3.avg_price - buy_price) * res3.filled_size - fee1 - fee3
                self.ledger.record_pnl(loss, opp.pair_id, note="unwind after leg2 timeout")
            report.unwound_size = res3.filled_size
        except Exception as e:  # unwind itself failed — loudest possible alert
            self.ledger.update_order(oid3, "failed")
            await self.safety.alert(
                f"UNWIND FAILED on {first.platform.value}:{first.market_id} — "
                f"manual intervention required ({e})",
                opportunity_id=opportunity_id,
            )

        report.hedged_size = filled2
        report.incident = "leg2_timeout_unwound"
        self.ledger.audit(
            "incident_leg2_timeout",
            {
                "opportunity_id": opportunity_id, "pair_id": opp.pair_id,
                "hedged": str(filled2), "unwound": str(report.unwound_size),
            },
        )
        return report

    async def _await_fill(self, client, res: OrderResult, timeout_s: float) -> OrderResult:
        """Poll order status until filled/terminal or timeout."""
        if res.status in _TERMINAL:
            return res
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(self.cfg.poll_fill_interval_s)
            res = await client.get_order(res.order_id)
            if res.status in _TERMINAL:
                return res
        return res

    @staticmethod
    async def _safe_cancel(client, order_id: str) -> None:
        if not order_id:
            return
        try:
            await client.cancel(order_id)
        except Exception:
            log.exception("cancel failed for order %s", order_id)
