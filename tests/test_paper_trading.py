"""Paper-trading integration test.

Recorded API responses (tests/fixtures/*.json) are parsed by the real
client parsers, fed through the real BookStore -> arb math -> Scanner ->
dry-run executor -> ledger pipeline, and the report is rendered at the
end. No network anywhere.
"""

from decimal import Decimal

import pytest

from arbot.clients.kalshi import KalshiClient
from arbot.clients.polymarket import PolymarketClient
from arbot.config import Config, ExecutionConfig, FeeConfig, SafetyConfig, SizingConfig
from arbot.executor import ArbExecutor
from arbot.models import Outcome, Pair, Platform
from arbot.report import build_report
from arbot.safety import SafetyManager
from arbot.scanner import BookStore, Scanner

from .conftest import load_fixture
from .fakes import FakeExchange

D = Decimal

PAIR = Pair(
    pair_id="test-pair",
    kalshi_ticker="KXTEST",
    polymarket_condition_id="0xtestcondition",
    polymarket_yes_token_id="111",
    polymarket_no_token_id="222",
    notes="fixture pair",
)


@pytest.fixture
def recorded_books():
    k_book = KalshiClient.parse_orderbook(
        "KXTEST", load_fixture("kalshi_orderbook_KXTEST.json")
    )
    yes_book = PolymarketClient.parse_book("111", load_fixture("poly_book_yes_token.json"))
    no_book = PolymarketClient.parse_book("222", load_fixture("poly_book_no_token.json"))
    return k_book, yes_book, no_book


class TestRecordedResponseParsing:
    def test_kalshi_orderbook_parsing(self, recorded_books):
        k_book, _, _ = recorded_books
        # NO bids at 62c/60c become YES asks at 38c/40c (best first)
        assert [(l.price, l.size) for l in k_book.yes_asks] == [
            (D("0.38"), D(500)), (D("0.40"), D(300)),
        ]
        # YES bids at 35c/33c become NO asks at 65c/67c
        assert k_book.no_asks[0].price == D("0.65")
        assert k_book.best_bid(Outcome.YES).price == D("0.35")
        assert k_book.depth(Outcome.YES) == D(800)

    def test_polymarket_book_parsing(self, recorded_books):
        _, yes_book, no_book = recorded_books
        assert yes_book.best_ask(Outcome.YES).price == D("0.44")
        assert no_book.best_ask(Outcome.YES).price == D("0.55")
        assert no_book.depth(Outcome.YES) == D(500)


@pytest.fixture
def paper_env(tmp_path, ledger, recorded_books):
    k_book, yes_book, no_book = recorded_books
    kalshi = FakeExchange("kalshi", books={"KXTEST": k_book})
    poly = FakeExchange("polymarket", books={"111": yes_book, "222": no_book})

    cfg = Config(
        min_edge_bps=D(150),
        fees=FeeConfig(kalshi_rate=D("0.07"), polymarket_rate=D(0)),
        sizing=SizingConfig(
            max_usd_per_arb=D(100),
            max_total_exposure_usd=D(500),
            max_book_depth_pct=D("0.25"),
        ),
        safety=SafetyConfig(kill_switch_file=str(tmp_path / "KILL")),
        execution=ExecutionConfig(dry_run=True),
    )
    safety = SafetyManager(cfg.safety, cfg.sizing, ledger)
    executor = ArbExecutor(
        {"kalshi": kalshi, "polymarket": poly}, ledger, safety, cfg.execution, cfg.fees
    )
    store = BookStore(kalshi, poly)
    scanner = Scanner(cfg, [PAIR], store, executor, ledger, safety)
    return scanner, ledger, kalshi, poly


class TestPaperTradingPipeline:
    async def test_scan_finds_and_paper_trades_the_arb(self, paper_env):
        scanner, ledger, kalshi, poly = paper_env

        await scanner.scan_pair(PAIR)

        # The fixture books contain one arb: kalshi YES @0.38 + poly NO @0.55.
        opps = ledger.db.execute("SELECT * FROM opportunities").fetchall()
        taken = [o for o in opps if o["taken"]]
        assert len(taken) == 1
        opp = taken[0]
        assert opp["pair_id"] == "test-pair"
        assert opp["direction"] == "kalshi_yes/poly_no"
        # unit cost 0.38+0.55+kalshi fee(0.0164920) = 0.946492 -> $100 budget -> 105
        assert D(opp["size"]) == 105
        assert D(opp["edge_bps"]) > D(150)

        # Dry-run: intended orders recorded, nothing hit the (fake) exchanges.
        orders = ledger.db.execute("SELECT * FROM orders").fetchall()
        assert len(orders) == 2
        assert all(o["dry_run"] == 1 for o in orders)
        markets = {(o["platform"], o["market_id"], o["outcome"]) for o in orders}
        assert markets == {
            ("kalshi", "KXTEST", "yes"),
            ("polymarket", "222", "no"),
        }
        assert kalshi.orders == {} and poly.orders == {}

        # Audit trail exists.
        events = {r["event"] for r in ledger.db.execute("SELECT event FROM audit_log")}
        assert "dry_run_arb" in events

    async def test_second_scan_respects_exposure_from_first(self, paper_env):
        # With dry-run orders there are no real fills, so exposure stays 0 and
        # a second scan finds the same opportunity again (books are static).
        scanner, ledger, _, _ = paper_env
        await scanner.scan_pair(PAIR)
        await scanner.scan_pair(PAIR)
        taken = ledger.db.execute(
            "SELECT COUNT(*) AS n FROM opportunities WHERE taken=1"
        ).fetchone()
        assert taken["n"] == 2

    async def test_report_renders_pipeline_results(self, paper_env):
        scanner, ledger, _, _ = paper_env
        await scanner.scan_pair(PAIR)

        text = build_report(ledger)
        assert "test-pair" in text
        assert "1 taken" in text
        assert "Locked capital" in text

        # dry-run orders show as positions only when explicitly included
        assert "(none)" in text  # no real positions
        text_dry = build_report(ledger, include_dry_run=True)
        assert "KXTEST" in text_dry

    async def test_kill_switch_stops_paper_trading_too(self, paper_env, tmp_path):
        scanner, ledger, _, _ = paper_env
        (tmp_path / "KILL").touch()
        await scanner.scan_pair(PAIR)
        taken = ledger.db.execute(
            "SELECT COUNT(*) AS n FROM opportunities WHERE taken=1"
        ).fetchone()
        assert taken["n"] == 0
        skipped = ledger.db.execute(
            "SELECT skip_reason FROM opportunities WHERE skip_reason LIKE 'halted%'"
        ).fetchall()
        assert len(skipped) == 1
