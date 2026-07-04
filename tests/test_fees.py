from decimal import Decimal

import pytest

from arbot.fees import fee_for, kalshi_fee, polymarket_fee

D = Decimal


class TestKalshiFee:
    def test_exact_cent_no_rounding(self):
        # 0.07 * 100 * 0.50 * 0.50 = 1.75 exactly
        assert kalshi_fee(D("0.50"), D(100)) == D("1.75")

    def test_rounds_up_to_next_cent(self):
        # 0.07 * 1 * 0.35 * 0.65 = 0.015925 -> 0.02
        assert kalshi_fee(D("0.35"), D(1)) == D("0.02")

    def test_rounds_up_not_half_even(self):
        # 0.07 * 7 * 0.50 * 0.50 = 0.1225 -> 0.13 (always up, never to nearest)
        assert kalshi_fee(D("0.50"), D(7)) == D("0.13")

    def test_symmetric_in_price(self):
        assert kalshi_fee(D("0.30"), D(50)) == kalshi_fee(D("0.70"), D(50))

    def test_zero_contracts(self):
        assert kalshi_fee(D("0.50"), D(0)) == D("0.00")

    def test_custom_rate(self):
        # 0.035 * 100 * 0.25 * 0.75 = 0.65625 -> 0.66
        assert kalshi_fee(D("0.25"), D(100), rate=D("0.035")) == D("0.66")


class TestPolymarketFee:
    def test_zero_rate_default(self):
        assert polymarket_fee(D("0.40"), D(1000)) == D("0.00")

    def test_uses_min_of_p_and_1_minus_p(self):
        # rate 0.02, min(0.30, 0.70) = 0.30 -> 0.02 * 0.30 * 100 = 0.60
        assert polymarket_fee(D("0.30"), D(100), rate=D("0.02")) == D("0.60")
        assert polymarket_fee(D("0.70"), D(100), rate=D("0.02")) == D("0.60")

    def test_rounds_up(self):
        # 0.02 * 0.333 * 1 = 0.00666 -> 0.01
        assert polymarket_fee(D("0.333"), D(1), rate=D("0.02")) == D("0.01")


class TestFeeFor:
    def test_dispatch(self):
        kwargs = dict(kalshi_rate=D("0.07"), polymarket_rate=D("0.02"))
        assert fee_for("kalshi", D("0.50"), D(100), **kwargs) == D("1.75")
        assert fee_for("polymarket", D("0.30"), D(100), **kwargs) == D("0.60")

    def test_unknown_platform(self):
        with pytest.raises(ValueError):
            fee_for("nyse", D("0.5"), D(1), kalshi_rate=D(0), polymarket_rate=D(0))
