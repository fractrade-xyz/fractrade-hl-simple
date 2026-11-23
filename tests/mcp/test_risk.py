"""The risk tools: trade outcomes from stops and take profits (risk.py, figures computed by
hand), and get_position_risk / plan_trade through the server with a fake library client."""

import json
from decimal import Decimal

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from fractrade_hl_simple.exceptions import NetworkException  # noqa: E402
from fractrade_hl_simple.mcp_server import guards, risk  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.server import build_server  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import read_tools  # noqa: E402
from fractrade_hl_simple.models import (  # noqa: E402
    AccountSummary, DexAccount, FeeRates, Leverage, Order, OrderType, Position)

D = Decimal
FLAT = risk.Book(bids=((D("2.5"), D(10**9)),), asks=((D("2.5"), D(10**9)),))  # no spread, no impact
NO_FEES = risk.Fees(taker=D(0), maker=D(0))


def stop(price, size, market=True):
    return risk.Exit("stop_loss", D(price), D(size), market=market)


def take(price, size, market=True, resting=False):
    return risk.Exit("take_profit", D(price), D(size), market=market, resting=resting)


class TestImpact:
    BOOK = risk.Book(bids=((D(100), D(5)),), asks=((D(101), D(1)), (D(102), D(2))))  # mid 100.5

    def test_a_buy_walks_the_asks(self):
        # 1 at 101 and 2 at 102: 101.667 on average, against the mid of 100.5.
        assert risk.impact(self.BOOK, True, D(3)).quantize(D("0.000001")) == D("0.011609")

class TestOutcomes:
    def icp(self, **fields):
        # The ICP short: 1301.6 at 2.49774, two stops, three take profits.
        values = dict(is_long=False, entry=D("2.49774"), size=D("1301.6"), fees=NO_FEES, book=FLAT,
                      account_value=D(1500),
                      exits=[stop("2.555", "650.8"), stop("2.58", "650.8"),
                             take("2.42", "500"), take("2.37", "500"), take("2.31", "301.6")])
        return risk.outcomes(**{**values, **fields})

    def test_the_totals_risk_reward_and_break_even_of_a_laddered_short(self):
        result = self.icp()
        assert result["if_all_stops"]["net_pnl"] == D("-90.80")
        assert result["if_all_take_profits"]["net_pnl"] == D("159.36")
        assert result["risk_reward"] == D("1.76") and result["break_even_win_rate_percent"] == D("36.30")
        assert result["if_all_stops"]["percent_of_account"] == D("6.05")
        assert result["unprotected_size"] == 0 and result["warnings"] == []

    def test_a_long_gains_when_the_price_rises(self):
        result = risk.outcomes(is_long=True, entry=D(100), size=D(2), fees=NO_FEES, book=FLAT, account_value=None,
                               exits=[stop(95, 2), take(110, 2)])
        assert (result["if_all_stops"]["net_pnl"], result["if_all_take_profits"]["net_pnl"]) == (D(-10), D(20))
        assert result["risk_reward"] == D(2) and result["if_all_stops"]["percent_of_account"] is None

    def test_fees_and_slippage_lower_the_net(self):
        # A long of 2 at 100, stop at 95: the stop sells into bids 1 below the mid.
        book = risk.Book(bids=((D(99), D(10)),), asks=((D(101), D(10)),))  # mid 100: 1 % impact
        fees = risk.Fees(taker=D("0.001"), maker=D("0.0002"))
        level = risk.outcomes(is_long=True, entry=D(100), size=D(2), fees=fees, book=book, account_value=None,
                              exits=[stop(95, 2)])["stop_losses"][0]
        # Executed at 95 × 0.99 = 94.05: slippage 1.90, fee 0.001 × 94.05 × 2 = 0.19.
        assert (level["gross_pnl"], level["slippage"], level["fee"], level["net_pnl"]) == (
            D("-10.00"), D("1.90"), D("0.19"), D("-12.09"))

    def test_a_resting_limit_take_profit_pays_the_maker_fee_without_slippage(self):
        fees = risk.Fees(taker=D("0.001"), maker=D("0.0002"))
        level = risk.outcomes(is_long=True, entry=D(100), size=D(1), fees=fees, book=FLAT, account_value=None,
                              exits=[take(110, 1, market=False, resting=True)])["take_profits"][0]
        assert (level["slippage"], level["fee"], level["executes_as"]) == (D("0.00"), D("0.02"), "limit (maker)")

    def test_stops_count_from_the_nearest_and_only_up_to_the_position(self):
        # A long of 2: the stop at 95 fills first, 1 of the one at 90; the one at 85 never.
        result = risk.outcomes(is_long=True, entry=D(100), size=D(2), fees=NO_FEES, book=FLAT, account_value=None,
                               exits=[stop(90, 2), stop(85, 1), stop(95, 1)])
        assert [(level["price"], level["size"]) for level in result["stop_losses"]] == [(D(95), D(1)), (D(90), D(1))]
        assert result["if_all_stops"]["net_pnl"] == D(-15)
        assert any("stop loss at 85 is beyond the position's size" in w for w in result["warnings"])

    def test_the_part_without_a_stop_is_named_with_its_loss_at_liquidation(self):
        result = self.icp(exits=[stop("2.62", "433.8"), stop("2.6789", "433.9")], liquidation_price=D("2.8792"))
        assert result["unprotected_size"] == D("433.9")
        warning = next(w for w in result["warnings"] if "has no stop" in w)
        # (2.8792 - 2.49774) × 433.9 = 165.5155
        assert "433.9 has no stop" in warning and "loses 165.52 USD" in warning
        assert result["risk_reward"] is None  # no take profits

    @pytest.mark.parametrize("exits, says", [
        ([], "no stop loss"),
        ([stop("2.95", "1301.6")], "beyond the liquidation price"),
        ([stop("2.40", "1301.6")], "already crossed by the market"),  # a short's stop below the price
    ])
    def test_warnings(self, exits, says):
        assert any(says in w for w in self.icp(exits=exits, liquidation_price=D("2.88"))["warnings"])

    def test_the_daily_loss_limit(self):
        result = self.icp(max_daily_loss_pct=D(5))
        assert any("reaches max_daily_loss_pct (5 %)" in w for w in result["warnings"])

    def test_a_planned_entry_fee_counts_in_both_totals(self):
        result = risk.outcomes(is_long=True, entry=D(100), size=D(2), fees=NO_FEES, book=FLAT, account_value=None,
                               exits=[stop(95, 2), take(110, 1)], entry_fee=D("0.20"))
        assert result["if_all_stops"]["net_pnl"] == D("-10.20")
        assert result["if_all_take_profits"]["net_pnl"] == D("9.90")  # half the size: half the entry fee


def sized(book=FLAT, fees=NO_FEES, risk_usd=50, stop_price=95, limit=None, decimals=2, stops=None):
    return risk.size_for_risk(is_long=True, limit=None if limit is None else D(limit), resting=False,
                              stops=stops or [(D(stop_price), D(1))], risk=D(risk_usd), fees=fees, book=book,
                              size_decimals=decimals)


def loss(size, book=FLAT, fees=NO_FEES, stop_price=95, limit=None, decimals=2, stops=None):
    return risk.planned_loss(size, is_long=True, limit=None if limit is None else D(limit), resting=False,
                             stops=stops or [(D(stop_price), D(1))], fees=fees, book=book, size_decimals=decimals)


BOOK_100 = risk.Book(bids=((D(100), D(10**9)),), asks=((D(100), D(10**9)),))  # mid 100, no impact


class TestSizeForRisk:

    def test_fees_at_the_executed_prices_make_it_smaller_and_it_never_exceeds_the_risk(self):
        fees = risk.Fees(taker=D("0.001"), maker=D("0.0002"))
        # Per unit: 5 + 0.1 (entry fee) + 0.095 (stop fee) = 5.195: 50 / 5.195 = 9.624...
        size, limited = sized(BOOK_100, fees)
        assert (size, limited) == (D("9.62"), False)
        assert loss(size, BOOK_100, fees) <= 50 < loss(size + D("0.01"), BOOK_100, fees)

    def test_a_book_whose_depth_jumps_cannot_make_it_oversized(self):
        # 10 units near the mid, then far away: an iteration oscillated here and returned 70.05,
        # which loses 669.54 against a risk of 100.
        book = risk.Book(bids=((D("99.9"), D(10)), (D(95), D(10_000))), asks=((D("100.1"), D(10)), (D(105), D(10_000))))
        size, _ = sized(book, risk_usd=100, stop_price=99, decimals=2)
        assert loss(size, book, stop_price=99) <= 100
        assert loss(size + D("0.01"), book, stop_price=99) > 100

    def test_a_size_beyond_the_visible_book_is_never_offered(self):
        thin = risk.Book(bids=((D(99), D(1)),), asks=((D(101), D(1)),))
        size, limited = sized(thin, risk_usd=10_000)
        assert size <= 1 and limited

    def test_several_stops_are_sized_together(self):
        # Half at 95 (5 per unit), half at 90 (10 per unit): 7.5 per unit on average.
        stops = [(D(95), D("0.5")), (D(90), D("0.5"))]
        size, _ = sized(BOOK_100, risk_usd=75, stops=stops)
        assert size == D(10) and loss(size, BOOK_100, stops=stops) == 75

    def test_several_stops_rounding_cannot_hide_a_larger_size_that_fits(self):
        # Integer lots: size 15 puts 3 lots on the far stop at 10 (463 lost), size 16 only 4 of
        # 16 (376 lost): the loss is not monotone, the search alone stopped at 14.
        book = risk.Book(bids=((D(100), D(16)),), asks=((D(100), D(16)),))
        stops = [(D(99), D("0.5")), (D(98), D("0.25")), (D(10), D("0.25"))]
        size, _ = risk.size_for_risk(is_long=True, limit=None, resting=False, stops=stops, risk=D(376), fees=NO_FEES,
                                     book=book, size_decimals=0)
        assert size == 16 and risk.planned_loss(size, is_long=True, limit=None, resting=False, stops=stops,
                                               fees=NO_FEES, book=book, size_decimals=0) == 376

    def test_a_limit_entry_is_sized_at_its_price(self):
        # Long at a limit of 100, stop 95: 5 per unit, whatever the book's mid.
        assert sized(FLAT, limit=100)[0] == D(10)

    def test_with_a_maker_rebate_the_largest_feasible_size_is_found(self):
        # A rebate makes a unit lose a little less than the move: a bound from the move alone
        # missed the largest size.
        book = risk.Book(bids=((D(101), D(10**6)),), asks=((D(101), D(10**6)),))
        fees = risk.Fees(taker=D("0.00045"), maker=D("-0.00002"))
        size, _ = risk.size_for_risk(is_long=True, limit=D(100), resting=True, stops=[(D(1), D(1))], risk=D(5000),
                                     fees=fees, book=book, size_decimals=5)
        step = D("0.00001")
        def lost(s):
            return risk.planned_loss(s, is_long=True, limit=D(100), resting=True, stops=[(D(1), D(1))],
                                     fees=fees, book=book, size_decimals=5)
        assert lost(size) <= 5000 < lost(size + step)


class TestTriggerLimitsAndSharedLiquidity:
    def test_a_stop_limit_counts_at_its_limit(self):
        result = risk.outcomes(is_long=True, entry=D(100), size=D(1), fees=NO_FEES, book=FLAT, account_value=None,
                               exits=[risk.Exit("stop_loss", D(95), D(1), market=False, limit=D(90))])
        assert result["if_all_stops"]["net_pnl"] == D(-10)  # at worst its limit of 90

    @pytest.mark.parametrize("is_long, trigger, limit, net", [
        (True, 95, 96, D(-4)),    # a long's sell stop limit above its trigger: if it fills, at 96
        (False, 105, 104, D(-4)),  # a short's buy stop limit below its trigger: at 104
    ])
    def test_a_stop_limit_that_fills_better_than_its_trigger_counts_the_better_price(self, is_long, trigger, limit,
                                                                                    net):
        result = risk.outcomes(is_long=is_long, entry=D(100), size=D(1), fees=NO_FEES, book=FLAT, account_value=None,
                               exits=[risk.Exit("stop_loss", D(trigger), D(1), market=False, limit=D(limit))])
        assert result["if_all_stops"]["net_pnl"] == net
        assert result["stop_losses"][0]["slippage"] == D(-1)  # better than the trigger
        assert any("may not fill" in w for w in result["warnings"])

    def test_stops_at_one_trigger_beyond_the_book_are_not_estimated_one_by_one(self):
        # 5 at 99 visible: one stop of 5 fits, two of 5 at the same trigger do not.
        book = risk.Book(bids=((D(99), D(5)),), asks=((D(101), D(10)),))
        result = risk.outcomes(is_long=True, entry=D(100), size=D(10), fees=NO_FEES, book=book, account_value=None,
                               exits=[stop(95, 5), stop(95, 5)])
        assert [level["slippage"] for level in result["stop_losses"]] == [None, None]
        assert any("slippage is not estimated" in w for w in result["warnings"])

    def test_exits_at_one_trigger_share_the_book(self):
        # bids 5 at 99 and 5 at 90: two stops of 5 at 95 fill together, as one of 10.
        book = risk.Book(bids=((D(99), D(5)), (D(90), D(5))), asks=((D(101), D(10)),))
        one = risk.outcomes(is_long=True, entry=D(100), size=D(10), fees=NO_FEES, book=book, account_value=None,
                            exits=[stop(95, 10)])["if_all_stops"]["net_pnl"]
        two = risk.outcomes(is_long=True, entry=D(100), size=D(10), fees=NO_FEES, book=book, account_value=None,
                            exits=[stop(95, 5), stop(95, 5)])["if_all_stops"]["net_pnl"]
        assert one == two == D("-102.25")


# ── the tools ─────────────────────────────────────────────────────────

MAIN = "0x" + "1" * 40
CONFIG = f'''
default_account = "main"
[accounts.main]
address = "{MAIN}"
max_daily_loss_pct = 10
'''
BOOK = {"bids": [{"price": "2.53", "size": "100000", "num_orders": 3}],
        "asks": [{"price": "2.54", "size": "100000", "num_orders": 3}]}


def icp_order(order_id, kind, price, size, is_buy=True, reduce_only=True, market=True):
    trigger = {"triggerPx": D(price), "isMarket": market, "tpsl": "sl" if kind == "stop_loss" else "tp"}
    return Order(order_id=order_id, symbol="ICP", is_buy=is_buy, size=D(size),
                 order_type=OrderType(trigger=trigger) if kind != "limit" else OrderType(limit={"tif": "Gtc"}),
                 reduce_only=reduce_only, status="open", created_at=0, limit_price=D(price),
                 trigger_price=D(price) if kind != "limit" else None, type=kind)


class FakeClient:
    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.perp_dexs, self.market_specs = [""], {"ICP": {"size_decimals": 1}, "BTC": {"size_decimals": 5}}

    def prepare_order(self, symbol, size, is_buy, limit_price=None, slippage=None):
        if limit_price is None:
            limit_price = self._mid_price(symbol) * (1 + (slippage or 0.02) * (1 if is_buy else -1))
        return size, self._format_price(symbol, limit_price)

    def _format_price(self, symbol, price):
        return float(f"{price:.5g}")  # 5 significant figures, as Hyperliquid

    def _mid_price(self, symbol):
        book = self.answers["get_order_book"]
        return (float(book["bids"][0]["price"]) + float(book["asks"][0]["price"])) / 2

    def get_markets(self):
        from types import SimpleNamespace
        return [SimpleNamespace(symbol=name) for name in self.market_specs]

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append(method)
            answer = self.answers[method]
            if isinstance(answer, Exception):
                raise answer
            return answer(*args, **kwargs) if callable(answer) else answer
        return call


def answers(**changes):
    values = {
        "get_positions": [Position(symbol="ICP", entry_price=D("2.49774"), size=D("-1301.6"),
                                   leverage=Leverage(type="cross", value=D(5)), liquidation_price=D("2.8792"),
                                   margin_used=D(650), position_value=D(3290), return_on_equity=D(0),
                                   unrealized_pnl=D("-45"))],
        "get_open_orders": [icp_order(1, "stop_loss", "2.62", "433.8"), icp_order(2, "stop_loss", "2.6789", "433.9"),
                            icp_order(3, "take_profit", "2.37", "433.9"), icp_order(4, "limit", "2.28", "433.8"),
                            icp_order(5, "limit", "2.30", "100", reduce_only=False)],  # not a protection
        "get_order_book": BOOK,
        "get_fee_rates": FeeRates(symbol="ICP", taker=D("0.00045"), maker=D("0.00015")),
        "get_account_summary": AccountSummary(mode="unified", account_value=D(1500), available_to_trade=D(800),
                                              margin_used=D(650), balances=[], dexes=[], sources={}),
        "get_leverage": Leverage(type="cross", value=D(5)),
    }
    return {**values, **changes}


def run(name, arguments, given=None, config_text=None):
    clients = []

    def make_client(base_url, account, default_slippage, perp_dexs):
        clients.append(FakeClient(given or answers()))
        return clients[-1]

    conf = parse_config(config_text or CONFIG, "test.toml")
    server = build_server(read_tools(conf, AccountRegistry(conf, {}, make_client)))

    async def call():
        async with Client(server, mode="2026-07-28") as client:
            result = await client.call_tool(name, arguments)
            return result.is_error, json.loads(result.content[0].text)

    return anyio.run(call)


class TestGetPositionRisk:
    def test_it_reads_the_protections_of_the_position(self):
        is_error, body = run("get_position_risk", {"symbol": "ICP"})
        data = body["data"]
        assert not is_error and data["side"] == "short" and data["unprotected_size"] == "433.9"
        assert [level["price"] for level in data["stop_losses"]] == ["2.62", "2.6789"]
        # The resting reduce-only limit buy is a maker take profit; the plain buy is not counted.
        assert [(level["price"], level["executes_as"]) for level in data["take_profits"]] == [
            ("2.37", "market (taker)"), ("2.28", "limit (maker)")]
        assert any("433.9 has no stop" in w for w in data["warnings"])

    def test_the_stop_attached_to_a_waiting_entry_is_no_protection_yet(self):
        # A sell limit that would add to the short, with a buy stop for its size attached:
        # Hyperliquid places that one only once the entry fills.
        entry = icp_order(1, "limit", "2.60", "1301.6", is_buy=False, reduce_only=False)
        entry.children = [{"coin": "ICP", "side": "B", "limitPx": "2.90", "sz": "1301.6", "oid": 2,
                           "origSz": "1301.6", "timestamp": 0, "isTrigger": True, "triggerPx": "2.62",
                           "orderType": "Stop Market", "reduceOnly": True, "tif": None}]
        is_error, body = run("get_position_risk", {"symbol": "ICP"}, answers(get_open_orders=[entry]))
        assert not is_error and body["data"]["unprotected_size"] == "1301.6" and body["data"]["stop_losses"] == []

    def test_a_stop_for_the_whole_position_counts_for_all_of_it(self):
        # Set "for the entire position" in the Hyperliquid app, it is listed with size 0 and
        # closes the whole position when it triggers. It was counted as size 0: no protection.
        whole = icp_order(1, "stop_loss", "2.62", "0")
        whole.is_position_tpsl = True
        is_error, body = run("get_position_risk", {"symbol": "ICP"}, answers(get_open_orders=[whole]))
        data = body["data"]
        assert not is_error and data["unprotected_size"] == "0"
        assert [(level["price"], level["size"]) for level in data["stop_losses"]] == [("2.62", "1301.6")]

    def test_a_position_tpsl_with_a_size_of_its_own_keeps_it(self):
        # A custom size stays fixed (Hyperliquid's TP/SL docs): only that much is protected.
        sized = icp_order(1, "stop_loss", "2.62", "500")
        sized.is_position_tpsl = True
        is_error, body = run("get_position_risk", {"symbol": "ICP"}, answers(get_open_orders=[sized]))
        assert not is_error and body["data"]["unprotected_size"] == "801.6"

    def test_no_position(self):
        is_error, body = run("get_position_risk", {"symbol": "ICP"}, answers(get_positions=[]))
        assert is_error and body["error"]["code"] == "POSITION_NOT_FOUND"


def day(values, pnl):
    """Hyperliquid's portfolio "day" histories: (time, account value) and (time, cumulative PnL)."""
    return {"day": {"accountValueHistory": [[t, v] for t, v in enumerate(values)],
                    "pnlHistory": [[t, v] for t, v in enumerate(pnl)]}}


def candles(closes):
    from fractrade_hl_simple.models import Candle
    hour = 3_600_000
    return [Candle(symbol="X", interval="1h", open_time=i * hour, close_time=(i + 1) * hour - 1, open=D(c),
                   high=D(c), low=D(c), close=D(c), volume=D(1), trades=1) for i, c in enumerate(closes)]


def account_answers(**changes):
    return answers(**{"get_portfolio": day(["1500", "1450"], ["0", "-50"]), **changes})


class TestGetAccountRisk:
    @pytest.mark.parametrize("pending", [150, 200, 250])
    def test_pending_margin_and_policy_budget_match_submission(self, pending):
        summary = AccountSummary(mode="unified", account_value=D(1000), available_to_trade=D(900),
                                 margin_used=D(100), balances=[], dexes=[], sources={})
        entry = icp_order(9, "limit", "100", str(pending / 20), reduce_only=False)
        reads = []

        def orders(*, address):
            assert address == MAIN
            reads.append("orders")
            return [entry]

        def account(address):
            assert address == MAIN
            reads.append("summary")
            return summary

        is_error, body = run("get_account_risk", {}, account_answers(
            get_open_orders=orders, get_account_summary=account), config_text=CONFIG + "max_total_margin_pct = 30\n")

        assert not is_error, body
        margin = body["data"]["margin"]
        assert D(margin["pending_margin"]) == pending
        assert D(margin["total_margin"]) == 100 + pending
        assert D(margin["margin_limit"]) == 300
        assert D(margin["remaining_margin_budget"]) == 200 - pending
        assert margin["max_total_margin_pct"] == "30"
        assert margin["free_margin"] == "900"  # exchange balance, not the policy budget
        check = guards.total_margin(D(100), D(pending), D(0), D(1000), D(30), "open orders")
        warnings = [w for w in body["data"]["warnings"] if "max_total_margin_pct" in w]
        assert bool(warnings) is (not check.passed)
        if warnings:
            assert check.detail in warnings[0]
        assert reads.index("orders") < reads.index("summary")

    def test_pending_margin_uses_remaining_size_each_markets_leverage_and_sell_mid(self):
        buy = icp_order(9, "limit", "100", "10", reduce_only=False)
        buy.filled_size = D(4)  # only six remain: 6 * 100 / 5 = 120
        sell = icp_order(10, "limit", "90", "2", is_buy=False, reduce_only=False)  # 2 * 110 / 5 = 44
        trigger = icp_order(11, "trigger_entry", "120", "2", reduce_only=False)
        trigger.symbol, trigger.limit_price = "xyz:NVDA", D(130)  # 2 * 130 / 10 = 26
        trigger.market = "hip3_perp"
        protection = icp_order(12, "stop_loss", "80", "500")
        spot = icp_order(13, "limit", "100", "500", reduce_only=False)
        spot.symbol, spot.market = "@1", "spot"
        looked_up = []

        def leverage(symbol, *, address):
            assert address == MAIN
            looked_up.append(symbol)
            return Leverage(type="cross", value=D({"ICP": 5, "xyz:NVDA": 10}[symbol]))

        is_error, body = run("get_account_risk", {}, account_answers(
            get_positions=[], get_open_orders=[buy, sell, trigger, protection, spot],
            get_leverage=leverage, get_price={"ICP": 110.0}))

        assert not is_error, body
        assert D(body["data"]["margin"]["pending_margin"]) == 190
        assert looked_up == ["ICP", "xyz:NVDA"]

    @pytest.mark.parametrize("missing", ["leverage", "mid", "mid_request"])
    def test_unavailable_pending_inputs_keep_the_report_but_never_show_zero_budget_use(self, missing):
        changes = {"get_open_orders": [icp_order(9, "limit", "2.30", "100", is_buy=False, reduce_only=False)],
                   "get_price": {"ICP": 2.54}}
        if missing == "leverage":
            changes["get_leverage"] = NetworkException("unavailable")
        elif missing == "mid_request":
            changes["get_price"] = NetworkException("unavailable")
        else:
            changes["get_price"] = {}
        is_error, body = run("get_account_risk", {}, account_answers(**changes),
                             config_text=CONFIG + "max_total_margin_pct = 50\nmax_total_notional_x = 3\n")

        assert not is_error, body
        assert body["data"]["positions"]  # the useful account report survives an optional estimate failure
        margin = body["data"]["margin"]
        assert margin["pending_margin"] is None and margin["total_margin"] is None
        assert margin["remaining_margin_budget"] is None and margin["margin_limit"] == "750"
        assert any("pending entry margin" in w for w in body["data"]["warnings"])
        assert any("max_total_margin_pct cannot be checked" in w for w in body["data"]["warnings"])
        if missing != "leverage":
            assert any("total notional cannot be checked" in w for w in body["data"]["warnings"])
        else:
            assert not any("total notional cannot be checked" in w for w in body["data"]["warnings"])

    def test_no_pending_entries_need_no_leverage_or_mid_reads(self):
        protection = icp_order(1, "stop_loss", "2.62", "1301.6")
        spot = icp_order(2, "limit", "100", "500", reduce_only=False)
        spot.symbol, spot.market = "@1", "spot"
        is_error, body = run("get_account_risk", {}, account_answers(
            get_open_orders=[protection, spot], get_leverage=AssertionError("unneeded leverage read"),
            get_price=AssertionError("unneeded mid read")))

        assert not is_error, body
        margin = body["data"]["margin"]
        assert margin["pending_margin"] == "0" and margin["total_margin"] == "650"
        assert margin["margin_limit"] is None and margin["remaining_margin_budget"] is None
        assert margin["max_total_margin_pct"] is None

    @pytest.mark.parametrize("missing", ["account_value", "margin_used"])
    def test_unknown_account_figures_leave_the_policy_budget_unknown(self, missing):
        summary = AccountSummary(mode="portfolio_margin", account_value=D(1000), available_to_trade=None,
                                 margin_used=D(100), balances=[], dexes=[], sources={})
        setattr(summary, missing, None)
        is_error, body = run("get_account_risk", {}, account_answers(get_positions=[], get_open_orders=[],
                             get_account_summary=summary), config_text=CONFIG + "max_total_margin_pct = 30\n")

        assert not is_error, body
        margin = body["data"]["margin"]
        assert margin["pending_margin"] == "0" and margin["remaining_margin_budget"] is None
        assert any("max_total_margin_pct cannot be checked" in w for w in body["data"]["warnings"])

    def test_every_position_with_its_protection_and_the_totals(self):
        is_error, body = run("get_account_risk", {}, account_answers())
        data = body["data"]
        assert not is_error, body
        icp, = data["positions"]
        # Notional at the mark: Hyperliquid's position value, which needs no order book.
        assert (icp["symbol"], icp["side"], icp["unprotected_size"], icp["notional"]) == ("ICP", "short", "433.9", "3290.00")
        assert data["totals"]["if_all_stops"]["net_pnl"] == icp["if_all_stops"]["net_pnl"]
        assert (data["totals"]["short_notional"], data["totals"]["net_notional"], data["totals"]["unprotected_positions"]) \
            == ("3290.00", "-3290.00", 1)
        assert data["margin"] == {"account_value": "1500", "margin_used": "650", "free_margin": "800",
                                  "free_margin_percent": "53.33", "pending_margin": "46.00", "total_margin": "696.00",
                                  "max_total_margin_pct": None, "margin_limit": None, "remaining_margin_budget": None}
        assert data["daily_loss"] == {"loss_24h": "50.00", "percent_of_account": "3.33", "max_daily_loss_pct": "10"}
        assert any(w.startswith("ICP: ") and "433.9 has no stop" in w for w in data["warnings"])

    def test_todays_loss_and_the_total_notional_are_audited(self):
        # Trades in the Hyperliquid app are not checked when made: the audit reports what breaks the limits.
        from fractrade_hl_simple.models import DexAccount
        config = CONFIG + 'max_total_notional_x = 2\nday_start = "06:00"\ntz = "Europe/Athens"\n'
        answers = account_answers()
        answers["get_account_summary"].dexes = [DexAccount(dex=None, account_value=None, withdrawable=None,
                                                           margin_used=D(650), positions=answers["get_positions"])]
        is_error, body = run("get_account_risk", {"day_start": "08:00", "tz": "UTC"}, answers, config_text=config)
        data = body["data"]
        assert not is_error and data["day_loss"]["start"].endswith("T08:00:00.000Z")  # the call's day start
        assert any("x the account value" in w and "max_total_notional_x" in w for w in data["warnings"])  # 3290 > 2 x 1500

    def test_a_pending_sell_without_its_mid_leaves_the_total_notional_unchecked(self):
        config = CONFIG + 'max_total_notional_x = 3\n'
        answers = account_answers(get_open_orders=[icp_order(9, "limit", "2.30", "100", is_buy=False, reduce_only=False)],
                                  get_price={})
        is_error, body = run("get_account_risk", {}, answers, config_text=config)
        assert not is_error and any("total notional cannot be checked" in w for w in body["data"]["warnings"])

    def test_positions_that_break_the_guardrails_and_low_free_margin(self):
        config = CONFIG + "max_leverage = 3\nmax_total_margin_pct = 40\nsymbols = [\"BTC\"]\n"
        summary = account_answers()["get_account_summary"]
        summary.available_to_trade = D(100)
        is_error, body = run("get_account_risk", {}, account_answers(get_account_summary=summary), config_text=config)
        warnings = body["data"]["warnings"]
        assert not is_error, body
        assert "ICP: not in the account's symbols" in warnings
        assert "ICP: leverage 5 is above max_leverage 3" in warnings
        assert any("696 USD is above 40 %" in w and "max_total_margin_pct" in w for w in warnings)
        assert any("free margin is 6.67 %" in w for w in warnings)

    def test_with_beta_each_position_and_the_weighted_net_notional(self):
        # The same hourly closes as BTC: beta 1, so the weighted notional is the (short) notional.
        closes = [100 + (i % 5) for i in range(48)]
        is_error, body = run("get_account_risk", {"with_beta": True}, account_answers(get_candles=candles(closes)))
        data = body["data"]
        assert not is_error, body
        assert data["positions"][0]["beta_btc"] == "1.0"
        assert data["totals"]["btc_beta_weighted_notional"] == "-3290.00"

    def test_a_standard_accounts_free_margin_is_per_dex_without_its_spot_balance(self):
        # 9900 of the 10000 are spot assets, which back no perp position: 1 is free on the dex.
        summary = AccountSummary(mode="standard", account_value=D(10000), available_to_trade=None, margin_used=D(99),
                                 balances=[], dexes=[DexAccount(dex=None, account_value=D(100), withdrawable=D(1),
                                                                margin_used=D(99), positions=[])], sources={})
        _, body = run("get_account_risk", {}, account_answers(get_account_summary=summary))
        margin = body["data"]["margin"]
        assert (margin["free_margin"], margin["free_margin_percent"]) == ("1", "1.00")
        assert any("free margin on the default dex is 1.00 %" in w for w in body["data"]["warnings"])

    def test_the_daily_loss_if_every_stop_fills_counts_the_days_gains(self):
        # Up 300 today on a capital of 1000; the stops would take 400 from now: a loss of 100,
        # 10 % of the capital, which reaches max_daily_loss_pct 10.
        stop = icp_order(1, "stop_loss", "2.80", "1301.6")
        position = answers()["get_positions"][0]
        position.unrealized_pnl = D(0)
        loss = risk.outcomes(is_long=False, entry=position.entry_price, size=abs(position.size),
                             exits=[risk.Exit("stop_loss", D("2.80"), D("1301.6"), market=True)],
                             fees=risk.Fees(taker=D("0.00045"), maker=D("0.00015")),
                             book=risk.Book(bids=((D("2.53"), D(100000)),), asks=((D("2.54"), D(100000)),)),
                             account_value=D(1500))["if_all_stops"]["net_pnl"]
        gain = -loss - 100  # so that the loss from now is 100 more than the day's gain
        _, body = run("get_account_risk", {}, account_answers(
            get_open_orders=[stop], get_positions=[position],
            get_portfolio=day(["700", str(1000 + gain)], ["0", str(gain)])))
        assert any("if every stop filled now, the last 24 hours' loss would be 10.00 %" in w
                   for w in body["data"]["warnings"]), body["data"]["warnings"]


class TestPlanTrade:
    def test_a_short_sized_by_risk_pct(self):
        # 1 % of 1500: 15 USD. Market entry at about 2.53 (the bid), stop at 2.60.
        is_error, body = run("plan_trade", {"symbol": "ICP", "side": "short", "stop_loss": 2.60, "risk_pct": 1,
                                            "take_profits": [{"price": 2.40, "fraction": 0.5},
                                                             {"price": 2.30, "fraction": 0.5}]})
        data = body["data"]
        assert not is_error and data["risk_budget"] == "15.00"
        assert -15 <= float(data["if_all_stops"]["net_pnl"]) < -14
        assert data["entry"] == "market (taker)" and data["fees"]["entry_fee"] is not None
        assert float(data["margin"]) == pytest.approx(float(data["notional"]) / 5, abs=0.01)
        assert data["risk_reward"] is not None and float(data["unprotected_size"]) == 0

    def test_a_given_size(self):
        is_error, body = run("plan_trade", {"symbol": "ICP", "side": "long", "stop_loss": 2.40, "size": 100})
        assert not is_error and body["data"]["size"] == "100.0" and body["data"]["risk_budget"] is None
        assert body["data"]["execution"]["eligible"] is None  # watch-only account: no trading policy

    @pytest.mark.parametrize("arguments, says", [
        ({"side": "long", "stop_loss": 2.60, "size": 100}, "must be below the entry"),
        ({"side": "short", "stop_loss": 2.40, "size": 100}, "must be above the entry"),
        ({"side": "long", "stop_loss": 2.40, "size": 100, "take_profits": [{"price": 2.30, "fraction": 1}]},
         "must be above the entry"),
        ({"side": "long", "stop_loss": 2.40, "size": 100,
          "take_profits": [{"price": 2.7, "fraction": 0.6}, {"price": 2.8, "fraction": 0.6}]}, "more than 1"),
        ({"side": "long", "stop_loss": 2.40}, "exactly one of size, risk_usd and risk_pct"),
        ({"side": "long", "stop_loss": 2.40, "size": 100, "risk_usd": 10}, "exactly one of"),
        ({"side": "long", "stop_loss": 2.40, "size": 0.01}, "rounds to 0"),
        ({"side": "long", "stop_loss": 2.40, "size": 100, "time_in_force": "Ioc"}, "requires entry_price"),
    ])
    def test_invalid_plans_are_refused(self, arguments, says):
        is_error, body = run("plan_trade", {"symbol": "ICP", **arguments})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT" and says in body["error"]["message"]

    def test_an_order_below_the_minimum_is_named(self):
        is_error, body = run("plan_trade", {"symbol": "ICP", "side": "long", "stop_loss": 2.40, "size": 3})
        assert not is_error and any("below Hyperliquid's minimum order" in w for w in body["data"]["warnings"])
        assert body["data"]["execution"]["eligible"] is False

    def test_an_ioc_plan_uses_taker_fees_even_when_its_limit_cannot_fill_now(self):
        error, body = run("plan_trade", {"symbol": "ICP", "side": "long", "size": 100,
                                        "entry_price": 2.50, "time_in_force": "Ioc", "stop_loss": 2.40})
        assert not error, body
        assert body["data"]["entry"] == "limit (taker)"
        assert body["data"]["fees"]["entry_fee"] == "0.11"
        assert any("partly or not at all" in w for w in body["data"]["warnings"])

    def test_a_limit_entry_is_planned_at_its_price_as_a_maker(self):
        is_error, body = run("plan_trade", {"symbol": "ICP", "side": "long", "entry_price": 2.50,
                                            "stop_loss": 2.40, "size": 100})
        data = body["data"]
        assert not is_error and data["entry"] == "limit (maker)" and data["entry_price"] == "2.5000000"
        # 100 × (2.50 - 2.40) = 10, plus the stop's slippage into a bid 0.005 below the mid, and fees
        assert -11 < float(data["if_all_stops"]["net_pnl"]) < -10 and data["notional"] == "250.00"

    def test_a_take_profit_below_the_lot_size_is_named_not_dropped_silently(self):
        is_error, body = run("plan_trade", {"symbol": "ICP", "side": "long", "stop_loss": 2.40, "size": 5,
                                            "take_profits": [{"price": 2.7, "fraction": 0.01}]})
        assert not is_error and body["data"]["take_profits"] == []
        assert any("rounds to 0 at the lot size" in w for w in body["data"]["warnings"])

    def test_a_risk_sized_plan_never_loses_more_than_the_risk(self):
        is_error, body = run("plan_trade", {"symbol": "ICP", "side": "long", "stop_loss": 2.45, "risk_usd": 20})
        assert not is_error and -20 <= float(body["data"]["if_all_stops"]["net_pnl"]) < -19


class TestSentPrices:
    def test_stops_are_estimated_at_the_prices_as_sent_and_can_merge(self):
        from fractrade_hl_simple.mcp_server.tools.risk import sent_levels
        levels = sent_levels(FakeClient({}), "BTC", True, [{"price": 80000.1, "fraction": 0.5},
                                                          {"price": 80000.2, "fraction": 0.5}])
        assert levels == [(D(80000), D("0.5")), (D(80000), D("0.5"))]  # one trigger: they share the book

    @pytest.mark.parametrize("entry_price, signed", [
        (None, None), ("2.50", "2.5"), ("2.60", "2.6"),  # at market, a passive limit, a crossing one
        ("2.60006", "2.6001"),                           # rounded to 5 significant figures as it is signed
    ])
    def test_the_plans_risk_warning_compares_the_exact_loss(self, entry_price, signed):
        # The loss shown in cents can equal the limit while the exact one is above it; a limit
        # entry is measured from its limit at the taker fee, as open_position checks it.
        arguments = {"symbol": "ICP", "side": "long", "stop_loss": 2.40, "size": 100,
                     **({"entry_price": float(entry_price)} if entry_price else {})}
        limit = D(signed) if signed else None
        exact = risk.planned_loss(D(100), is_long=True, limit=limit, resting=False, stops=[(D("2.4"), D(1))],
                                  fees=risk.Fees(taker=D("0.00045"), maker=D("0.00015")),
                                  book=risk.Book(bids=((D("2.53"), D(100000)),), asks=((D("2.54"), D(100000)),)),
                                  size_decimals=1)
        for pct, warns in ((exact / 15 - D("0.0000001"), True), (exact / 15 + D("0.0000001"), False)):
            config_text = CONFIG.replace("max_daily_loss_pct = 10", f"max_risk_pct = {pct:.10f}")
            is_error, body = run("plan_trade", arguments, config_text=config_text)
            assert not is_error
            assert any("max_risk_pct" in w for w in body["data"]["warnings"]) is warns


class TestPartialFillsNeverLoseMore:
    """The loss at the stops grows with the size, slippage included: a partial fill of a
    checked size never loses more at its stops, and sizing by risk is a sound search."""

    FEES = risk.Fees(taker=D("0.00045"), maker=D("0.00015"))

    def lost(self, size, book, stops, decimals=0):
        return risk.planned_loss(D(size), is_long=True, limit=None, resting=False, stops=stops, fees=self.FEES,
                                 book=book, size_decimals=decimals)

    def test_a_thin_book_where_the_nearest_stop_paid_more_slippage(self):
        # Estimated each on its own, 7 lost 41.41 and 8 lost 35.89: a 7 fill exceeded a risk of 40 that 8 passed.
        book = risk.Book(bids=((D("99.9"), D(2)), (D(90), D(98))), asks=((D("100.1"), D(100)),))
        stops = [(D(99), D("0.5")), (D(98), D("0.25")), (D(97), D("0.25"))]
        losses = [self.lost(n, book, stops) for n in range(1, 101)]
        assert all(a <= b for a, b in zip(losses, losses[1:]))
        size, _ = risk.size_for_risk(is_long=True, limit=None, resting=False, stops=stops, risk=D(40), fees=self.FEES,
                                     book=book, size_decimals=0)
        assert all(self.lost(n, book, stops) <= 40 for n in range(1, int(size) + 1))

    @pytest.mark.parametrize("seed", range(20))
    def test_over_many_thin_books_and_stops(self, seed):
        import random
        rng = random.Random(seed)
        bids, price = [], D(100)
        for _ in range(rng.randint(1, 5)):
            price -= D(rng.randint(1, 500)) / 100
            bids.append((price, D(rng.randint(1, 40))))
        book = risk.Book(bids=tuple(bids), asks=((D("100.05"), D(10**6)),))
        weights = [rng.randint(1, 9) for _ in range(rng.randint(1, 4))]
        stops = [(D(rng.randint(50, 99)), D(w) / sum(weights)) for w in weights]
        stops[-1] = (stops[-1][0], 1 - sum((f for _, f in stops[:-1]), D(0)))  # exactly 1 together
        losses = [self.lost(n, book, stops) for n in range(1, int(sum(level[1] for level in bids)) + 1)]
        known = [loss for loss in losses if loss is not None]
        assert all(a <= b for a, b in zip(known, known[1:]))


class TestObservationMetadata:
    @pytest.mark.parametrize("name, arguments", [
        ("get_position_risk", {"symbol": "ICP"}),
        ("get_account_risk", {}),
        ("plan_trade", {"symbol": "ICP", "side": "long", "size": 100, "stop_loss": 2.4}),
    ])
    def test_scope_and_timestamp_cover_all_risk_reads(self, monkeypatch, name, arguments):
        from fractrade_hl_simple.mcp_server.tools import read
        from fractrade_hl_simple.mcp_server.convert import iso
        clock = [1_795_000_000.0]
        original = FakeClient.__getattr__

        def delayed(self, method):
            call = original(self, method)

            def finish(*args, **kwargs):
                answer = call(*args, **kwargs)
                clock[0] += 1
                return answer
            return finish

        monkeypatch.setattr(FakeClient, "__getattr__", delayed)
        monkeypatch.setattr(read.time, "time", lambda: clock[0])
        error, body = run(name, arguments, account_answers() if name == "get_account_risk" else None)
        assert not error, body
        assert body["observed_at"] == iso(int(clock[0] * 1000))
        assert clock[0] > 1_795_000_000.0
        assert body["scope"] == {"perp_dexs": [""], "symbol": arguments.get("symbol")}
        if name == "plan_trade":
            assert body["observed_at"] == body["data"]["execution"]["observed_at"]

    def test_planning_scope_uses_the_account_reader_not_the_broader_network_reader(self):
        from fractrade_hl_simple.mcp_server.tools.risk import risk_tools
        conf = parse_config('''
[accounts.main]
address = "0x1111111111111111111111111111111111111111"
[accounts.other]
address = "0x2222222222222222222222222222222222222222"
perp_dexs = ["xyz"]
''', "scope.toml")
        scopes = []

        def factory(base_url, account, default_slippage, perp_dexs):
            assert account is None
            client = FakeClient(answers())
            client.perp_dexs = ["", *perp_dexs]
            scopes.append(client.perp_dexs)
            return client

        registry = AccountRegistry(conf, {}, factory)
        tool = next(t for t in risk_tools(registry) if t.name == "plan_trade")
        body = tool.run({"account": "main", "symbol": "ICP", "side": "long", "size": 100, "stop_loss": 2.4})
        assert scopes == [["", "xyz"], [""]]
        assert body["scope"] == {"perp_dexs": [""], "symbol": "ICP"}
        assert registry._traders == {}
