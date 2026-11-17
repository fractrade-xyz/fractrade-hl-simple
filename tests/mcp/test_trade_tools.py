"""The trade tools: what is signed, the guards, the cloid, the halt and the
signer, through the server and the real library client with the SDK mocked."""

import json
from decimal import Decimal
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
import requests  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402
from mcp import Client  # noqa: E402

from fractrade_hl_simple.exceptions import NetworkException  # noqa: E402
from fractrade_hl_simple.hyperliquid import HyperliquidClient  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.server import build_server  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import read_tools  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.trade import trade_tools  # noqa: E402

KEY = "0x" + "ab" * 32
OWN = "0x" + "1" * 40  # main's address; KEY is an API wallet of it (see Session)
OTHER = "0x" + "4" * 40
CLOID = "0x" + "c" * 32
META = [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                      {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
                      {"name": "HYPE", "szDecimals": 2, "maxLeverage": 3, "marginMode": "noCross"}]},
        [{"funding": "0.0001", "openInterest": "1000", "markPx": "85000"},
         {"funding": "0.00005", "openInterest": "5000", "markPx": "3200"},
         {"funding": "0.00001", "openInterest": "100", "markPx": "40"}]]
# What the mocked Info answers to post(): the market data, and the account's state the risk
# guardrails read (activeAssetData: the market's leverage; portfolio: the last 24 hours).
ANSWERS = {"metaAndAssetCtxs": META, "activeAssetData": {"leverage": {"type": "cross", "value": 5}},
           "portfolio": [["day", {"accountValueHistory": [[0, "1000"]], "pnlHistory": [[0, "0"], [1, "0"]]}]]}
FILLED = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
    {"filled": {"oid": 3, "totalSz": "0.001", "avgPx": "85010.0"}}]}}}
RESTING = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 4}}]}}}


def config(address=OWN, extra=""):
    return parse_config(f'''
[accounts.main]
address = "{address}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5
max_slippage = 0.01
{extra}

[accounts.whale]
address = "{OTHER}"
''', "test.toml")


CONFIG_CAP = f'''
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 99.9999
max_leverage = 5
'''


def cancel_answer(status):
    return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [status]}}}


def market(base_url, account, default_slippage, perp_dexs):
    """The client factory: the real library client, with the SDK's Info and Exchange mocked,
    so what a tool would sign is what the mocked Exchange is called with."""
    with patch("fractrade_hl_simple.hyperliquid.Info") as info_cls, \
         patch("fractrade_hl_simple.hyperliquid.Exchange"):
        info = info_cls.return_value
        info.post.side_effect = lambda path, payload: ANSWERS[payload["type"]]
        info.name_to_coin = {"BTC": "BTC", "ETH": "ETH", "HYPE": "HYPE"}
        info.all_mids.return_value = {"BTC": "85000.0", "ETH": "3000.0"}
        options = {} if default_slippage is None else {"default_slippage": default_slippage}
        client = HyperliquidClient(account=account, base_url=base_url, trust_env=False, max_retries=0,
                                   cache_market_specs=False, perp_dexs=list(perp_dexs), **options)
    HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None  # mock data stays here
    return client


class Session:
    """A server with the read and trade tools, and the trading client of account main."""

    def __init__(self, conf=None, keys=None, make_client=market, state=None):
        conf = conf or config()
        self.registry = AccountRegistry(conf, {"main": KEY} if keys is None else keys, make_client,
                                        state or shared_state())
        self.server = build_server([*read_tools(conf, self.registry), *trade_tools(self.registry)])
        self.target = self.registry.resolve_trade("main")
        # The key is an API wallet of main (the signer check asks Hyperliquid's userRole).
        try:
            reader = self.registry.read_client(self.target)
        except Exception:  # a test whose network is down: no signer check gets that far
            return
        if hasattr(reader, "info"):
            reader.info.user_role.return_value = {"role": "agent", "data": {"user": self.target.address}}

    @property
    def trader(self):
        return self.registry.trade_client(self.target)

    def call(self, *calls):
        """(is_error, body) of each call, in order, on one connection."""
        async def run():
            async with Client(self.server, mode="2026-07-28") as client:
                results = []
                for name, arguments in calls:
                    result = await client.call_tool(name, arguments)
                    results.append((result.is_error, json.loads(result.content[0].text)))
                return results
        return anyio.run(run)

    def one(self, name, arguments):
        return self.call((name, arguments))[0]

    def code(self, name, arguments):
        is_error, body = self.one(name, arguments)
        assert is_error, body
        return body["error"]["code"]

    def tools(self):
        async def run():
            async with Client(self.server, mode="2026-07-28") as client:
                return {tool.name: tool for tool in (await client.list_tools()).tools}
        return anyio.run(run)


def sent(client):
    """The arguments of the last SDK order call, without the client order id."""
    return {k: v for k, v in client.exchange.order.call_args.kwargs.items() if k != "cloid"}


@pytest.fixture
def session():
    session = Session()
    session.trader.exchange.order.return_value = FILLED
    return session


class TestOrders:
    def test_a_market_buy_signs_what_the_librarys_buy_signs(self, session):
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.0011345})
        by_tool = sent(session.trader)
        session.trader.buy("BTC", 0.0011345)
        assert not is_error and by_tool == sent(session.trader)
        # Without slippage: the account's max_slippage, 1 %, as the client's default.
        assert (by_tool["sz"], by_tool["limit_px"], by_tool["order_type"]) == (
            0.00113, 85850.0, {"limit": {"tif": "Ioc"}})
        order = body["data"]
        assert (order["order_id"], order["status"], order["filled_size"], order["average_fill_price"]) == (
            3, "filled", "0.001", "85010.0")
        assert len(order["cloid"]) == 34 and body["account"] == "main"

    def test_a_limit_order_with_post_only_rests_as_maker_under_the_cloid_given(self, session):
        session.trader.exchange.order.return_value = RESTING
        is_error, body = session.one("sell", {"symbol": "BTC", "size": 0.001, "limit_price": 90000.55,
                                              "post_only": True, "cloid": CLOID})
        assert not is_error and (body["data"]["status"], body["data"]["cloid"]) == ("open", CLOID)
        assert (sent(session.trader)["limit_px"], sent(session.trader)["order_type"]) == (
            90001.0, {"limit": {"tif": "Alo"}})
        assert session.trader.exchange.order.call_args.kwargs["cloid"].to_raw() == CLOID
        # A sell is valued at the mid if that is higher (it can fill there): one price read.
        session.trader.info.all_mids.assert_called_once_with(dex="")

    def test_post_only_without_a_limit_price_is_refused_and_keeps_the_cloid_free(self, session):
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "post_only": True, "cloid": CLOID}) == \
            "INVALID_ARGUMENT"
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]

    def test_an_order_is_logged_in_one_line_whatever_the_symbol(self, session, caplog):
        with caplog.at_level(logging.INFO, logger="fractrade_hl_simple.mcp_server.tools.trade"):
            session.one("buy", {"symbol": "BTC", "size": 0.001})
            session.one("buy", {"symbol": "BTC", "size": 1.0})
            session.one("buy", {"symbol": "BTC\nforged entry", "size": 0.001})
        assert [r.getMessage() for r in caplog.records] == [
            "buy 'BTC' (account main): order 3 filled", "buy 'BTC' (account main): GUARD_BLOCKED",
            "buy 'BTC\\nforged entry' (account main): UNKNOWN_SYMBOL"]

    def test_the_price_is_read_once_and_that_price_is_checked_and_signed(self, session):
        session.trader.info.all_mids.side_effect = [{"BTC": "85000.0"}, {"BTC": "99000.0"}]
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert session.trader.info.all_mids.call_count == 1 and sent(session.trader)["limit_px"] == 85850.0


class TestGuards:
    def test_an_order_above_max_order_usd_is_not_sent_and_its_cloid_stays_free(self, session):
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.002, "limit_price": 85000, "cloid": CLOID})
        assert is_error and body["error"]["code"] == "GUARD_BLOCKED"
        assert {c["rule"]: c["passed"] for c in body["error"]["details"]["checks"]}["order_notional"] is False
        session.trader.exchange.order.assert_not_called()
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "limit_price": 85000, "cloid": CLOID})[0]

    def test_the_rounded_price_is_checked_not_the_one_asked_for(self):
        # 0.001 at 99999.6 is 99.9996, below the cap; signed at 100000 (5 significant figures) it is 100.
        session = Session(parse_config(CONFIG_CAP, "test.toml"))
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "limit_price": 99999.6}) == "GUARD_BLOCKED"
        session.trader.exchange.order.assert_not_called()

    def test_more_slippage_than_the_account_allows_is_blocked(self, session):
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "slippage": 0.05}) == "GUARD_BLOCKED"
        session.trader.exchange.order.assert_not_called()

    def test_a_reduce_only_order_is_never_blocked_by_the_allowlist_or_the_notional_cap(self):
        session = Session(config(extra='symbols = ["ETH"]'))
        session.trader.exchange.order.return_value = FILLED
        is_error, _ = session.one("sell", {"symbol": "BTC", "size": 0.01, "reduce_only": True})
        assert not is_error and sent(session.trader)["reduce_only"] is True
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "GUARD_BLOCKED"  # not reduce-only

    def test_a_modify_is_checked_like_a_new_order(self, session):
        modify = {"order_id": 4, "symbol": "BTC", "is_buy": True, "size": 0.002, "price": 85000,
                  "order_type": {"limit": {"tif": "Gtc"}}}
        assert session.code("modify_order", modify) == "GUARD_BLOCKED"
        session.trader.exchange.modify_order.assert_not_called()

    def test_a_cancel_of_a_spot_pair_is_refused(self, session):
        # The library's cancel_order also cancels a spot order by its pair name.
        assert session.code("cancel_order", {"order_id": 4, "symbol": "PURR/USDC"}) == "GUARD_BLOCKED"
        session.trader.exchange.cancel.assert_not_called()


class TestCloid:
    def test_a_cloid_the_server_made_is_refused_when_passed_again(self, session):
        made = session.one("buy", {"symbol": "BTC", "size": 0.001})[1]["data"]["cloid"]
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": made}) == "INVALID_ARGUMENT"
        assert session.trader.exchange.order.call_args.kwargs["cloid"].to_raw() == made  # it was sent with it

    def test_a_cloid_is_free_again_when_the_order_provably_was_not_sent(self, session):
        # A connect timeout: not processed (the library's contract), so the same call can be made again.
        session.trader.exchange.order.side_effect = [requests.ConnectTimeout("connect timed out"), FILLED]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})
        assert is_error and (body["error"]["code"], body["error"]["retryable"]) == ("TIMEOUT", True)
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]

    def test_a_cloid_stays_used_when_the_order_may_have_been_sent(self, session):
        session.trader.exchange.order.side_effect = requests.ConnectionError("connection reset")
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID}) == "OUTCOME_UNKNOWN"
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID}) == "INVALID_ARGUMENT"
        assert session.trader.exchange.order.call_count == 1

    def test_a_cloid_used_before_is_refused_in_any_case(self, session):
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID.upper().replace("0X", "0x")})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"
        assert "get_order_status_by_cloid" in body["error"]["hint"]
        assert session.trader.exchange.order.call_count == 1

    def test_a_cloid_stays_used_when_the_order_was_rejected(self, session):
        session.trader.exchange.order.return_value = {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"error": "Order must have minimum value of $10."}]}}}
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.0001, "limit_price": 85000, "cloid": CLOID})
        assert is_error and body["error"]["code"] == "ORDER_REJECTED"
        assert body["error"]["details"]["reason"] == "Order must have minimum value of $10."
        assert session.code("buy", {"symbol": "BTC", "size": 0.0001, "limit_price": 85000, "cloid": CLOID}) == \
            "INVALID_ARGUMENT"

    def test_an_order_whose_outcome_is_unknown_names_its_cloid_and_how_to_find_it(self, session):
        session.trader.exchange.order.return_value = {"status": "ok", "response": "not the documented format"}
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):  # the library looks the order up three times
            is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})
        error = body["error"]
        assert is_error and (error["code"], error["details"]["cloid"], error["details"]["symbol"]) == (
            "OUTCOME_UNKNOWN", CLOID, "BTC")
        assert error["hint"].startswith("do not place the order again: look it up with get_order_status_by_cloid")
        assert session.trader.exchange.order.call_count == 1  # never sent twice

    def test_the_order_is_found_by_its_cloid(self, session):
        session.registry.read_client(session.target).get_order_status_by_cloid = lambda cloid, address: {
            "status": "order", "order": {"status": "filled", "statusTimestamp": 1_700_000_000_000, "order": {
                "coin": "BTC", "side": "B", "limitPx": "85850.0", "sz": "0.0", "oid": 3, "timestamp": 1_700_000_000_000,
                "origSz": "0.001", "cloid": cloid, "orderType": "Limit", "tif": "Ioc", "reduceOnly": False}}}
        is_error, body = session.one("get_order_status_by_cloid", {"account": "main", "cloid": CLOID})
        assert not is_error and (body["data"]["found"], body["data"]["status"], body["data"]["order"]["cloid"]) == (
            True, "filled", CLOID)


class TestModifyAndCancel:
    def test_a_modified_order_is_returned_with_the_id_hyperliquid_gave_it(self, session):
        session.trader.exchange.modify_order.return_value = {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"resting": {"oid": 99}}]}}}
        is_error, body = session.one("modify_order", {"order_id": 4, "symbol": "BTC", "is_buy": True, "size": 0.001,
                                                      "price": 80000, "order_type": {"limit": {"tif": "Gtc"}}})
        assert not is_error and body["data"]["order_id"] == 99

    @pytest.mark.parametrize("tool, arguments", [
        ("cancel_order", {"order_id": 4}), ("cancel_order_by_cloid", {"cloid": CLOID})])
    @pytest.mark.parametrize("symbol, says", [
        ("btc", "Unknown symbol 'btc'"), ("xyz:NVDA", "the xyz dex is not enabled; enable_perp_dexs adds it")])
    def test_a_cancel_of_an_unknown_market_is_unknown_symbol_and_signs_nothing(self, session, tool, arguments,
                                                                               symbol, says):
        is_error, body = session.one(tool, {**arguments, "symbol": symbol})
        assert is_error and body["error"]["code"] == "UNKNOWN_SYMBOL" and says in body["error"]["message"]
        session.trader.exchange.cancel.assert_not_called()
        session.trader.exchange.cancel_by_cloid.assert_not_called()

    def test_a_modify_sends_the_prepared_order(self, session):
        session.trader.exchange.modify_order.return_value = RESTING
        is_error, body = session.one("modify_order", {"order_id": 4, "symbol": "BTC", "is_buy": True,
                                                      "size": 0.0010009, "price": 80000.55,
                                                      "order_type": {"limit": {"tif": "Alo"}}})
        kwargs = session.trader.exchange.modify_order.call_args.kwargs
        assert not is_error and body["data"]["status"] == "open"
        assert (kwargs["oid"], kwargs["sz"], kwargs["limit_px"], kwargs["order_type"]) == (
            4, 0.001, 80001.0, {"limit": {"tif": "Alo"}})

    def test_a_trigger_order_type_is_not_accepted(self, session):
        # Hyperliquid does not modify trigger orders; update_stop_loss moves a stop.
        assert session.code("modify_order", {"order_id": 4, "symbol": "BTC", "is_buy": False, "size": 0.001,
                                             "price": 80000, "order_type": {"trigger": {"triggerPx": 80000}}}) == \
            "INVALID_ARGUMENT"

    def test_cancels_say_whether_the_order_was_open(self, session):
        session.trader.exchange.cancel.return_value = cancel_answer("success")
        session.trader.exchange.cancel_by_cloid.return_value = cancel_answer(
            {"error": "Order was never placed, already canceled, or filled. asset=0"})
        assert session.one("cancel_order", {"order_id": 4, "symbol": "BTC"})[1]["data"] == {"canceled": True}
        assert session.one("cancel_order_by_cloid", {"cloid": CLOID, "symbol": "BTC"})[1]["data"] == {"canceled": False}
        symbol, cloid = session.trader.exchange.cancel_by_cloid.call_args.args
        assert (symbol, cloid.to_raw()) == ("BTC", CLOID)


class TestConcurrency:
    """Two calls for one account at once: the account lock serializes them."""

    def run_two(self, session, first, second, between=lambda: None):
        tools = {spec.name: spec for spec in trade_tools(session.registry)}
        started, release, results = threading.Event(), threading.Event(), {}

        def slow_order(**kwargs):
            started.set()
            release.wait(5)
            return FILLED

        session.trader.exchange.order.side_effect = slow_order

        def call(label, name, arguments):
            try:
                results[label] = tools[name].run(arguments)
            except Exception as error:
                results[label] = error

        one = threading.Thread(target=call, args=("first", *first))
        one.start()
        assert started.wait(5)
        two = threading.Thread(target=call, args=("second", *second))
        two.start()
        between()
        release.set()
        one.join(5), two.join(5)
        return results

    def test_the_same_cloid_twice_at_once_is_sent_once(self, session):
        order = ("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})
        results = self.run_two(session, order, order)
        assert results["first"]["data"]["cloid"] == CLOID and results["second"].code == "INVALID_ARGUMENT"
        assert session.trader.exchange.order.call_count == 1

    def test_a_call_waiting_for_the_lock_does_not_run_after_a_halt(self, session):
        order = ("buy", {"symbol": "BTC", "size": 0.001})
        results = self.run_two(session, order, order, between=lambda: session.registry.halt(session.target))
        assert results["first"]["data"]["status"] == "filled" and results["second"].code == "KILL_SWITCH_ENGAGED"


class TestAccountState:
    def test_while_halted_only_risk_reducing_calls_run(self, session):
        session.registry.halt(session.target)
        session.trader.exchange.cancel.return_value = cancel_answer("success")
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and body["error"]["code"] == "KILL_SWITCH_ENGAGED" and "reduce_only" in body["error"]["hint"]
        assert not session.one("sell", {"symbol": "BTC", "size": 0.001, "reduce_only": True})[0]
        assert not session.one("cancel_order", {"order_id": 4, "symbol": "BTC"})[0]
        assert session.trader.exchange.order.call_count == 1  # only the reduce-only sell

    def test_a_key_that_does_not_trade_the_account_sends_nothing(self):
        def factory(base_url, account, default_slippage, perp_dexs):
            client = market(base_url, account, default_slippage, perp_dexs)
            client.get_user_role = lambda address: {"role": "missing"}
            return client

        session = Session(config(address=OTHER.replace("4", "5")), make_client=factory)
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "SIGNER_NOT_AUTHORIZED"
        session.trader.exchange.order.assert_not_called()

    def test_the_key_is_looked_up_again_before_an_order_that_adds_risk_but_not_before_a_close(self, session):
        reader = session.registry.read_client(session.target)
        agent = {"role": "agent", "data": {"user": session.target.address}}
        reader.info.user_role.side_effect = [agent, agent, {"role": "missing"}]
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.002")
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]      # lookup 1
        assert not session.one("close", {"symbol": "BTC"})[0]                    # the kept result
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]      # lookup 2
        sent_before = session.trader.exchange.order.call_count
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "SIGNER_NOT_AUTHORIZED"  # lookup 3
        assert session.trader.exchange.order.call_count == sent_before and reader.info.user_role.call_count == 3

    def test_a_watch_account_cannot_trade(self, session):
        assert session.code("buy", {"account": "whale", "symbol": "BTC", "size": 0.001}) == "TRADING_DISABLED"

    def test_order_tools_are_destructive_and_cancels_idempotent(self, session):
        tools = session.tools()
        trade = [spec.name for spec in trade_tools(session.registry)]
        hints = {name: (tools[name].annotations.read_only_hint, tools[name].annotations.destructive_hint,
                        tools[name].annotations.idempotent_hint) for name in trade}
        idempotent = {"cancel_order", "cancel_order_by_cloid", "cancel_all_orders", "set_leverage", "kill_switch"}
        # set_leverage and kill_switch change nothing that cannot be changed back: no extra friction in an emergency.
        not_destructive = {"set_leverage", "kill_switch"}
        assert hints == {name: (False, name not in not_destructive, name in idempotent) for name in trade}


def state(*positions):
    """clearinghouseState with these (coin, size) positions."""
    summary = {"accountValue": "1000", "totalMarginUsed": "100", "totalNtlPos": "500", "totalRawUsd": "1000"}
    return {"marginSummary": summary, "crossMarginSummary": summary, "withdrawable": "800", "assetPositions": [
        {"type": "oneWay", "position": {
            "coin": coin, "entryPx": "85000", "szi": size, "leverage": {"type": "cross", "value": "10"},
            "liquidationPx": None, "marginUsed": "8.5", "positionValue": "85", "returnOnEquity": "0.01",
            "unrealizedPnl": "1.0"}} for coin, size in positions]}


def filled(oid, size):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"oid": oid, "totalSz": size, "avgPx": "84990.0"}}]}}}


def resting(oid):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": oid}}]}}}


class TestClose:
    def test_close_signs_a_reduce_only_market_order_for_the_whole_position(self):
        session = Session(config(extra='symbols = ["ETH"]'))  # BTC is outside the allowlist
        session.registry.halt(session.target)  # and the account is halted
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.002")
        is_error, body = session.one("close", {"symbol": "BTC"})
        assert not is_error and (body["data"]["order_id"], body["data"]["status"]) == (5, "filled")
        signed = sent(session.trader)
        # A sell for a long, at the mid minus the account's max_slippage (1 %).
        assert (signed["is_buy"], signed["sz"], signed["limit_px"], signed["reduce_only"], signed["order_type"]) == (
            False, 0.002, 84150.0, True, {"limit": {"tif": "Ioc"}})

    def test_a_close_that_filled_partly_says_what_is_still_open(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.0015")
        is_error, body = session.one("close", {"symbol": "BTC"})
        error = body["error"]
        assert is_error and (error["code"], error["details"]["remaining"], error["details"]["order"]["order_id"]) == (
            "POSITION_NOT_CLOSED", "0.0005", 5)
        # The closing order's cloid, made by the library, is not free for a new order.
        cloid = error["details"]["order"]["cloid"]
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": cloid}) == "INVALID_ARGUMENT"

    def test_close_with_more_slippage_than_allowed_is_blocked(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        assert session.code("close", {"symbol": "BTC", "slippage": 0.05}) == "GUARD_BLOCKED"
        session.trader.exchange.order.assert_not_called()

    def test_close_without_a_position(self, session):
        session.trader.info.user_state.return_value = state()
        assert session.code("close", {"symbol": "BTC"}) == "POSITION_NOT_FOUND"

    def test_close_all_positions_tries_every_position_and_reports_each(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"), ("ETH", "-0.1"))
        rejected = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"error": "Order could not immediately match against any resting orders."}]}}}
        session.trader.exchange.order.side_effect = [filled(5, "0.002"), rejected]
        is_error, body = session.one("close_all_positions", {})
        error = body["error"]
        assert is_error and error["code"] == "CLOSE_POSITIONS_INCOMPLETE"
        assert error["details"]["closed"]["BTC"]["order_id"] == 5
        failed = error["details"]["failed"]["ETH"]
        assert (failed["code"], failed["details"]["remaining"], failed["details"]["order"]) == (
            "POSITION_NOT_CLOSED", "0.1", None)

    def test_close_all_positions_returns_the_closing_order_per_symbol(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.002")
        is_error, body = session.one("close_all_positions", {})
        assert not is_error and list(body["data"]["closed"]) == ["BTC"]


class TestProtection:
    def test_a_stop_loss_closes_the_position_side_and_triggers_at_its_price(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "-0.002"))  # a short
        session.trader.exchange.order.return_value = resting(7)
        is_error, body = session.one("stop_loss", {"symbol": "BTC", "size": 0.002, "trigger_price": 90000.55,
                                                   "cloid": CLOID})
        signed = session.trader.exchange.order.call_args.kwargs
        assert not is_error and (body["data"]["type"], body["data"]["status"], body["data"]["cloid"]) == (
            "stop_loss", "open", CLOID)
        assert (signed["is_buy"], signed["reduce_only"], signed["order_type"]) == (
            True, True, {"trigger": {"triggerPx": 90001.0, "isMarket": True, "tpsl": "sl"}})

    def test_a_take_profit_without_a_position_leaves_its_cloid_free(self, session):
        session.trader.info.user_state.return_value = state()
        arguments = {"symbol": "BTC", "size": 0.002, "trigger_price": 95000, "cloid": CLOID}
        assert session.code("take_profit", arguments) == "POSITION_NOT_FOUND"
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = resting(8)
        assert not session.one("take_profit", arguments)[0]

    def test_protection_is_allowed_while_halted_and_outside_the_allowlist(self):
        session = Session(config(extra='symbols = ["ETH"]'))
        session.registry.halt(session.target)
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = resting(7)
        assert not session.one("stop_loss", {"symbol": "BTC", "size": 0.002, "trigger_price": 80000})[0]

    def test_moving_a_stop_loss_places_the_new_one_then_cancels_the_old(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.info.frontend_open_orders.return_value = [{
            "coin": "BTC", "side": "A", "limitPx": "80000.0", "sz": "0.002", "oid": 6, "timestamp": 1_700_000_000_000,
            "origSz": "0.002", "triggerPx": "80000.0", "isTrigger": True, "orderType": "Stop Market",
            "reduceOnly": True, "tif": None, "cloid": None}]
        session.trader.exchange.order.return_value = resting(9)
        session.trader.exchange.cancel.return_value = cancel_answer("success")
        is_error, body = session.one("update_stop_loss", {"symbol": "BTC", "new_price": 81000})
        assert not is_error and (body["data"]["order_id"], body["data"]["trigger_price"]) == (9, "81000.0")
        assert session.trader.exchange.cancel.call_args.args == ("BTC", 6)
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": body["data"]["cloid"]}) == \
            "INVALID_ARGUMENT"

    def test_a_replacement_that_does_not_cover_the_position_keeps_the_old_one_and_says_so(self, session):
        # Read for the size, for the new order's side, and again to check: it grew meanwhile.
        session.trader.info.user_state.side_effect = [state(("BTC", "0.002"))] * 2 + [state(("BTC", "0.003"))]
        session.trader.info.frontend_open_orders.return_value = []
        session.trader.exchange.order.return_value = resting(9)
        is_error, body = session.one("update_stop_loss", {"symbol": "BTC", "new_price": 81000})
        error = body["error"]
        assert is_error and error["code"] == "PROTECTION_REPLACEMENT_INCOMPLETE"
        assert (error["details"]["new_order"]["order_id"], error["details"]["old_order_ids"],
                error["details"]["position"]["size"]) == (9, [], "0.003")


class TestLibraryCloids:
    """Every cloid an agent can see is refused as a new order's: also those the library made."""

    def reused(self, session, cloid):
        sent_before = session.trader.exchange.order.call_count
        refused = session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": cloid}) == "INVALID_ARGUMENT"
        return refused and session.trader.exchange.order.call_count == sent_before

    def test_a_close_whose_outcome_is_unknown(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.side_effect = requests.ReadTimeout("read timed out")
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            is_error, body = session.one("close", {"symbol": "BTC"})
        assert is_error and body["error"]["code"] == "OUTCOME_UNKNOWN"
        session.trader.exchange.order.side_effect = None
        assert self.reused(session, body["error"]["details"]["cloid"])

    def test_a_stop_loss_replacement_whose_outcome_is_unknown(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.info.frontend_open_orders.return_value = []
        session.trader.exchange.order.side_effect = requests.ReadTimeout("read timed out")
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            is_error, body = session.one("update_stop_loss", {"symbol": "BTC", "new_price": 81000})
        assert is_error and body["error"]["code"] == "OUTCOME_UNKNOWN"
        session.trader.exchange.order.side_effect = None
        assert self.reused(session, body["error"]["details"]["cloid"])

    def test_a_partial_fill_inside_close_all_positions(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.0015")
        is_error, body = session.one("close_all_positions", {})
        failed = body["error"]["details"]["failed"]["BTC"]
        assert is_error and failed["details"]["remaining"] == "0.0005"
        session.trader.exchange.order.return_value = FILLED
        assert self.reused(session, failed["details"]["order"]["cloid"])


class TestReplacementOrder:
    def setup(self, session):
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.info.frontend_open_orders.return_value = [{
            "coin": "BTC", "side": "A", "limitPx": "80000.0", "sz": "0.002", "oid": 6, "timestamp": 1_700_000_000_000,
            "origSz": "0.002", "triggerPx": "80000.0", "isTrigger": True, "orderType": "Stop Market",
            "reduceOnly": True, "tif": None, "cloid": None}]
        session.trader.exchange.order.return_value = resting(9)

    def test_the_new_stop_is_placed_before_the_old_one_is_canceled(self, session):
        self.setup(session)
        session.trader.exchange.cancel.return_value = cancel_answer("success")
        assert not session.one("update_stop_loss", {"symbol": "BTC", "new_price": 81000})[0]
        names = [name for name, _, _ in session.trader.exchange.mock_calls if name in ("order", "cancel")]
        assert names == ["order", "cancel"]  # never without a stop in between

    def test_an_old_stop_that_could_not_be_canceled_is_named(self, session):
        self.setup(session)
        session.trader.exchange.cancel.return_value = cancel_answer({"error": "something else"})
        is_error, body = session.one("update_stop_loss", {"symbol": "BTC", "new_price": 81000})
        error = body["error"]
        assert is_error and error["code"] == "PROTECTION_REPLACEMENT_INCOMPLETE"
        assert (error["details"]["new_order"]["order_id"], error["details"]["old_order_ids"]) == (9, [6])


class TestCancelAllAndLeverage:
    def test_cancel_all_cancels_every_perp_order_and_leaves_spot_ones(self, session):
        session.registry.halt(session.target)  # allowed while halted
        session.trader.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 6}, {"coin": "ETH", "oid": 7}, {"coin": "@107", "oid": 8}]
        session.trader.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {
            "statuses": ["success", "success"]}}}
        is_error, body = session.one("cancel_all_orders", {})
        assert not is_error and body["data"] is None
        assert session.trader.exchange.bulk_cancel.call_args.args[0] == [
            {"coin": "BTC", "oid": 6}, {"coin": "ETH", "oid": 7}]

    def test_every_order_that_could_not_be_canceled_is_named_in_details(self, session):
        # The message is cut at 500 characters; details name each order still open.
        session.trader.info.open_orders.return_value = [{"coin": "BTC", "oid": 6}, {"coin": "ETH", "oid": 7},
                                                         {"coin": "BTC", "oid": 8}]
        long = "x" * 480
        session.trader.exchange.bulk_cancel.return_value = {"status": "ok", "response": {"type": "cancel", "data": {
            "statuses": [{"error": long}, "success", {"error": "also not"}]}}}
        is_error, body = session.one("cancel_all_orders", {})
        error = body["error"]
        assert is_error and error["code"] == "ORDERS_NOT_CANCELED" and len(error["message"]) == 500
        assert error["details"]["failed"] == [{"symbol": "BTC", "order_id": 6, "reason": long},
                                              {"symbol": "BTC", "order_id": 8, "reason": "also not"}]

    def test_cancel_all_of_a_spot_pair_is_refused(self, session):
        assert session.code("cancel_all_orders", {"symbol": "PURR/USDC"}) == "GUARD_BLOCKED"
        session.trader.exchange.bulk_cancel.assert_not_called()

    def test_leverage_is_set_and_echoed(self, session):
        session.trader.exchange.update_leverage.return_value = {"status": "ok", "response": {"type": "default"}}
        is_error, body = session.one("set_leverage", {"symbol": "BTC", "leverage": 5, "is_cross": False})
        assert not is_error and body["data"] == {"symbol": "BTC", "leverage": 5, "is_cross": False}
        assert session.trader.exchange.update_leverage.call_args.args == (5, "BTC", False)

    def test_leverage_above_the_accounts_cap_or_outside_the_allowlist_is_not_sent(self):
        session = Session(config(extra='symbols = ["BTC"]'))
        assert session.code("set_leverage", {"symbol": "BTC", "leverage": 6}) == "GUARD_BLOCKED"
        assert session.code("set_leverage", {"symbol": "ETH", "leverage": 2}) == "GUARD_BLOCKED"
        session.trader.exchange.update_leverage.assert_not_called()

    def test_leverage_defaults_to_cross_margin(self, session):
        session.trader.exchange.update_leverage.return_value = {"status": "ok", "response": {"type": "default"}}
        assert session.one("set_leverage", {"symbol": "BTC", "leverage": 3})[1]["data"]["is_cross"] is True
        assert session.trader.exchange.update_leverage.call_args.args == (3, "BTC", True)

    def test_leverage_of_an_unknown_market_is_unknown_symbol(self, session):
        # The SDK looked the symbol up while signing and failed with a bare KeyError (INTERNAL).
        assert session.code("set_leverage", {"symbol": "BTCC", "leverage": 2}) == "UNKNOWN_SYMBOL"
        session.trader.exchange.update_leverage.assert_not_called()

    def test_the_markets_own_limits_are_the_librarys(self, session):
        # HYPE: at most 3x, isolated only (see META).
        assert session.code("set_leverage", {"symbol": "HYPE", "leverage": 4, "is_cross": False}) == "INVALID_ARGUMENT"
        assert session.code("set_leverage", {"symbol": "HYPE", "leverage": 2}) == "INVALID_ARGUMENT"  # cross
        session.trader.exchange.update_leverage.assert_not_called()

    def test_a_refused_leverage_change_has_hyperliquids_reason(self, session):
        session.trader.exchange.update_leverage.return_value = {"status": "err", "response": "Cannot switch leverage type with open position."}
        is_error, body = session.one("set_leverage", {"symbol": "BTC", "leverage": 2, "is_cross": False})
        assert is_error and (body["error"]["code"], body["error"]["details"]["reason"]) == (
            "ACTION_REJECTED", "Cannot switch leverage type with open position.")

    def test_a_leverage_change_whose_outcome_is_unknown_says_to_check_first(self, session):
        session.trader.exchange.update_leverage.return_value = {"status": "something else"}
        error = session.one("set_leverage", {"symbol": "BTC", "leverage": 2})[1]["error"]
        assert (error["code"], error["retryable"], error["details"]["action"]) == (
            "OUTCOME_UNKNOWN", False, "BTC leverage update")
        assert error["hint"].startswith("do not repeat the call")

    def test_leverage_is_refused_while_halted(self, session):
        session.registry.halt(session.target)
        assert session.code("set_leverage", {"symbol": "BTC", "leverage": 2}) == "KILL_SWITCH_ENGAGED"
        session.trader.exchange.update_leverage.assert_not_called()


class TestKillSwitch:
    def reader(self, session):
        return session.registry.read_client(session.target)

    def test_it_halts_and_reports_what_is_still_live(self, session):
        reader = self.reader(session)
        reader.info.user_state.return_value = state(("BTC", "-0.002"))
        reader.info.frontend_open_orders.return_value = [
            {"coin": "BTC", "side": "B", "limitPx": "90000.0", "sz": "0.002", "oid": 6, "timestamp": 1_700_000_000_000,
             "origSz": "0.002", "triggerPx": "90000.0", "isTrigger": True, "orderType": "Stop Market",
             "reduceOnly": True, "tif": None, "cloid": None},
            {"coin": "@107", "side": "B", "limitPx": "1.0", "sz": "1", "oid": 7, "timestamp": 1_700_000_000_000,
             "origSz": "1", "orderType": "Limit", "reduceOnly": False, "tif": "Gtc", "cloid": None}]
        is_error, body = session.one("kill_switch", {})
        assert not is_error and body["halted"] == ["main"]
        account = body["accounts"][0]
        assert (account["halted"], account["positions"], account["open_orders"], account["error"]) == (
            True, [{"symbol": "BTC", "dex": None, "side": "short", "size": "0.002"}],
            {"count": 1, "stop_losses": 1, "take_profits": 0}, None)  # the spot order is not counted
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "KILL_SWITCH_ENGAGED"
        session.trader.info.user_state.return_value = state(("BTC", "-0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.002")
        assert not session.one("close", {"symbol": "BTC"})[0]  # risk-reducing calls still run

    def test_the_halt_stands_when_nothing_can_be_read(self):
        def factory(base_url, account, default_slippage, perp_dexs):
            if account is None:  # the read client: Hyperliquid cannot be reached
                raise NetworkException("connection refused")
            return market(base_url, account, default_slippage, perp_dexs)

        session = Session(make_client=factory)
        is_error, body = session.one("kill_switch", {})
        assert not is_error and body["accounts"][0]["error"]["code"] == "NETWORK_ERROR"
        assert body["accounts"][0]["positions"] is None and session.registry.is_halted(session.target)

    def test_all_accounts_halts_every_trading_account(self):
        conf = parse_config(f'''
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.copy]
address = "{OTHER}"
key_env = "HL_KEY_COPY"
trading = true
vault = true
max_order_usd = 100
max_leverage = 5
''', "test.toml")
        session = Session(conf, keys={"main": KEY, "copy": KEY})
        is_error, body = session.one("kill_switch", {})
        assert is_error and body["error"]["code"] == "AMBIGUOUS_ACCOUNT" and "all_accounts" in body["error"]["hint"]
        assert not any(session.registry.is_halted(t) for t in session.registry.trading_targets())
        assert session.code("kill_switch", {"account": "main", "all_accounts": True}) == "INVALID_ARGUMENT"
        is_error, body = session.one("kill_switch", {"all_accounts": True})
        assert not is_error and body["halted"] == ["main", "copy"]
        assert all(session.registry.is_halted(t) for t in session.registry.trading_targets())

    def test_it_does_not_wait_for_a_trade_call_that_is_running(self, session):
        tools = {spec.name: spec for spec in trade_tools(session.registry)}
        started, release = threading.Event(), threading.Event()

        def slow_order(**kwargs):
            started.set()
            release.wait(5)
            return FILLED

        session.trader.exchange.order.side_effect = slow_order
        buying = threading.Thread(target=tools["buy"].run, args=({"symbol": "BTC", "size": 0.001},))
        buying.start()
        assert started.wait(5)
        halted = tools["kill_switch"].run({})  # while the buy holds the account lock
        assert halted["halted"] == ["main"] and buying.is_alive()
        release.set()
        buying.join(5)

    def test_a_halt_never_waits_for_capacity_while_reports_hang(self):
        # Two kill switches whose reports hang on the network hold every emergency thread; a
        # third must still halt its account at once.
        from fractrade_hl_simple.mcp_server.server import OWN_THREADS
        conf = parse_config(f'''
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.b]
address = "{OTHER}"
key_env = "HL_KEY_B"
trading = true
vault = true
max_order_usd = 100
max_leverage = 5
''', "test.toml")
        session = Session(conf, keys={"main": KEY, "b": KEY})
        a, b = session.registry.resolve_trade("main"), session.registry.resolve_trade("b")
        reader = session.registry.read_client(a)
        release, reading = threading.Event(), []

        def hang(*args, **kwargs):
            reading.append(1)
            release.wait(10)
            return state()

        reader.info.user_state.side_effect = hang

        async def run():
            async with Client(session.server, mode="2026-07-28") as client:
                async with anyio.create_task_group() as group:
                    for _ in range(OWN_THREADS):
                        group.start_soon(client.call_tool, "kill_switch", {"account": "main"})
                    while len(reading) < OWN_THREADS:
                        await anyio.sleep(0.01)
                    group.start_soon(client.call_tool, "kill_switch", {"account": "b"})
                    with anyio.fail_after(5):
                        while not session.registry.is_halted(b):
                            await anyio.sleep(0.01)
                    halted_while_busy = len(reading) == OWN_THREADS
                    release.set()
                return halted_while_busy

        assert anyio.run(run) and session.registry.is_halted(a)

    def test_the_report_names_each_positions_dex_and_what_the_account_trades(self):
        # main trades only Hyperliquid's own perps; another account at the URL reads xyz.
        conf = parse_config(f'''
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.whale]
address = "{OTHER}"
perp_dexs = ["xyz"]
''', "test.toml")
        session = Session(conf)
        reader = session.registry.read_client(session.target)
        reader.info.user_state.side_effect = lambda address, dex="": state(("xyz:NVDA", "3")) if dex == "xyz" else state()
        reader.info.frontend_open_orders.return_value = []
        account = session.one("kill_switch", {})[1]["accounts"][0]
        assert (account["dexes_read"], account["dexes_traded"]) == (["default", "xyz"], ["default"])
        assert account["positions"] == [{"symbol": "xyz:NVDA", "dex": "xyz", "side": "long", "size": "3"}]

    def test_a_halt_never_waits_for_a_blocked_log(self):
        # A blocked stderr (the server's log handler) must not delay another account's halt.
        conf = parse_config(f'''
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.b]
address = "{OTHER}"
key_env = "HL_KEY_B"
trading = true
vault = true
max_order_usd = 100
max_leverage = 5
''', "test.toml")
        session = Session(conf, keys={"main": KEY, "b": KEY})
        b = session.registry.resolve_trade("b")
        release, logging_now = threading.Event(), threading.Event()

        class Blocked(logging.Handler):
            def emit(self, record):
                logging_now.set()
                release.wait(3)

        trade_logger = logging.getLogger("fractrade_hl_simple.mcp_server.tools.trade")
        handler, level = Blocked(), trade_logger.level
        trade_logger.addHandler(handler)
        trade_logger.setLevel(logging.INFO)

        async def run():
            async with Client(session.server, mode="2026-07-28") as client:
                async with anyio.create_task_group() as group:
                    group.start_soon(client.call_tool, "kill_switch", {"account": "main"})
                    while not logging_now.is_set():
                        await anyio.sleep(0.01)
                    started = time.monotonic()  # wall-clock: a blocked event loop also stops its own timers
                    group.start_soon(client.call_tool, "kill_switch", {"account": "b"})
                    while not session.registry.is_halted(b):
                        await anyio.sleep(0.01)
                    took = time.monotonic() - started
                    release.set()
                return took

        try:
            assert anyio.run(run) < 1.5
        finally:
            release.set()
            trade_logger.removeHandler(handler)
            trade_logger.setLevel(level)

    def test_the_dexes_read_are_those_of_the_client_that_read(self, session):
        # enable_perp_dexs can replace the read client between looking its dexes up and reading.
        reader = self.reader(session)
        reader.perp_dexs = ["", "xyz"]
        reader.info.user_state.return_value = state()
        reader.info.frontend_open_orders.return_value = []
        account = session.one("kill_switch", {})[1]["accounts"][0]
        assert account["dexes_read"] == ["default", "xyz"]


class TestRiskGuardrailsThroughTheTools:
    def session(self, extra):
        session = Session(config(extra=extra))
        session.trader.exchange.order.return_value = FILLED
        return session

    def posted(self, session):
        return [c.args[1]["type"] for c in session.trader.info.post.call_args_list if c.args[1]["type"] != "metaAndAssetCtxs"]

    def test_without_guardrails_an_order_reads_no_account_state(self, session):
        session.trader.info.post.reset_mock()
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert self.posted(session) == []

    def test_the_margin_guardrails_read_the_markets_leverage(self):
        from types import SimpleNamespace
        session = self.session("max_margin_pct = 50")
        session.trader.get_account_summary = lambda: SimpleNamespace(account_value=Decimal(1000), margin_used=Decimal(0))
        session.trader.info.post.reset_mock()
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert self.posted(session) == ["activeAssetData"]

    def test_a_reduce_only_order_reads_nothing(self):
        session = self.session("max_daily_loss_pct = 10\nmax_total_margin_pct = 50")
        session.trader.info.post.reset_mock()
        assert not session.one("sell", {"symbol": "BTC", "size": 0.001, "reduce_only": True})[0]
        assert self.posted(session) == []

    def test_the_total_margin_reads_the_account_and_blocks_what_would_exceed_it(self):
        from types import SimpleNamespace
        session = self.session("max_total_margin_pct = 10")
        session.trader.get_account_summary = lambda: SimpleNamespace(account_value=Decimal(1000), margin_used=Decimal(90))
        # At 5x: $50 of value is $10 of margin, 90 + 10 = 100 = 10 % of $1000; $55 is $11 and above.
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "limit_price": 50000})[0]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.0011, "limit_price": 50000})
        assert is_error and "margin in use with this order 101 USD is above 10 % of the account value (100 USD)" \
            in body["error"]["message"]

    def test_after_the_daily_loss_new_risk_is_refused_and_closing_still_works(self):
        session = self.session("max_daily_loss_pct = 10")
        losing = [["day", {"accountValueHistory": [[0, "1000"], [1, "880"]], "pnlHistory": [[0, "0"], [1, "-120"]]}]]
        # 880 + 120 = 1000 without the loss: 12 %
        session.trader.info.post.side_effect = lambda path, payload: losing if payload["type"] == "portfolio" \
            else ANSWERS[payload["type"]]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "loss over the last 24 hours 12 % reached 10 %" in body["error"]["message"]
        session.trader.info.user_state.return_value = state(("BTC", "0.002"))
        session.trader.exchange.order.return_value = filled(5, "0.002")
        assert not session.one("close", {"symbol": "BTC"})[0]

    def test_the_order_rate_counts_only_orders_that_add_risk(self):
        session = self.session("max_orders_per_hour = 2")
        for _ in range(2):
            assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert not session.one("sell", {"symbol": "BTC", "size": 0.001, "reduce_only": True})[0]  # not counted
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "reached max_orders_per_hour 2" in body["error"]["message"]

    def test_isolated_only_refuses_an_order_on_a_cross_market_and_says_how_to_fix_it(self):
        session = self.session("isolated_only = true")
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "set it with set_leverage and is_cross false" in body["error"]["message"]
        assert session.code("set_leverage", {"symbol": "BTC", "leverage": 3}) == "GUARD_BLOCKED"  # is_cross true
        session.trader.exchange.order.assert_not_called()

    def test_a_market_whose_leverage_was_raised_elsewhere_is_refused_with_check_order_leverage(self):
        session = self.session("check_order_leverage = true")
        session.trader.info.post.side_effect = lambda path, payload: {"leverage": {"type": "cross", "value": 25}} \
            if payload["type"] == "activeAssetData" else ANSWERS[payload["type"]]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "the market's leverage 25x is above max_leverage 5x" in body["error"]["message"]


class TestDayLoss:
    """The loss over the last 24 hours, as a share of the account value without it."""

    def day(self, values, pnl):
        return [["week", {}], ["day", {"accountValueHistory": [[i, v] for i, v in enumerate(values)],
                                       "pnlHistory": [[i, p] for i, p in enumerate(pnl)]}]]

    def test_a_deposit_is_not_a_gain_and_raises_the_base(self):
        # 1008 a day ago, 598 deposited, then 75 lost: 75 of 1606, not of 1008.
        from fractrade_hl_simple.mcp_server.tools.trade import _day_loss
        assert _day_loss(self.day(["1008", "1606", "1531"], ["0", "0", "-75"])) == (Decimal(75), Decimal(1606))

    def test_an_account_funded_today_is_covered(self):
        # 0 a day ago: the value 24 hours ago alone would make every loss 0 % (or a division by 0).
        from fractrade_hl_simple.mcp_server.tools.trade import _day_loss
        loss, capital = _day_loss(self.day(["0", "1000", "880"], ["0", "0", "-120"]))
        assert (loss, capital) == (Decimal(120), Decimal(1000))

    @pytest.mark.parametrize("portfolio", [None, [["day", None]], [["day", {"pnlHistory": [[0, "x"]],
                                                                                "accountValueHistory": [[0, "1"]]}]]])
    def test_an_answer_in_no_documented_format_is_not_established(self, portfolio):
        from fractrade_hl_simple.mcp_server.tools.trade import _day_loss
        assert _day_loss(portfolio) == (None, None)


class TestRiskReadsThatFail:
    """Whatever the guardrails' reads bring, an order that cannot be checked is not sent,
    and the error says so (GUARD_BLOCKED, the order was not sent)."""

    def session(self, extra):
        session = Session(config(extra=extra))
        session.trader.exchange.order.return_value = FILLED
        return session

    def summary(self, session, value, margin):
        from types import SimpleNamespace
        session.trader.get_account_summary = lambda: SimpleNamespace(account_value=value, margin_used=margin)

    @pytest.mark.parametrize("value, margin", [(Decimal("Infinity"), Decimal(0)), (Decimal("NaN"), Decimal(0)),
                                               (Decimal(1000), Decimal(-100_000)), (None, None)])
    def test_account_figures_that_are_no_finite_number_refuse(self, value, margin):
        session = self.session("max_total_margin_pct = 30")
        self.summary(session, value, margin)
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and body["error"]["code"] == "GUARD_BLOCKED"
        session.trader.exchange.order.assert_not_called()

    def test_a_failed_read_is_guard_blocked_retryable_and_the_cloid_stays_free(self):
        from fractrade_hl_simple.exceptions import NetworkException
        session = self.session("max_margin_pct = 5")
        real = session.trader.get_leverage
        session.trader.get_leverage = MagicMock(side_effect=NetworkException("connection reset"))
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})
        error = body["error"]
        assert is_error and (error["code"], error["retryable"]) == ("GUARD_BLOCKED", True)
        assert "could not read the market's leverage (NETWORK_ERROR" in error["message"]
        assert "the order was not sent" in error["message"]
        session.trader.exchange.order.assert_not_called()
        session.trader.get_leverage = real
        self.summary(session, Decimal(1000), Decimal(0))
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]  # same cloid: free

    def test_a_leverage_in_no_documented_format_is_guard_blocked_not_internal(self):
        session = self.session("check_order_leverage = true")
        session.trader.info.post.side_effect = lambda path, payload: {"leverage": {"type": "cross", "value": 0}} \
            if payload["type"] == "activeAssetData" else ANSWERS[payload["type"]]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and body["error"]["code"] == "GUARD_BLOCKED" and not body["error"]["retryable"]
        session.trader.exchange.order.assert_not_called()

    @pytest.mark.parametrize("day", [
        {"pnlHistory": [[0, "0"], [1, "-120"]]},                                       # no account values
        {"accountValueHistory": [], "pnlHistory": []},                                 # empty
        {"accountValueHistory": [[0, "1000"], [2, "880"]], "pnlHistory": [[0, "0"], [1, "-120"]]},  # other ends
    ])
    def test_a_day_history_that_does_not_establish_the_loss_refuses(self, day):
        session = self.session("max_daily_loss_pct = 10")
        session.trader.info.post.side_effect = lambda path, payload: [["day", day]] \
            if payload["type"] == "portfolio" else ANSWERS[payload["type"]]
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "max_daily_loss_pct cannot be checked" in body["error"]["message"]
        session.trader.exchange.order.assert_not_called()


class TestOrderRateCounting:
    def test_an_order_refused_for_its_cloid_is_not_counted(self):
        session = Session(config(extra="max_orders_per_hour = 2"))
        session.trader.exchange.order.return_value = FILLED
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID}) == "INVALID_ARGUMENT"
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]  # the second of two
        assert session.trader.exchange.order.call_count == 2

    def test_orders_older_than_an_hour_no_longer_count(self, monkeypatch):
        import fractrade_hl_simple.mcp_server.state as state_module
        now = [1_800_000_000.0]
        monkeypatch.setattr(state_module, "time", SimpleNamespace(time=lambda: now[0], sleep=time.sleep))
        session = Session(config(extra="max_orders_per_hour = 1"))
        session.trader.exchange.order.return_value = FILLED
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "GUARD_BLOCKED"
        now[0] += 3601
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]


class TestModifyUnderTheGuardrails:
    MODIFY = {"order_id": 4, "symbol": "BTC", "is_buy": True, "size": 0.002, "price": 50000,
              "order_type": {"limit": {"tif": "Gtc"}}}

    def session(self):
        from types import SimpleNamespace
        session = Session(config(extra="max_margin_pct = 1"))
        session.trader.get_account_summary = lambda: SimpleNamespace(account_value=Decimal(1000), margin_used=Decimal(0))
        session.trader.exchange.modify_order.return_value = RESTING
        return session

    def test_a_modify_that_adds_risk_is_checked_at_the_markets_leverage(self):
        # 0.002 at 50000 is 100 of value, at 5x 20 of margin: above 1 % of 1000.
        session = self.session()
        is_error, body = session.one("modify_order", self.MODIFY)
        assert is_error and "order margin 20 USD is above 1 % of the account value (10 USD)" in body["error"]["message"]
        session.trader.exchange.modify_order.assert_not_called()

    def test_a_reduce_only_modify_reads_nothing_and_is_not_limited(self):
        session = self.session()
        session.trader.info.post.reset_mock()
        assert not session.one("modify_order", {**self.MODIFY, "reduce_only": True})[0]
        assert [c.args[1]["type"] for c in session.trader.info.post.call_args_list] == []


class TestRiskReadsThatFailToParse:
    def test_a_mid_in_no_documented_format_is_guard_blocked_and_says_the_order_was_not_sent(self):
        session = Session(config())
        session.trader.info.all_mids.side_effect = lambda dex="": {"BTC": "not-a-number"}
        is_error, body = session.one("sell", {"symbol": "BTC", "size": 0.001, "limit_price": 80000})
        error = body["error"]
        assert is_error and (error["code"], error["retryable"]) == ("GUARD_BLOCKED", False)
        assert "could not read the market's mid price (an answer in no documented format" in error["message"]
        assert "the order was not sent" in error["message"]
        session.trader.exchange.order.assert_not_called()


class TestWhatTheOrderRateCounts:
    def session(self):
        return Session(config(extra="max_orders_per_hour = 1"))

    def test_an_order_whose_outcome_is_unknown_counts(self):
        session = self.session()
        session.trader.exchange.order.side_effect = [{"status": "ok", "response": "?"}]  # no documented status
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "OUTCOME_UNKNOWN"
        session.trader.exchange.order.side_effect = None
        session.trader.exchange.order.return_value = FILLED
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "GUARD_BLOCKED"  # it may have been sent

    def test_an_order_that_failed_before_sending_does_not_count(self):
        from fractrade_hl_simple.exceptions import InvalidArgumentException
        session = self.session()
        session.trader.exchange.order.side_effect = InvalidArgumentException("refused before sending")
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "INVALID_ARGUMENT"
        session.trader.exchange.order.side_effect = None
        session.trader.exchange.order.return_value = FILLED
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001})[0]

    def test_a_modify_that_adds_risk_counts(self):
        session = self.session()
        session.trader.exchange.modify_order.return_value = RESTING
        assert not session.one("modify_order", {"order_id": 4, "symbol": "BTC", "is_buy": True, "size": 0.001,
                                                "price": 80000, "order_type": {"limit": {"tif": "Gtc"}}})[0]
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "GUARD_BLOCKED"


from fractrade_hl_simple.mcp_server.state import SharedState  # noqa: E402


def shared_state():
    """A started shared state in a folder of its own, removed with it (the trade tools need one)."""
    import tempfile
    import weakref
    from pathlib import Path
    from fractrade_hl_simple.mcp_server.state import SharedState
    folder = tempfile.mkdtemp()
    state = SharedState(Path(folder) / "state")
    state.start()
    import shutil
    weakref.finalize(state, shutil.rmtree, folder, True)
    return state


class TestTwoServersOfOneConfig:
    """Two chats, two servers, one account: they share the halt, the order rate and the
    cloids (state.py). Each has its own SharedState on one folder, as two processes do."""

    def servers(self, extra=""):
        first = shared_state()
        second = SharedState(first.folder)
        second.start()
        one, two = Session(config(extra=extra), state=first), Session(config(extra=extra), state=second)
        for session in (one, two):
            session.trader.exchange.order.return_value = FILLED
        return one, two

    def test_the_kill_switch_in_one_chat_halts_the_other(self):
        one, two = self.servers()
        assert not one.one("kill_switch", {})[0]
        assert two.code("buy", {"symbol": "BTC", "size": 0.001}) == "KILL_SWITCH_ENGAGED"
        two.trader.exchange.order.assert_not_called()
        two.trader.info.user_state.return_value = state(("BTC", "0.002"))
        two.trader.exchange.order.return_value = filled(5, "0.002")
        assert not two.one("close", {"symbol": "BTC"})[0]  # reducing risk still works

    def test_the_order_rate_counts_the_orders_of_both(self):
        one, two = self.servers("max_orders_per_hour = 2")
        assert not one.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert not two.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert one.code("buy", {"symbol": "BTC", "size": 0.001}) == "GUARD_BLOCKED"

    def test_two_chats_ordering_at_once_the_second_sees_the_first_ones_margin(self):
        # max_total_margin_pct 3 of 1000: 30 of margin. Each order: 0.002 at 50000 (100, the
        # config's max_order_usd) at 5x, 20 of margin. Without the shared lock both would read
        # 0 in use and both pass.
        import threading
        one, two = self.servers("max_total_margin_pct = 3")
        margin, sending, finish = [Decimal(0)], threading.Event(), threading.Event()
        for session in (one, two):
            session.trader.get_account_summary = lambda: SimpleNamespace(account_value=Decimal(1000),
                                                                         margin_used=margin[0])

        def slow_fill(*args, **kwargs):  # chat one's order is on its way
            sending.set()
            finish.wait(5)
            margin[0] += 20
            return FILLED

        one.trader.exchange.order.side_effect = slow_fill
        order = {"symbol": "BTC", "size": 0.002, "limit_price": 50000}
        results = {}
        first = threading.Thread(target=lambda: results.update(one=one.one("buy", order)), daemon=True)
        first.start()
        assert sending.wait(5)
        second = threading.Thread(target=lambda: results.update(two=two.one("buy", order)), daemon=True)
        second.start()
        second.join(0.5)
        assert second.is_alive()  # waits for chat one's call to finish
        finish.set()
        first.join(5)
        second.join(5)
        assert not results["one"][0]
        is_error, body = results["two"]
        assert is_error and "margin in use with this order 40 USD is above 3 %" in body["error"]["message"]
        two.trader.exchange.order.assert_not_called()

    def test_an_order_is_counted_before_it_is_sent(self):
        # A crash after the exchange got it must not leave it uncounted for the other chats.
        # While the order is sent, chat one holds the lock: what is on disk is what the other
        # chats will read.
        one, two = self.servers("max_orders_per_hour = 5")
        folder, seen = one.registry._state.folder, []
        one.trader.exchange.order.side_effect = lambda *a, **k: seen.append(
            sum(len(path.read_text().splitlines()) for path in folder.glob("*.orders"))) or FILLED
        assert not one.one("buy", {"symbol": "BTC", "size": 0.001})[0]
        assert seen == [1] and two.registry.risk_orders_last_hour(two.target) == 1

    def test_a_cloid_used_in_one_chat_is_refused_in_the_other(self):
        one, two = self.servers()
        assert not one.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]
        assert two.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID}) == "INVALID_ARGUMENT"


class TestWhenTheSharedStateCannotBeWritten:
    """A full disk: an order's outcome is never replaced by the bookkeeping's error, and
    an order whose bookkeeping cannot be done before sending is not sent."""

    def full_disk(self, monkeypatch, session, method):
        def fails(*args, **kwargs):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(session.registry._state, method, fails)

    def test_an_unknown_outcome_stays_outcome_unknown(self, monkeypatch):
        session = Session()
        session.trader.exchange.order.return_value = {"status": "ok", "response": "?"}
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        self.full_disk(monkeypatch, session, "remember_cloids")
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})
        assert is_error and body["error"]["code"] == "OUTCOME_UNKNOWN" and body["error"]["details"]["cloid"] == CLOID

    def test_a_filled_order_is_reported_filled(self, monkeypatch):
        session = Session()
        session.trader.exchange.order.return_value = FILLED
        self.full_disk(monkeypatch, session, "remember_cloids")
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert not is_error and body["data"]["status"] == "filled"

    def test_an_unexpected_error_is_logged_once(self, caplog):
        # The tool's own log line built the whole error body, which logged the traceback too.
        session = Session()
        session.trader.exchange.order.side_effect = AttributeError("a bug")
        with caplog.at_level(logging.INFO):
            assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "INTERNAL"
        assert [r.getMessage() for r in caplog.records].count("unexpected error in a tool call") == 1
        assert any(r.getMessage().endswith(": INTERNAL") for r in caplog.records)

    def test_any_failure_of_the_bookkeeping_after_an_order_leaves_its_result(self, monkeypatch):
        # Not only a full disk: e.g. a damaged file's decoding error came back as INTERNAL,
        # and the agent never saw the filled order.
        session = Session()
        session.trader.exchange.order.return_value = FILLED

        def damaged(*args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        monkeypatch.setattr(session.registry._state, "remember_cloids", damaged)
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert not is_error and body["data"]["status"] == "filled"

    @pytest.mark.parametrize("method", ["reserve_cloid", "count_risk_order"])
    def test_bookkeeping_that_fails_before_sending_sends_nothing(self, monkeypatch, method):
        session = Session(config(extra="max_orders_per_hour = 5"))
        self.full_disk(monkeypatch, session, method)
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and body["error"]["code"] == "GUARD_BLOCKED"
        assert "the shared state cannot be written" in body["error"]["message"]
        assert "the order was not sent" in body["error"]["message"]
        session.trader.exchange.order.assert_not_called()

    def test_a_halt_that_cannot_be_shared_still_halts_this_chat_and_says_so(self, monkeypatch):
        session = Session()
        self.full_disk(monkeypatch, session, "halt")
        is_error, body = session.one("kill_switch", {})
        assert is_error and "halted in this server only" in body["error"]["message"]
        assert session.code("buy", {"symbol": "BTC", "size": 0.001}) == "KILL_SWITCH_ENGAGED"


class TestWhenTheSharedStateCannotBeWrittenAgain:
    def test_a_cloid_refused_by_the_bookkeeping_before_sending_stays_free(self, monkeypatch):
        session = Session(config(extra="max_orders_per_hour = 5"))
        session.trader.exchange.order.return_value = FILLED
        real = session.registry._state.count_risk_order
        monkeypatch.setattr(session.registry._state, "count_risk_order",
                            lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device")))
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID}) == "GUARD_BLOCKED"
        monkeypatch.setattr(session.registry._state, "count_risk_order", real)  # the disk has room again
        assert not session.one("buy", {"symbol": "BTC", "size": 0.001, "cloid": CLOID})[0]

    def test_reading_the_order_rate_that_fails_sends_nothing_and_says_so(self, monkeypatch):
        session = Session(config(extra="max_orders_per_hour = 5"))
        monkeypatch.setattr(session.registry._state, "risk_orders_last_hour",
                            lambda *a: (_ for _ in ()).throw(OSError(5, "Input/output error")))
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "the order was not sent" in body["error"]["message"]
        session.trader.exchange.order.assert_not_called()

    def test_a_failed_shared_halt_of_one_account_still_halts_the_others(self, monkeypatch):
        conf = parse_config(f'''
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.copy]
address = "{OTHER}"
key_env = "HL_KEY_COPY"
trading = true
max_order_usd = 100
max_leverage = 5
''', "test.toml")
        session = Session(conf, keys={"main": KEY, "copy": "0x" + "ab" * 32})
        state = session.registry._state
        real = state.halt
        monkeypatch.setattr(state, "halt", lambda key: (_ for _ in ()).throw(OSError(28, "No space left"))
                            if key[0] == OWN.lower() else real(key))
        is_error, body = session.one("kill_switch", {"all_accounts": True})
        assert is_error and "halted in this server only: main" in body["error"]["message"]
        main, copy = session.registry.resolve_trade("main"), session.registry.resolve_trade("copy")
        assert session.registry.is_halted(main) and session.registry.is_halted(copy)
        assert state.is_halted(copy.key) and not state.is_halted(main.key)  # copy's is shared


class TestAReadOnlyServer:
    def test_it_reports_the_halt_of_the_trading_servers_and_creates_nothing(self, tmp_path):
        trading = SharedState(tmp_path / "state")
        trading.start()
        one = Session(state=trading)
        assert not one.one("kill_switch", {})[0]
        reader = AccountRegistry(config(), {}, market, SharedState(tmp_path / "state"))  # not started
        assert reader.is_halted(reader.resolve_read(account="main"))
        assert not SharedState(tmp_path / "nothing").is_halted(("0x" + "1" * 40, "https://api.hyperliquid.xyz"))
        assert not (tmp_path / "nothing").exists()


class TestOpenPosition:
    """open_position: a market entry with its stop losses and take profits, through the library."""

    ARGS = {"symbol": "BTC", "side": "long", "size": 0.001,
            "stop_losses": [{"price": 80000, "fraction": 0.5}, {"price": 79000, "fraction": 0.5}],
            "take_profits": [{"price": 90000, "fraction": 1}]}

    def session(self, extra="max_order_usd = 1000", fills="0.001", legs=None):
        session = Session(parse_config(f"""
[accounts.main]
address = "{OWN}"
key_env = "HL_KEY_MAIN"
trading = true
max_leverage = 5
{extra}
""", "test.toml"))
        session.trader.info.user_state.return_value = state(("BTC", fills))
        session.trader.exchange.order.side_effect = [filled(3, fills), *(legs or [resting(4), resting(5), resting(6)])]
        return session

    def orders(self, session):
        return [c.kwargs for c in session.trader.exchange.order.call_args_list]

    def test_the_cloids_of_the_entry_and_its_levels_are_saved_in_one_write(self, monkeypatch):
        # Each one rewrote the whole 30-day file, holding the account's lock.
        session = self.session()
        state, saves = session.registry._state, []
        save = state._save_cloids
        monkeypatch.setattr(state, "_save_cloids", lambda key, used: saves.append(len(used)) or save(key, used))
        assert not session.one("open_position", self.ARGS)[0]
        assert saves == [4]  # the entry and its three levels

    def test_the_entry_as_checked_then_each_level_for_its_share_of_the_fill(self):
        session = self.session()
        is_error, body = session.one("open_position", self.ARGS)
        entry, *legs = self.orders(session)
        assert not is_error and body["data"]["entry"]["status"] == "filled"
        # Immediate or cancel at the mid (85000) plus the 2 % default slippage: what the guards saw.
        assert entry["is_buy"] and entry["order_type"] == {"limit": {"tif": "Ioc"}} and entry["limit_px"] == 86700.0
        assert [(leg["sz"], leg["order_type"]["trigger"]["triggerPx"], leg["is_buy"]) for leg in legs] == [
            (0.0005, 80000.0, False), (0.0005, 79000.0, False), (0.001, 90000.0, False)]
        assert (len(body["data"]["stop_losses"]), len(body["data"]["take_profits"])) == (2, 1)

    def test_after_a_partial_fill_the_levels_cover_the_filled_size(self):
        session = self.session(fills="0.0003")
        assert not session.one("open_position", self.ARGS)[0]
        assert [leg["sz"] for leg in self.orders(session)[1:]] == [0.00015, 0.00015, 0.0003]

    def test_stop_losses_must_cover_the_whole_position(self):
        session = self.session()
        is_error, body = session.one("open_position", {**self.ARGS, "stop_losses": [{"price": 80000, "fraction": 0.5}]})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT" and "add up to 1" in body["error"]["message"]
        session.trader.exchange.order.assert_not_called()

    def test_the_entry_is_checked_against_the_limits(self):
        session = self.session("max_order_usd = 50")
        assert session.code("open_position", self.ARGS) == "GUARD_BLOCKED"
        session.trader.exchange.order.assert_not_called()

    def test_a_halted_account_opens_nothing(self):
        session = self.session()
        assert not session.one("kill_switch", {})[0]
        assert session.code("open_position", self.ARGS) == "KILL_SWITCH_ENGAGED"
        session.trader.exchange.order.assert_not_called()

    def test_a_partial_level_below_the_minimum_is_refused_before_the_entry(self):
        session = self.session("max_order_usd = 1000\nmax_orders_per_hour = 1")
        too_small = {**self.ARGS, "size": 0.0002,
                     "stop_losses": [{"price": 80000, "fraction": 0.5}, {"price": 79000, "fraction": 0.5}]}
        is_error, body = session.one("open_position", too_small)  # 0.0001 at 80000: 8 USD
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT" and "minimum order" in body["error"]["message"]
        session.trader.exchange.order.assert_not_called()
        assert not session.one("open_position", self.ARGS)[0]  # the refused one did not count for the rate

    def test_a_stop_above_the_market_is_refused_although_below_the_entrys_slippage_limit(self):
        # Mid 85000, the entry's IOC limit 86700: a long's stop at 86000 would fire at once.
        session = self.session()
        is_error, body = session.one("open_position", {**self.ARGS, "stop_losses": [{"price": 86000, "fraction": 1}]})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"
        session.trader.exchange.order.assert_not_called()

    def test_a_level_too_small_for_a_tiny_fill_is_a_warning(self):
        session = self.session(fills="0.00001")
        is_error, body = session.one("open_position", {**self.ARGS, "stop_losses": [{"price": 80000, "fraction": 1}],
                                                       "take_profits": [{"price": 90000, "fraction": 0.5}]})
        assert not is_error and body["data"]["take_profits"] == []
        assert any("take_profit_1 is not placed" in w for w in body["data"]["warnings"])

    def test_a_level_whose_outcome_is_unknown_keeps_its_cloid_used(self):
        # Its cloid is in details.missing: placing an order under it again is refused.
        from hyperliquid.utils.error import ServerError
        session = self.session(legs=[ServerError(502, "Bad Gateway")])  # sent, its answer lost
        session.trader.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            is_error, body = session.one("open_position", self.ARGS)
        unknown = session.trader.exchange.order.call_args.kwargs["cloid"].to_raw()
        missing = body["error"]["details"]["missing"][0]
        assert is_error and (missing["outcome_unknown"], missing["cloid"]) == (True, unknown)
        assert "get_order_status_by_cloid" in body["error"]["hint"]
        session.trader.exchange.order.side_effect = None
        session.trader.exchange.order.return_value = FILLED
        assert session.code("buy", {"symbol": "BTC", "size": 0.001, "cloid": unknown}) == "INVALID_ARGUMENT"

    def test_a_level_that_fails_after_the_entry_names_every_missing_one(self):
        rejected = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"error": "Order must have minimum value of $10."}]}}}
        session = self.session(legs=[resting(4), rejected])
        is_error, body = session.one("open_position", self.ARGS)
        error = body["error"]
        assert is_error and error["code"] == "PROTECTION_NOT_PLACED" and error["details"]["leg"] == "stop_loss_2"
        assert [(m["leg"], m["size"]) for m in error["details"]["missing"]] == [
            ("stop_loss_2", "0.0005"), ("take_profit_1", "0.001")]
        assert "place them with stop_loss / take_profit, or close" in error["hint"]
        assert len(self.orders(session)) == 3  # the entry, the first stop, the rejected second: no close


class TestMaxRiskPctThroughTheTools:
    """max_risk_pct: an open_position may lose at most this share of the account at its stops."""

    def session(self, max_risk_pct):
        session = TestOpenPosition().session(f"max_order_usd = 1000\nmax_risk_pct = {max_risk_pct}")
        session.trader.get_account_summary = lambda: SimpleNamespace(account_value=Decimal(1000), margin_used=Decimal(0))
        session.trader.get_fee_rates = lambda symbol, address=None: SimpleNamespace(taker=Decimal("0.00045"),
                                                                                    maker=Decimal("0.00015"))
        session.trader.get_order_book = lambda symbol, decimal=False: {
            "bids": [{"price": "84990", "size": "10", "num_orders": 3}],
            "asks": [{"price": "85010", "size": "10", "num_orders": 3}]}
        return session

    def test_a_trade_within_its_risk_opens(self):
        # 0.001 BTC from about 85010 to stops at 80000 and 79000: about 5.5 USD, within 1 % of 1000.
        session = self.session(1)
        assert not session.one("open_position", TestOpenPosition.ARGS)[0]

    def test_a_trade_above_its_risk_is_refused_before_anything_is_sent(self):
        session = self.session("0.5")
        is_error, body = session.one("open_position", TestOpenPosition.ARGS)
        assert is_error and body["error"]["code"] == "GUARD_BLOCKED" and "above 0.5 %" in body["error"]["message"]
        session.trader.exchange.order.assert_not_called()

    def test_buy_and_sell_that_add_risk_are_refused_and_reducing_works(self):
        session = self.session(1)
        is_error, body = session.one("buy", {"symbol": "BTC", "size": 0.001})
        assert is_error and "open the trade with open_position" in body["error"]["message"]
        session.trader.exchange.order.side_effect = None
        session.trader.exchange.order.return_value = filled(5, "0.001")
        assert not session.one("sell", {"symbol": "BTC", "size": 0.001, "reduce_only": True})[0]
