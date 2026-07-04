import sqlite3
from decimal import Decimal

import pytest

from arbot.models import Leg, Outcome, Platform

D = Decimal


def make_leg(platform=Platform.KALSHI, market="KXTEST", outcome=Outcome.YES,
             price=D("0.40"), size=D(100)):
    return Leg(platform=platform, market_id=market, outcome=outcome,
               price=price, size=size, cost=price * size, fee=D(0))


class TestAuditLog:
    def test_audit_rows_are_append_only(self, ledger):
        ledger.audit("test_event", {"k": "v"})
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            ledger.db.execute("UPDATE audit_log SET event = 'tampered'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            ledger.db.execute("DELETE FROM audit_log")

    def test_audit_mirrors_to_jsonl_file(self, ledger):
        ledger.audit("startup", {"pairs": ["a"]})
        content = ledger.audit_log_path.read_text()
        assert '"event": "startup"' in content


class TestPositionsAndPnl:
    def test_open_position_from_fills(self, ledger):
        oid = ledger.record_order(
            opportunity_id=None, leg=make_leg(), action="buy",
            order_type="limit", status="filled",
        )
        ledger.record_fill(oid, D("0.40"), D(100), D("1.00"))
        positions = ledger.open_positions()
        assert len(positions) == 1
        assert positions[0]["net_size"] == 100
        assert positions[0]["net_cost"] == pytest.approx(41.00)  # 40 + 1 fee
        assert ledger.exposure_usd() == D("41")
        assert ledger.exposure_usd("KXTEST") == D("41")
        assert ledger.exposure_usd("OTHER") == D(0)

    def test_sell_reduces_position(self, ledger):
        oid = ledger.record_order(
            opportunity_id=None, leg=make_leg(), action="buy",
            order_type="limit", status="filled",
        )
        ledger.record_fill(oid, D("0.40"), D(100), D(0))
        oid2 = ledger.record_order(
            opportunity_id=None, leg=make_leg(size=D(100)), action="sell",
            order_type="market", status="filled",
        )
        ledger.record_fill(oid2, D("0.35"), D(100), D(0))
        assert ledger.open_positions() == []

    def test_dry_run_orders_excluded_by_default(self, ledger):
        oid = ledger.record_order(
            opportunity_id=None, leg=make_leg(), action="buy",
            order_type="limit", status="dry_run", dry_run=True,
        )
        ledger.record_fill(oid, D("0.40"), D(100), D(0))
        assert ledger.open_positions() == []
        assert len(ledger.open_positions(include_dry_run=True)) == 1

    def test_realized_pnl_today(self, ledger):
        ledger.record_pnl(D("-3.50"), "pair-a", note="unwind")
        ledger.record_pnl(D("1.25"), "pair-b")
        assert ledger.realized_pnl_today() == D("-2.25")


class TestOpportunityQueries:
    def test_edges_and_skips_today(self, ledger):
        from arbot.arb import DirectionQuote

        ledger.record_opportunity(
            DirectionQuote("p1", "kalshi_yes/poly_no", None, "below_min_edge", D(90))
        )
        ledger.record_opportunity(
            DirectionQuote("p1", "kalshi_yes/poly_no", None, "below_min_edge", D(80))
        )
        edges = ledger.edges_today()
        assert edges["seen"] == 2
        assert edges["taken"] == 0
        assert ledger.skip_reasons_today() == [("below_min_edge", 2)]
