"""Guards of the trade tools: the account's limits on the order as it is signed."""

import math
from decimal import Decimal

import pytest

from fractrade_hl_simple.mcp_server import guards
from fractrade_hl_simple.mcp_server.config import parse_config
from fractrade_hl_simple.mcp_server.errors import ToolError, error_body
from fractrade_hl_simple.models import Leverage

D = Decimal

ADDRESS = "0x" + "1" * 40


def account(extra: str = ""):
    return parse_config(f'''
[accounts.main]
address = "{ADDRESS}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5
{extra}
''', "test.toml").accounts["main"]


RISK = guards.RiskState(leverage=Leverage(type="cross", value=Decimal(5)))  # the account state of an order


def blocked(report):
    """The error body of a report that must block."""
    assert not report.passed
    with pytest.raises(ToolError) as caught:
        report.enforce()
    return error_body(caught.value)["error"]


def rules(report):
    return {check.rule: check.passed for check in report.checks}


class TestRules:
    @pytest.mark.parametrize("symbol", ["BTC", "kPEPE", "xyz:NVDA", "para:TOTAL2", "io:ANTH"])
    def test_perps_of_every_dex_pass(self, symbol):
        assert guards.perp_only(symbol).passed

    @pytest.mark.parametrize("symbol", ["PURR/USDC", "@107", "#12", ""])
    def test_spot_and_outcome_markets_fail(self, symbol):
        # The library's cancel_order cancels a spot order by its pair name.
        assert not guards.perp_only(symbol).passed

    def test_the_allowlist_matches_names_exactly(self):
        allowlist = frozenset({"BTC", "xyz:NVDA"})
        assert guards.symbol_allowed("xyz:NVDA", allowlist).passed
        assert not guards.symbol_allowed("NVDA", allowlist).passed
        assert not guards.symbol_allowed("btc", allowlist).passed
        assert guards.symbol_allowed("ETH", None).passed  # no allowlist: every symbol

    def test_notional_at_the_limit_passes_although_float_multiplication_exceeds_it(self):
        assert 0.001 * 86701.0 > 86.701  # 86.70100000000001 in binary floating point
        assert guards.order_notional(0.001, 86701.0, Decimal("86.701")).passed
        check = guards.order_notional(0.002, 86701.0, Decimal("100"))
        assert not check.passed and "173.402" in check.detail

    @pytest.mark.parametrize("price", [math.nan, math.inf])
    def test_a_notional_that_is_no_number_fails(self, price):
        assert not guards.order_notional(0.001, price, Decimal("100")).passed

    def test_a_float_whose_repr_is_no_number_is_compared_by_its_value(self):
        class Float(float):  # as numpy 2's float64: repr np.float64(0.02)
            def __repr__(self):
                return f"np.float64({float(self)})"
        assert guards.slippage(Float(0.02), Decimal("0.02")).passed
        assert guards.order_notional(Float(0.001), Float(86701.0), Decimal("86.701")).passed

    def test_slippage_at_the_cap_passes(self):
        # Decimal(0.02) is 0.0200000000000000004163...: compared as written, not in binary.
        assert guards.slippage(0.02, Decimal("0.02")).passed
        assert not guards.slippage(0.021, Decimal("0.02")).passed
        assert guards.slippage(None, Decimal("0.02")).passed  # the client's default is the cap

    @pytest.mark.parametrize("value", [math.nan, math.inf, True, "0.01"])
    def test_a_slippage_that_is_no_number_fails(self, value):
        assert not guards.slippage(value, Decimal("0.02")).passed

class TestActions:
    def test_an_order_is_checked_against_every_limit(self):
        report = guards.check_order(account('symbols = ["BTC"]'), "BTC", 0.001, 86701.0,
                                    is_buy=True, reduce_only=False, market=True, slippage_value=0.01, risk=RISK)
        assert report.passed
        assert rules(report) == {"perp_only": True, "symbol_allowed": True, "order_notional": True, "slippage": True}

    def test_a_symbol_outside_the_allowlist_is_blocked(self):
        report = guards.check_order(account('symbols = ["BTC"]'), "ETH", 0.01, 3000.0,
                                    is_buy=True, reduce_only=False, market=False, risk=RISK)
        assert rules(report)["symbol_allowed"] is False
        assert "ETH is not in the account's symbols (BTC)" in blocked(report)["message"]

    def test_a_market_order_above_max_slippage_is_blocked(self):
        report = guards.check_order(account("max_slippage = 0.01"), "BTC", 0.001, 86701.0,
                                    is_buy=True, reduce_only=True, market=True, slippage_value=0.05)
        assert "slippage 0.05 is above max_slippage 0.01" in blocked(report)["message"]

    def test_leverage_respects_the_allowlist_and_the_cap(self):
        assert guards.check_leverage(account('symbols = ["BTC"]'), "BTC", 5).passed
        assert not guards.check_leverage(account('symbols = ["BTC"]'), "ETH", 5).passed
        assert not guards.check_leverage(account(), "BTC", 6).passed


def risk(leverage=20, kind="cross", **fields):
    return guards.RiskState(leverage=Leverage(type=kind, value=Decimal(leverage)), **fields)


def risky(extra, size, price, state):
    """The checks of an order that adds risk, for an account with `extra` config."""
    return guards.check_order(account(extra), "BTC", size, price, is_buy=True, reduce_only=False, market=False, risk=state)


class TestRiskGuardrails:
    """Each guardrail is optional: it is checked only when its key is set."""

    @pytest.mark.parametrize("leverage, value, passes", [
        (20, 1000.0, True),   # $1000 account, 5 %: $50 of margin, so BTC at 20x up to $1000
        (20, 1010.0, False),
        (3, 150.0, True),     # a market at 3x: the same $50 is a $150 order
        (3, 160.0, False),
    ])
    def test_margin_per_trade_follows_the_markets_leverage(self, leverage, value, passes):
        report = risky("max_margin_pct = 5", 1.0, value, risk(leverage, account_value=Decimal(1000)))
        assert rules(report)["max_margin_pct"] is passes

    def test_one_big_trade_within_the_total_margin_without_a_per_trade_cap(self):
        # The author's setup: no max_margin_pct, no max_order_usd, max_total_margin_pct 80, 20x.
        own = parse_config(f'''
[accounts.main]
address = "{ADDRESS}"
key_env = "HL_KEY_MAIN"
trading = true
max_leverage = 20
max_total_margin_pct = 80
''', "test.toml").accounts["main"]
        state = risk(20, account_value=Decimal(1000), margin_used=Decimal(0))
        report = guards.check_order(own, "BTC", 0.2, 80000.0, is_buy=True, reduce_only=False, market=False, risk=state)
        assert report.passed  # $16000 at 20x: $800 of margin, 80 % of $1000
        assert "max_margin_pct" not in rules(report) and "order_notional" not in rules(report)
        too_big = guards.check_order(own, "BTC", 0.21, 80000.0, is_buy=True, reduce_only=False, market=False, risk=state)
        assert rules(too_big)["max_total_margin_pct"] is False

    def test_isolated_only_refuses_cross_margin_for_orders_and_set_leverage(self):
        assert rules(risky("isolated_only = true", 0.001, 85000.0, risk(5, "cross")))["isolated_only"] is False
        assert rules(risky("isolated_only = true", 0.001, 85000.0, risk(5, "isolated")))["isolated_only"] is True
        assert not guards.check_leverage(account("isolated_only = true"), "BTC", 5, is_cross=True).passed
        assert guards.check_leverage(account("isolated_only = true"), "BTC", 5, is_cross=False).passed

    @pytest.mark.parametrize("loss, passes", [(Decimal(99), True), (Decimal(100), False), (Decimal(0), True)])
    def test_the_daily_loss_stop(self, loss, passes):
        state = risk(5, day_loss=loss, day_capital=Decimal(1000))
        assert rules(risky("max_daily_loss_pct = 10", 0.001, 85000.0, state))["max_daily_loss_pct"] is passes

    @pytest.mark.parametrize("extra, state", [
        ("max_margin_pct = 5", risk(5)),                          # no account value (e.g. portfolio margin)
        ("max_total_margin_pct = 50", risk(5, account_value=Decimal(1000))),  # no margin in use
        ("max_daily_loss_pct = 10", risk(5)),                     # no 24 h PnL
    ])
    def test_a_guardrail_whose_data_is_missing_refuses(self, extra, state):
        assert not risky(extra, 0.001, 85000.0, state).passed

    def test_a_reduce_only_order_needs_no_risk_state(self):
        report = guards.check_order(account("max_margin_pct = 5\nisolated_only = true"), "BTC", 1.0, 85000.0,
                                    is_buy=True, reduce_only=True, market=False)
        assert report.passed and set(rules(report)) == {"perp_only"}

class TestRiskDataThatCannotPass:
    """A guardrail passes only on finite figures: Infinity, NaN, a negative margin in use or
    a negative loss refuse, as a missing figure does."""

    @pytest.mark.parametrize("extra, fields", [
        ("max_margin_pct = 5", {"account_value": Decimal("Infinity")}),
        ("max_margin_pct = 5", {"account_value": Decimal("NaN")}),
        ("max_total_margin_pct = 30", {"account_value": Decimal("Infinity"), "margin_used": Decimal(0)}),
        ("max_total_margin_pct = 30", {"account_value": Decimal(1000), "margin_used": Decimal(-10_000)}),
        ("max_total_margin_pct = 30", {"account_value": Decimal(1000), "margin_used": Decimal("NaN")}),
        ("max_daily_loss_pct = 10", {"day_loss": Decimal(50), "day_capital": Decimal("Infinity")}),
        ("max_daily_loss_pct = 10", {"day_loss": Decimal(0), "day_capital": Decimal("NaN")}),
        ("max_daily_loss_pct = 10", {"day_loss": Decimal(-5), "day_capital": Decimal(1000)}),
    ])
    def test_refuses(self, extra, fields):
        assert not risky(extra, 0.001, 85000.0, risk(5, **fields)).passed


class TestSellsAreValuedAtTheMarket:
    """A sell priced below the market fills at the market: it is valued at the mid then."""

    def test_a_sell_far_below_the_market_is_valued_at_the_mid(self):
        state = risk(5, account_value=Decimal(1000), mid=Decimal(85000))
        # 0.0031 BTC at 80000 would be 49.60 of margin; at the mid, 52.70: above 5 % of 1000.
        report = guards.check_order(account("max_margin_pct = 5"), "BTC", 0.0031, 80000.0, is_buy=False,
                                    reduce_only=False, market=False, risk=state)
        assert rules(report)["max_margin_pct"] is False

    def test_a_sell_above_the_market_is_valued_at_its_price(self):
        assert guards.valued_price(90000.0, False, Decimal(85000)) == Decimal(90000)
        assert guards.valued_price(80000.0, True, Decimal(85000)) == Decimal(80000)  # a buy: its limit, the most

    @pytest.mark.parametrize("mid", [None, Decimal("NaN"), Decimal("Infinity")])
    def test_a_sell_without_a_mid_cannot_pass(self, mid):
        report = guards.check_order(account(), "BTC", 0.0001, 80000.0, is_buy=False, reduce_only=False,
                                    market=False, risk=risk(5, mid=mid))
        assert rules(report)["order_notional"] is False


class TestMaxRiskPct:
    def test_a_trades_loss_at_its_stops_is_held_to_a_share_of_the_account(self):
        assert guards.trade_risk(D("9.99"), D(1000), D(1)).passed
        assert not guards.trade_risk(D("10.01"), D(1000), D(1)).passed

    @pytest.mark.parametrize("loss, value", [(None, Decimal(1000)), (Decimal(5), None), (Decimal(5), Decimal("NaN")),
                                             (Decimal("-Infinity"), Decimal(1000)), (Decimal("NaN"), Decimal(1000))])
    def test_without_stops_or_an_account_value_it_refuses(self, loss, value):
        assert not guards.trade_risk(loss, value, Decimal(1)).passed

class TestTradingDay:
    @pytest.mark.parametrize("now, start", [
        ("2026-11-18T05:59:00+02:00", "2026-11-17T06:00:00+02:00"),  # before 06:00: still yesterday's day
        ("2026-03-29T05:59:00+03:00", "2026-03-28T06:00:00+02:00"),  # across the spring change: 23 hours
        ("2026-10-25T05:59:00+02:00", "2026-10-24T06:00:00+03:00"),  # across the autumn change: 25 hours
    ])
    def test_it_starts_by_the_local_clock(self, now, start):
        from datetime import datetime

        def ms(text):
            return int(datetime.fromisoformat(text).timestamp() * 1000)
        assert guards.day_start_ms(ms(now), "06:00", "Europe/Athens") == ms(start)

    @pytest.mark.parametrize("portfolio, expected", [
        # A deposit raises the account value but is no PnL: it gives no room back.
        ({"day": {"accountValueHistory": [[90, "1000"], [200, "1440"]], "pnlHistory": [[90, "0"], [200, "-60"]]}},
         (D(60), D(1000), 90)),
        # No record before the start in the day history (late in the day): the week's is used.
        ({"day": {"accountValueHistory": [[101, "900"]], "pnlHistory": [[101, "0"]]},
          "week": {"accountValueHistory": [[40, "1000"], [200, "950"]], "pnlHistory": [[40, "10"], [200, "-40"]]}},
         (D(50), D(1000), 40)),
    ], ids=["deposit", "week"])
    def test_the_loss_since_hyperliquids_last_record_before_the_start(self, portfolio, expected):
        assert guards.portfolio_since(portfolio, 100) == expected
