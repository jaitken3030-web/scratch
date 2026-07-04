from decimal import Decimal

from arbot.arb import evaluate_direction, evaluate_pair
from arbot.config import FeeConfig, SizingConfig
from arbot.models import BookLevel, MarketBook, Outcome, Pair, Platform

D = Decimal

PAIR = Pair(
    pair_id="test-pair",
    kalshi_ticker="KXTEST",
    polymarket_condition_id="0xcond",
    polymarket_yes_token_id="111",
    polymarket_no_token_id="222",
)

NO_FEES = FeeConfig(kalshi_rate=D(0), polymarket_rate=D(0))
KALSHI_FEES = FeeConfig(kalshi_rate=D("0.07"), polymarket_rate=D(0))
WIDE_OPEN = SizingConfig(
    max_usd_per_arb=D("1000000"), max_total_exposure_usd=D("1000000"), max_book_depth_pct=D(1)
)


def kalshi_book(yes_asks=(), no_asks=()):
    return MarketBook(
        platform=Platform.KALSHI,
        market_id="KXTEST",
        yes_asks=[BookLevel(D(p), D(s)) for p, s in yes_asks],
        no_asks=[BookLevel(D(p), D(s)) for p, s in no_asks],
    )


def poly_book(yes_asks=(), no_asks=()):
    return MarketBook(
        platform=Platform.POLYMARKET,
        market_id="0xcond",
        yes_asks=[BookLevel(D(p), D(s)) for p, s in yes_asks],
        no_asks=[BookLevel(D(p), D(s)) for p, s in no_asks],
    )


def kyes_pno(kb, pb, fees=NO_FEES, sizing=WIDE_OPEN, min_edge=D(100), headroom=None):
    return evaluate_direction(
        PAIR, "kalshi_yes/poly_no", kb, Outcome.YES, pb, Outcome.NO,
        fees, sizing, min_edge, headroom,
    )


class TestBasicArb:
    def test_single_level_arb_no_fees(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.40", 100)]),
            poly_book(no_asks=[("0.55", 100)]),
        )
        opp = q.opportunity
        assert opp is not None and q.skip_reason is None
        assert opp.size == 100
        assert opp.total_cost == D("95.00")
        assert opp.edge_bps == D(500)
        assert opp.est_profit == D("5.00")
        assert q.top_edge_bps == D(500)

    def test_legs_are_matched_in_size(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.40", 70)]),
            poly_book(no_asks=[("0.55", 300)]),
        )
        leg_k, leg_p = q.opportunity.legs
        assert leg_k.size == leg_p.size == 70
        assert leg_k.platform is Platform.KALSHI
        assert leg_p.market_id == "222"  # NO token id
        assert leg_p.outcome is Outcome.NO

    def test_no_edge_when_combined_cost_over_dollar(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.65", 100)]),
            poly_book(no_asks=[("0.44", 100)]),
        )
        assert q.opportunity is None
        assert q.skip_reason == "no_edge"
        assert q.top_edge_bps < 0

    def test_positive_edge_below_threshold_is_skipped(self):
        # cost 0.99 -> 100bps edge, threshold 150bps
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.55", 100)]),
            poly_book(no_asks=[("0.44", 100)]),
            min_edge=D(150),
        )
        assert q.opportunity is None
        assert q.skip_reason == "below_min_edge"
        assert q.top_edge_bps == D(100)

    def test_empty_book(self):
        q = kyes_pno(kalshi_book(), poly_book(no_asks=[("0.55", 100)]))
        assert q.opportunity is None
        assert q.skip_reason == "empty_book"


class TestDepthAndCaps:
    def test_walk_stops_at_unprofitable_level(self):
        # Level 2 costs 0.44 + 0.55 = 0.99 -> only 100bps, below the 150 min.
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.40", 50), ("0.44", 50)]),
            poly_book(no_asks=[("0.55", 200)]),
            min_edge=D(150),
        )
        opp = q.opportunity
        assert opp.size == 50
        assert opp.legs[0].price == D("0.40")  # never touched level 2

    def test_walk_takes_multiple_profitable_levels(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.38", 30), ("0.40", 30)]),
            poly_book(no_asks=[("0.55", 200)]),
        )
        opp = q.opportunity
        assert opp.size == 60
        leg_k = opp.legs[0]
        assert leg_k.price == D("0.40")  # worst touched level -> limit price
        assert leg_k.cost == D("0.38") * 30 + D("0.40") * 30

    def test_budget_cap_rounds_down_to_whole_contracts(self):
        # unit cost 0.95, budget $19 -> exactly 20; budget $18.99 -> 19
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.40", 1000)]),
            poly_book(no_asks=[("0.55", 1000)]),
            sizing=SizingConfig(max_usd_per_arb=D("18.99"), max_book_depth_pct=D(1)),
        )
        assert q.opportunity.size == 19
        assert q.opportunity.total_cost <= D("18.99")

    def test_book_depth_pct_cap(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.40", 100)]),
            poly_book(no_asks=[("0.55", 400)]),
            sizing=SizingConfig(max_usd_per_arb=D(100000), max_book_depth_pct=D("0.25")),
        )
        # 25% of the thinner (kalshi, 100) book -> 25 contracts
        assert q.opportunity.size == 25

    def test_exposure_headroom_caps_budget(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.40", 1000)]),
            poly_book(no_asks=[("0.55", 1000)]),
            headroom=D("9.50"),
        )
        assert q.opportunity.size == 10  # floor(9.50 / 0.95)


class TestFeesInArbMath:
    def test_kalshi_fee_reduces_edge(self):
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.38", 500)]),
            poly_book(no_asks=[("0.55", 500)]),
            fees=KALSHI_FEES,
            sizing=SizingConfig(max_usd_per_arb=D(100), max_book_depth_pct=D(1)),
            min_edge=D(150),
        )
        opp = q.opportunity
        # unit cost = 0.93 + 0.07*0.38*0.62 = 0.946492 -> floor(100/0.946492) = 105
        assert opp.size == 105
        leg_k = opp.legs[0]
        # fee = ceil_cents(0.07 * 105 * 0.38 * 0.62) = ceil_cents(1.73166) = 1.74
        assert leg_k.fee == D("1.74")
        assert opp.total_cost == D("105") * D("0.93") + D("1.74")  # 99.39
        assert opp.est_profit == D("5.61")
        expected_edge = (D("5.61") / D(105) * 10000).quantize(D("0.01"))
        assert opp.edge_bps.quantize(D("0.01")) == expected_edge

    def test_fee_can_kill_marginal_edge(self):
        # 0.485 + 0.495 = 0.98 raw (200bps edge), but the Kalshi fee
        # (~0.0175/contract) pushes unit cost past the 150bps threshold.
        q = kyes_pno(
            kalshi_book(yes_asks=[("0.485", 100)]),
            poly_book(no_asks=[("0.495", 100)]),
            fees=KALSHI_FEES,
            min_edge=D(150),
        )
        assert q.opportunity is None
        assert q.skip_reason == "below_min_edge"


class TestBothDirections:
    def test_evaluate_pair_returns_both_directions(self):
        kb = kalshi_book(yes_asks=[("0.40", 100)], no_asks=[("0.62", 100)])
        pb = poly_book(yes_asks=[("0.44", 100)], no_asks=[("0.55", 100)])
        quotes = evaluate_pair(PAIR, kb, pb, NO_FEES, WIDE_OPEN, D(100))
        assert [q.direction for q in quotes] == ["kalshi_yes/poly_no", "kalshi_no/poly_yes"]
        # yes/no: 0.40 + 0.55 = 0.95 -> arb; no/yes: 0.62 + 0.44 = 1.06 -> none
        assert quotes[0].opportunity is not None
        assert quotes[1].opportunity is None

    def test_reverse_direction_arb(self):
        kb = kalshi_book(yes_asks=[("0.60", 100)], no_asks=[("0.42", 100)])
        pb = poly_book(yes_asks=[("0.50", 100)], no_asks=[("0.55", 100)])
        quotes = evaluate_pair(PAIR, kb, pb, NO_FEES, WIDE_OPEN, D(100))
        assert quotes[0].opportunity is None  # 0.60 + 0.55 = 1.15
        opp = quotes[1].opportunity  # 0.42 + 0.50 = 0.92 -> 800bps
        assert opp is not None
        assert opp.edge_bps == D(800)
        leg_k, leg_p = opp.legs
        assert leg_k.outcome is Outcome.NO
        assert leg_p.market_id == "111"  # YES token
