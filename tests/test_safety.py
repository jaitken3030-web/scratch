from decimal import Decimal

import pytest

from arbot.config import SafetyConfig, SizingConfig
from arbot.models import Leg, Outcome, Platform
from arbot.safety import SafetyManager

D = Decimal


@pytest.fixture
def safety(ledger, tmp_path):
    cfg = SafetyConfig(
        kill_switch_file=str(tmp_path / "KILL"),
        daily_loss_limit_usd=D("50"),
        per_market_exposure_usd=D("200"),
        webhook_url="",
    )
    sizing = SizingConfig(max_total_exposure_usd=D("500"))
    return SafetyManager(cfg, sizing, ledger)


async def test_kill_switch_blocks_trading(safety, tmp_path):
    allowed, _ = await safety.trading_allowed()
    assert allowed
    (tmp_path / "KILL").touch()
    allowed, why = await safety.trading_allowed()
    assert not allowed
    assert "kill switch" in why


async def test_daily_loss_limit_halts_and_alerts(safety, ledger):
    ledger.record_pnl(D("-60"), note="bad day")
    allowed, why = await safety.trading_allowed()
    assert not allowed
    assert "daily loss" in why
    rows = ledger.db.execute("SELECT event FROM audit_log WHERE event='alert'").fetchall()
    assert len(rows) == 1
    # Alert fires once, not on every subsequent check.
    await safety.trading_allowed()
    rows = ledger.db.execute("SELECT event FROM audit_log WHERE event='alert'").fetchall()
    assert len(rows) == 1


async def test_loss_under_limit_allows_trading(safety, ledger):
    ledger.record_pnl(D("-49.99"))
    allowed, _ = await safety.trading_allowed()
    assert allowed


def test_pair_headroom_reflects_exposure(safety, ledger):
    leg = Leg(
        platform=Platform.KALSHI, market_id="KXTEST", outcome=Outcome.YES,
        price=D("0.50"), size=D(100), cost=D("50"), fee=D(0),
    )
    oid = ledger.record_order(
        opportunity_id=None, leg=leg, action="buy", order_type="limit", status="filled"
    )
    ledger.record_fill(oid, D("0.50"), D(100), D(0))
    # per-market: 200 - 50 = 150; total: 500 - 50 = 450 -> min is 150
    assert safety.pair_headroom_usd("KXTEST", ("111", "222")) == D("150")
    # unrelated market only hits the total cap
    assert safety.pair_headroom_usd("OTHER", ("888", "999")) == D("200")


def test_headroom_never_negative(safety, ledger):
    leg = Leg(
        platform=Platform.KALSHI, market_id="KXBIG", outcome=Outcome.YES,
        price=D("0.90"), size=D(700), cost=D("630"), fee=D(0),
    )
    oid = ledger.record_order(
        opportunity_id=None, leg=leg, action="buy", order_type="limit", status="filled"
    )
    ledger.record_fill(oid, D("0.90"), D(700), D(0))
    assert safety.pair_headroom_usd("KXBIG", ("1", "2")) == D(0)
