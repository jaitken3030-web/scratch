from decimal import Decimal

import pytest

from arbot.config import ExecutionConfig, FeeConfig, SafetyConfig, SizingConfig
from arbot.executor import ArbExecutor
from arbot.models import Leg, Opportunity, Outcome, Platform
from arbot.safety import SafetyManager

from .fakes import FakeExchange

D = Decimal

NO_FEES = FeeConfig(kalshi_rate=D(0), polymarket_rate=D(0))


def make_opportunity(size=D(50)) -> Opportunity:
    leg_k = Leg(
        platform=Platform.KALSHI, market_id="KXTEST", outcome=Outcome.YES,
        price=D("0.40"), size=size, cost=D("0.40") * size, fee=D(0),
    )
    leg_p = Leg(
        platform=Platform.POLYMARKET, market_id="222", outcome=Outcome.NO,
        price=D("0.55"), size=size, cost=D("0.55") * size, fee=D(0),
    )
    return Opportunity(
        pair_id="test-pair", direction="kalshi_yes/poly_no", legs=(leg_k, leg_p),
        size=size, total_cost=D("47.50"), edge_bps=D(500), est_profit=D("2.50"),
    )


@pytest.fixture
def safety(ledger, tmp_path):
    return SafetyManager(
        SafetyConfig(kill_switch_file=str(tmp_path / "KILL")), SizingConfig(), ledger
    )


def build_executor(ledger, safety, kalshi, poly, dry_run=False, timeout=0.2):
    cfg = ExecutionConfig(dry_run=dry_run, second_leg_timeout_s=timeout, poll_fill_interval_s=0.01)
    return ArbExecutor(
        {"kalshi": kalshi, "polymarket": poly}, ledger, safety, cfg, NO_FEES
    )


# Kalshi book is thinner in these depth maps, so kalshi goes first.
DEPTHS = {("kalshi", "KXTEST"): D(100), ("polymarket", "222"): D(1000)}


class TestDryRun:
    async def test_dry_run_records_but_never_trades(self, ledger, safety):
        kalshi, poly = FakeExchange("kalshi"), FakeExchange("polymarket")
        ex = build_executor(ledger, safety, kalshi, poly, dry_run=True)
        opp = make_opportunity()
        opp_id = ledger.record_opportunity(opp, taken=True)

        report = await ex.execute(opp, opp_id, depths=DEPTHS)

        assert report.executed and report.dry_run
        assert report.hedged_size == 50
        assert kalshi.orders == {} and poly.orders == {}
        orders = ledger.db.execute("SELECT * FROM orders").fetchall()
        assert len(orders) == 2
        assert all(o["dry_run"] == 1 and o["status"] == "dry_run" for o in orders)
        audit = ledger.db.execute(
            "SELECT * FROM audit_log WHERE event='dry_run_arb'"
        ).fetchall()
        assert len(audit) == 1


class TestLiveHappyPath:
    async def test_both_legs_fill(self, ledger, safety):
        kalshi = FakeExchange("kalshi", fill_after_polls=1)
        poly = FakeExchange("polymarket", fill_after_polls=1)
        ex = build_executor(ledger, safety, kalshi, poly)
        opp = make_opportunity()
        opp_id = ledger.record_opportunity(opp, taken=True)

        report = await ex.execute(opp, opp_id, depths=DEPTHS)

        assert report.executed
        assert report.hedged_size == 50
        assert report.incident is None
        fills = ledger.db.execute("SELECT * FROM fills").fetchall()
        assert len(fills) == 2
        # thinner (kalshi) leg placed first
        assert list(kalshi.orders) == ["kalshi-1"]
        audit = ledger.db.execute(
            "SELECT * FROM audit_log WHERE event='arb_executed'"
        ).fetchall()
        assert len(audit) == 1

    async def test_leg_ordering_prefers_thinner_book(self, ledger, safety):
        kalshi = FakeExchange("kalshi")
        poly = FakeExchange("polymarket")
        ex = build_executor(ledger, safety, kalshi, poly)
        opp = make_opportunity()
        first, second = ex._order_legs(
            opp, {("kalshi", "KXTEST"): D(5000), ("polymarket", "222"): D(10)}
        )
        assert first.platform is Platform.POLYMARKET

    async def test_leg_ordering_falls_back_to_cheaper(self, ledger, safety):
        ex = build_executor(ledger, safety, FakeExchange("kalshi"), FakeExchange("polymarket"))
        first, _ = ex._order_legs(make_opportunity(), None)
        assert first.price == D("0.40")


class TestLeg2TimeoutUnwind:
    async def test_unwind_and_incident(self, ledger, safety):
        kalshi = FakeExchange("kalshi", fill_after_polls=1)
        poly = FakeExchange("polymarket", fill_after_polls=10**9)  # never fills
        ex = build_executor(ledger, safety, kalshi, poly, timeout=0.05)
        opp = make_opportunity()
        opp_id = ledger.record_opportunity(opp, taken=True)

        report = await ex.execute(opp, opp_id, depths=DEPTHS)

        assert not report.executed
        assert report.incident == "leg2_timeout_unwound"
        assert report.unwound_size == 50
        # leg2 canceled, leg1 unwound via market sell
        assert len(poly.canceled) == 1
        assert kalshi.market_sells == [("KXTEST", Outcome.YES, D(50))]
        # unhedged alert + incident in the audit log
        events = {
            r["event"]
            for r in ledger.db.execute("SELECT event FROM audit_log").fetchall()
        }
        assert "alert" in events and "incident_leg2_timeout" in events
        # realized loss recorded: bought 0.40, market-sold at 0.30
        assert ledger.realized_pnl_today() == D("-5.00")  # (0.30-0.40)*50

    async def test_leg1_no_fill_cancels_without_unwind(self, ledger, safety):
        kalshi = FakeExchange("kalshi", fill_after_polls=10**9)
        poly = FakeExchange("polymarket")
        ex = build_executor(ledger, safety, kalshi, poly, timeout=0.05)
        opp = make_opportunity()
        opp_id = ledger.record_opportunity(opp, taken=True)

        report = await ex.execute(opp, opp_id, depths=DEPTHS)

        assert not report.executed
        assert report.incident == "leg1_no_fill"
        assert len(kalshi.canceled) == 1
        assert kalshi.market_sells == [] and poly.orders == {}


class TestSafetyGate:
    async def test_kill_switch_blocks_live_execution(self, ledger, safety, tmp_path):
        (tmp_path / "KILL").touch()
        kalshi, poly = FakeExchange("kalshi"), FakeExchange("polymarket")
        ex = build_executor(ledger, safety, kalshi, poly)
        opp = make_opportunity()
        opp_id = ledger.record_opportunity(opp, taken=True)

        report = await ex.execute(opp, opp_id, depths=DEPTHS)

        assert not report.executed
        assert "blocked" in report.incident
        assert kalshi.orders == {} and poly.orders == {}
