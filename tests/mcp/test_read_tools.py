"""The read tools about one wallet: which wallet they read, their output
shapes and their arguments."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402
from mcp import Client  # noqa: E402

from fractrade_hl_simple import _answers  # noqa: E402
from fractrade_hl_simple.exceptions import HyperliquidException, NetworkException  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.server import build_server  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import RECENT_FILLS, read_tools  # noqa: E402
from fractrade_hl_simple.models import (  # noqa: E402
    AccountSummary, CollateralBalance, CumFunding, DexAccount, Fill, FillHistory, FundingPayment, Leverage, Order,
    OrderType, PnLReport, Position, coin_market,
)

MAIN = "0x" + "1" * 40
COPY = "0x" + "2" * 40
OTHER = "0x" + "4" * 40
MAINNET, TESTNET = constants.MAINNET_API_URL, constants.TESTNET_API_URL
CONFIG = f'''
default_account = "main"
[accounts.main]
address = "{MAIN}"
[accounts.copy]
address = "{COPY}"
'''
T0 = 1794000000000  # 2026-11-06T21:20:00Z


def position(symbol, size, **fields):
    values = dict(symbol=symbol, entry_price=Decimal("100"), leverage=Leverage(type="cross", value=Decimal("5")),
                  liquidation_price=Decimal("80"), margin_used=Decimal("20"), position_value=abs(Decimal(size)) * 100,
                  return_on_equity=Decimal("0.1"), size=Decimal(size), unrealized_pnl=Decimal("2"),
                  cum_funding=CumFunding(all_time=Decimal("1"), since_open=Decimal("0.5"), since_change=Decimal("0.5")))
    return Position(**{**values, **fields})


def order(order_id, symbol="BTC", filled="0", **fields):
    return Order(order_id=order_id, symbol=symbol, is_buy=True, size=Decimal("0.01"), order_type=OrderType(limit={}),
                 reduce_only=False, status="open", time_in_force="Gtc", created_at=T0,
                 filled_size=Decimal(filled), limit_price=Decimal("80000"), type="limit", **fields)


def fill(time, symbol="BTC", side="B", crossed=True):
    return Fill(symbol=symbol, side=side, price=Decimal("85000"), size=Decimal("0.001"), closed_pnl=Decimal("0"),
                direction="Open Long", order_id=7, crossed=crossed, time=time, hash="0xabc", fee=Decimal("0.03"),
                fee_token="USDC", trade_id=time)


def history(fills, reason=None):
    return FillHistory(fills=fills, truncated=reason is not None, truncated_reason=reason)


class FakeClient:
    """A library client that records each call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        # What the client knows without a request: the perp markets of the dexes it reads.
        self.perp_dexs = ["", "xyz"]
        self.market_specs = {name: {} for name in ("BTC", "ETH", "xyz:NVDA", "xyz:TSLA")}
        self.market_specs.update(answers.get("market_specs", {}))  # e.g. a dex's collateral
        self.listed_since = answers.get("listed_since", [])  # markets get_markets() finds too

    def get_markets(self):
        self.calls.append(("get_markets", (), {}))
        if isinstance(self.answers.get("get_markets"), Exception):
            raise self.answers["get_markets"]
        return [SimpleNamespace(symbol=name) for name in [*self.market_specs, *self.listed_since]]  # the symbol check reads only that

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            answer = self.answers[method]
            if isinstance(answer, Exception):
                raise answer
            return answer
        return call


def run_tool(name, arguments, answers):
    clients = {}

    def make_client(base_url, account, default_slippage, perp_dexs):
        assert account is None  # reads never use a client with a key
        return clients.setdefault(base_url, FakeClient(answers))

    conf = parse_config(CONFIG, "test.toml")
    server = build_server(read_tools(conf, AccountRegistry(conf, {}, make_client)))

    async def call():
        async with Client(server, mode="2026-07-28") as client:
            result = await client.call_tool(name, arguments)
            return result.is_error, json.loads(result.content[0].text)

    is_error, body = anyio.run(call)
    return is_error, body, clients


# Every tool about one wallet, with arguments and the library answer it needs.
def summary(mode="unified"):
    return AccountSummary(
        mode=mode, account_value=Decimal("1080.0"), available_to_trade=Decimal("600.0"), margin_used=Decimal("400.0"),
        balances=[CollateralBalance(token="USDC", market="spot", total=Decimal("1000.0"), hold=Decimal("400.0"),
                                    available=Decimal("600.0"), price=Decimal("1"), usd_value=Decimal("1000.0")),
                  CollateralBalance(token="+12", market="outcome", total=Decimal("3"), hold=Decimal("0"),
                                    available=Decimal("3"), price=None, usd_value=None)],
        dexes=[DexAccount(dex=None, account_value=None, withdrawable=None, margin_used=Decimal("0"), positions=[]),
               DexAccount(dex="xyz", account_value=None, withdrawable=None, margin_used=Decimal("400.0"),
                          positions=[position("xyz:NVDA", "-2"), position("xyz:TSLA", "0")])],
        sources={"account_value": "spot balances at USD prices"})


WALLET_TOOLS = [
    ("get_account", {}, {"get_account_summary": summary(), "get_open_orders": []}),
    ("get_positions", {}, {"get_positions": []}),
    ("get_open_orders", {}, {"get_open_orders": []}),
    ("get_open_orders", {"market": "spot"}, {"get_spot_open_orders": []}),
    ("get_fills", {}, {"get_fills": []}),
    ("get_fills", {"start": "2026-11-01T00:00:00Z"}, {"get_fill_history": history([])}),
    ("get_changes", {}, {"get_open_orders": [], "get_fill_history": history([]), "get_positions": [],
                         "get_historical_orders": []}),
    ("get_order_status", {"order_id": 1}, {"get_order_status": {"status": "unknownOid"}}),
    ("get_portfolio", {}, {"get_portfolio": []}),
    ("get_funding_payments", {}, {"get_user_funding": []}),
    ("get_pnl", {"start": "2026-11-01T00:00:00Z", "end": "2026-11-10T00:00:00Z"},
     {"get_pnl": PnLReport(start_time=T0, end_time=T0, trades=[], realized_pnl=Decimal(0), fees=Decimal(0),
                           fees_other={}, funding=Decimal(0), net_pnl=Decimal(0), unrealized_pnl=None,
                           truncated=False)}),
]
WALLETS = [  # (arguments, account in the envelope, address read, base_url read)
    ({}, "main", MAIN, MAINNET),
    ({"account": "copy"}, "copy", COPY, MAINNET),
    ({"address": OTHER, "base_url": TESTNET}, None, OTHER, TESTNET),
]


class TestWhichWallet:
    @pytest.mark.parametrize("wallet", WALLETS, ids=["default", "named", "address"])
    @pytest.mark.parametrize("tool, arguments, answers", WALLET_TOOLS, ids=lambda v: v if isinstance(v, str) else None)
    def test_every_tool_reads_exactly_the_wallet_asked_for(self, tool, arguments, answers, wallet):
        wallet_arguments, account, address, base_url = wallet
        is_error, body, clients = run_tool(tool, {**arguments, **wallet_arguments}, answers)
        assert not is_error, body
        assert (body["account"], body["address"], body["base_url"]) == (account, address, base_url)
        assert list(clients) == [base_url]
        calls = clients[base_url].calls
        assert calls and all(kwargs.get("address") == address for _, _, kwargs in calls)

    @pytest.mark.parametrize("arguments, code", [
        ({"account": "mian"}, "ACCOUNT_NOT_FOUND"),
        ({"account": "main", "address": OTHER}, "INVALID_ARGUMENT"),
        ({"address": "0x1234"}, "INVALID_ARGUMENT"),                                     # the schema's pattern
        ({"address": OTHER, "base_url": MAINNET + "/"}, "INVALID_ARGUMENT"),             # not in the enum
        ({"account": "main", "base_url": TESTNET}, "INVALID_ARGUMENT"),                  # base_url only with address
    ])
    def test_wrong_wallet_arguments(self, arguments, code):
        is_error, body, _ = run_tool("get_positions", arguments, {"get_positions": []})
        assert is_error and body["error"]["code"] == code

    def test_a_library_failure_comes_with_its_code(self):
        is_error, body, _ = run_tool("get_positions", {}, {"get_positions": NetworkException("unreachable")})
        assert is_error and (body["error"]["code"], body["error"]["retryable"]) == ("NETWORK_ERROR", True)


class TestAccount:
    def test_the_summary_with_positions_and_open_orders(self):
        answers = {"get_account_summary": summary(),
                   "get_open_orders": [order(1), order(2, symbol="@107"), order(3, symbol="xyz:NVDA")]}
        is_error, body, _ = run_tool("get_account", {}, answers)
        data = body["data"]
        assert not is_error and (data["mode"], data["account_value"], data["available_to_trade"], data["margin_used"]) == (
            "unified", "1080.0", "600.0", "400.0")
        assert data["balances"][1] == {"token": "+12", "market": "outcome", "total": "3", "hold": "0", "available": "3",
                                       "price": None, "usd_value": None}
        assert data["dexes"] == [
            {"dex": None, "account_value": None, "withdrawable": None, "margin_used": "0",
             "cross_maintenance_margin_used": None, "positions": 0},
            {"dex": "xyz", "account_value": None, "withdrawable": None, "margin_used": "400.0",
             "cross_maintenance_margin_used": None, "positions": 1}]
        assert [p["symbol"] for p in data["positions"]] == ["xyz:NVDA"]
        assert data["open_orders"] == 2  # perp orders only
        assert data["sources"] == {"account_value": "spot balances at USD prices"}


class TestPositions:
    def test_open_positions_as_documented(self):
        answers = {"get_positions": [position("BTC", "0.5"), position("xyz:NVDA", "-2"), position("ETH", "0")]}
        is_error, body, _ = run_tool("get_positions", {}, answers)
        assert not is_error and (body["count"], body["truncated"]) == (2, False)
        assert body["data"][0] == {
            "symbol": "BTC", "dex": None, "side": "long", "size": "0.5", "entry_price": "100", "notional": "50.0",
            "unrealized_pnl": "2", "return_on_equity": "0.1", "leverage": "5", "margin_mode": "cross",
            "liquidation_price": "80", "margin_used": "20", "funding_since_open": "0.5"}
        assert (body["data"][1]["symbol"], body["data"][1]["dex"], body["data"][1]["side"], body["data"][1]["size"]) == (
            "xyz:NVDA", "xyz", "short", "2")

    def test_fields_hyperliquid_may_leave_out(self):
        answers = {"get_positions": [position("BTC", "1", entry_price=None, liquidation_price=None, cum_funding=None)]}
        _, body, _ = run_tool("get_positions", {}, answers)
        assert {k: body["data"][0][k] for k in ("entry_price", "liquidation_price", "funding_since_open")} == {
            "entry_price": None, "liquidation_price": None, "funding_since_open": None}

    def test_one_symbol(self):
        answers = {"get_positions": [position("BTC", "0.5"), position("xyz:NVDA", "-2")]}
        _, body, _ = run_tool("get_positions", {"symbol": "xyz:NVDA"}, answers)
        assert [p["symbol"] for p in body["data"]] == ["xyz:NVDA"]


class TestSymbolFilter:
    """A wallet tool's symbol is checked against the markets: "NVDA" for xyz:NVDA must not read
    as "no position"."""

    @pytest.mark.parametrize("tool, answers", [
        ("get_positions", {"get_positions": [position("xyz:NVDA", "-2")]}),
        ("get_open_orders", {"get_open_orders": []}),  # the library filters by symbol: nothing for NVDA
        ("get_fills", {"get_fills": [fill(T0, "xyz:NVDA")]}),
        ("get_funding_payments", {"get_user_funding": []}),
    ])
    def test_a_symbol_without_its_dex_gets_the_market_suggested(self, tool, answers):
        is_error, body, _ = run_tool(tool, {"symbol": "NVDA"}, answers)
        assert is_error and body["error"]["code"] == "UNKNOWN_SYMBOL"
        assert "did you mean 'xyz:NVDA'?" in body["error"]["message"]

    @pytest.mark.parametrize("tool, answers", [
        ("get_fills", {"get_fills": []}), ("get_funding_payments", {"get_user_funding": []})])
    def test_a_dex_prefix_in_capitals_is_not_taken_for_a_dex_that_is_not_read(self, tool, answers):
        is_error, body, _ = run_tool(tool, {"symbol": "XYZ:NVDA"}, answers)
        assert is_error and "did you mean 'xyz:NVDA'?" in body["error"]["message"]

    def test_a_known_market_without_a_position_is_an_empty_list_without_a_request(self):
        is_error, body, clients = run_tool("get_positions", {"symbol": "ETH"}, {"get_positions": []})
        assert not is_error and body["data"] == []
        assert [call[0] for call in clients[MAINNET].calls] == ["get_positions"]

    def test_a_market_listed_after_the_client_was_made_is_found_by_one_refresh(self):
        answers = {"get_positions": [], "listed_since": ["NEWCOIN"]}
        is_error, body, clients = run_tool("get_positions", {"symbol": "NEWCOIN"}, answers)
        assert not is_error and body["data"] == []
        assert [call[0] for call in clients[MAINNET].calls] == ["get_positions", "get_markets"]

    def test_when_the_markets_cannot_be_read_the_error_is_the_network_one_not_unknown_symbol(self):
        answers = {"get_positions": [], "get_markets": NetworkException("connection refused")}
        is_error, body, _ = run_tool("get_positions", {"symbol": "NEWCOIN"}, answers)
        assert is_error and (body["error"]["code"], body["error"]["retryable"]) == ("NETWORK_ERROR", True)

    def test_a_symbol_with_records_is_never_refused(self):
        # e.g. a market no longer in the metadata: its fills are history, not a typo.
        _, body, _ = run_tool("get_fills", {"symbol": "OLDCOIN"}, {"get_fills": [fill(T0, "OLDCOIN")]})
        assert [f["symbol"] for f in body["data"]] == ["OLDCOIN"]

    def test_positions_on_a_dex_that_is_not_read_are_unknown_not_empty(self):
        is_error, body, _ = run_tool("get_positions", {"symbol": "flx:GOLD"}, {"get_positions": []})
        assert is_error and body["error"]["code"] == "UNKNOWN_SYMBOL"
        assert "the flx dex is not enabled (read: default, xyz); enable_perp_dexs adds it" in body["error"]["message"]

    def test_fills_and_funding_cover_every_dex_so_other_dexes_pass(self):
        _, body, _ = run_tool("get_fills", {"symbol": "flx:GOLD"}, {"get_fills": [fill(T0, "flx:GOLD")]})
        assert [f["symbol"] for f in body["data"]] == ["flx:GOLD"]
        is_error, _, _ = run_tool("get_funding_payments", {"symbol": "flx:GOLD"}, {"get_user_funding": []})
        assert not is_error


class TestOpenOrders:
    def test_perp_orders_by_default(self):
        answers = {"get_open_orders": [order(11, filled="0.004", trigger_price=Decimal("79000"))]}
        is_error, body, clients = run_tool("get_open_orders", {"symbol": "BTC"}, answers)
        assert not is_error and body["data"] == [{
            "order_id": 11, "cloid": None, "symbol": "BTC", "side": "buy", "type": "limit", "order_type": "Limit",
            "is_market": False, "size": "0.01", "filled_size": "0.004", "reduced_size": None,
            "remaining_size": "0.006", "limit_price": "80000", "trigger_price": "79000", "slippage_cap_pct": None,
            "trigger_condition": None, "reduce_only": False, "is_position_tpsl": None, "time_in_force": "Gtc",
            "created_at": "2026-11-06T21:20:00.000Z", "children": []}]
        assert clients[MAINNET].calls == [("get_open_orders", ("BTC",), {"address": MAIN})]

    def test_perp_orders_leave_out_the_spot_and_outcome_orders_of_the_default_dex(self):
        answers = {"get_open_orders": [order(1, symbol="BTC"), order(2, symbol="@107"),
                                       order(3, symbol="PURR/USDC"), order(4, symbol="xyz:NVDA"),
                                       order(5, symbol="#12")]}
        _, body, _ = run_tool("get_open_orders", {}, answers)
        assert [o["symbol"] for o in body["data"]] == ["BTC", "xyz:NVDA"]

    def test_spot_orders(self):
        _, body, clients = run_tool("get_open_orders", {"market": "spot", "symbol": "HYPE"},
                                    {"get_spot_open_orders": [order(9, symbol="HYPE")]})
        assert [o["symbol"] for o in body["data"]] == ["HYPE"]
        assert clients[MAINNET].calls == [("get_spot_open_orders", ("HYPE",), {"address": MAIN})]

    def test_an_unknown_market_is_refused(self):
        is_error, body, _ = run_tool("get_open_orders", {"market": "futures"}, {})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"

    # Seen live: a stop showed limit_price = trigger_price, so whether it was a market stop had
    # to be checked on Hyperliquid.
    @pytest.mark.parametrize("name, side, limit, kind, is_market, cap", [
        ("Limit", "B", "2900.0", "Limit", False, None),
        ("Stop Market", "A", "3000.0", "Stop Market", True, 10.0),  # limitPx as sent: not its bound
        ("Stop Limit", "A", "2940.0", "Stop Limit", False, 2.0),     # a sell may fill 2 % below the trigger
        ("Take Profit Limit", "A", "3030.0", "Take Profit Limit", False, 0.0),  # a sell's limit above: no slippage
    ])
    def test_each_order_type_says_how_it_executes(self, name, side, limit, kind, is_market, cap):
        trigger = name != "Limit"
        raw = {"coin": "ETH", "side": side, "limitPx": limit, "sz": "0.5", "oid": 5, "timestamp": T0,
               "triggerCondition": "Price below 3000" if trigger else "N/A", "isTrigger": trigger,
               "triggerPx": "3000.0" if trigger else "0.0", "reduceOnly": trigger, "orderType": name,
               "origSz": "0.5", "tif": None if trigger else "Gtc", "cloid": None}
        _, body, _ = run_tool("get_open_orders", {}, {
            "get_open_orders": [_answers.parse_open_order(raw, raw["coin"])]})
        listed = body["data"][0]
        assert (listed["order_type"], listed["is_market"], listed["slippage_cap_pct"]) == (kind, is_market, cap)

    def test_a_reduce_only_order_shrunk_or_filled_shows_no_fill(self):
        # Seen live: a take profit limit Hyperliquid shrank to the position after a partial close by
        # hand was shown with filled_size 46296.2, although nothing of it had filled.
        raw = {"coin": "USUAL", "side": "A", "limitPx": "0.0106", "sz": "53703.8", "oid": 5, "timestamp": T0,
               "triggerCondition": "N/A", "isTrigger": False, "triggerPx": "0.0", "reduceOnly": True,
               "orderType": "Limit", "origSz": "100000.0", "tif": "Gtc", "cloid": None}
        _, listed, _ = run_tool("get_open_orders", {}, {
            "get_open_orders": [_answers.parse_open_order(raw, raw["coin"])]})
        _, by_id, _ = run_tool("get_order_status", {"order_id": 5}, {"get_order_status": status(raw, state="open")})
        for shown in (listed["data"][0], by_id["data"]["order"]):
            assert (shown["size"], shown["filled_size"], shown["reduced_size"], shown["remaining_size"]) == (
                "100000.0", None, "46296.2", "53703.8")


class TestFills:
    def test_the_most_recent_fills_newest_first_up_to_the_limit(self):
        answers = {"get_fills": [fill(T0 + i, side="B" if i % 2 else "A", crossed=bool(i % 2)) for i in range(5)]}
        _, body, _ = run_tool("get_fills", {"limit": 2}, answers)
        assert (body["count"], body["truncated"], body["truncated_reason"]) == (2, True, "limit")
        assert body["data"][0] == {
            "time": "2026-11-06T21:20:00.004Z", "symbol": "BTC", "side": "sell", "direction": "Open Long",
            "price": "85000", "size": "0.001", "closed_pnl": "0", "fee": "0.03", "fee_token": "USDC",
            "taker": False, "order_id": 7, "trade_id": T0 + 4, "start_position": None}  # a maker fill
        assert (body["data"][1]["time"], body["data"][1]["side"], body["data"][1]["taker"]) == (
            "2026-11-06T21:20:00.003Z", "buy", True)

    def test_spot_fills_named_as_the_spot_methods_name_them(self):
        spot = [SimpleNamespace(coin="@107", symbol="HYPE"), SimpleNamespace(coin="@207", symbol="HYPE/USDT0")]
        answers = {"get_fills": [fill(T0, "BTC"), fill(T0 + 1, "@107"), fill(T0 + 2, "@207"), fill(T0 + 3, "#12")],
                   "get_spot_markets": spot}
        _, body, _ = run_tool("get_fills", {"market": "spot"}, answers)
        assert [f["symbol"] for f in body["data"]] == ["HYPE/USDT0", "HYPE"]
        _, body, _ = run_tool("get_fills", {"market": "spot", "symbol": "HYPE"}, answers)
        assert [(f["symbol"], f["time"]) for f in body["data"]] == [("HYPE", "2026-11-06T21:20:00.001Z")]
        is_error, body, _ = run_tool("get_fills", {"market": "spot", "symbol": "HYPE/USDC"}, answers)
        assert is_error and "did you mean 'HYPE', 'HYPE/USDT0'?" in body["error"]["message"]

    def test_spot_and_outcome_fills_are_left_out(self):
        answers = {"get_fills": [fill(T0, "BTC"), fill(T0 + 1, "@107"), fill(T0 + 2, "#12"), fill(T0 + 3, "xyz:NVDA")]}
        _, body, _ = run_tool("get_fills", {}, answers)
        assert [f["symbol"] for f in body["data"]] == ["xyz:NVDA", "BTC"]

    def test_one_symbol_is_filtered_from_all_fills(self):
        answers = {"get_fills": [fill(T0, "BTC"), fill(T0 + 1, "ETH"), fill(T0 + 2, "BTC")]}
        _, body, clients = run_tool("get_fills", {"symbol": "BTC"}, answers)
        assert ([f["time"] for f in body["data"]], body["truncated"], body["truncated_reason"]) == (
            ["2026-11-06T21:20:00.002Z", "2026-11-06T21:20:00.000Z"], False, None)
        assert clients[MAINNET].calls == [("get_fills", (), {"address": MAIN})]

    def test_at_hyperliquids_cap_older_fills_may_be_missing(self):
        # userFills returns at most the 2000 most recent: none of them may be ETH, but older ones can be.
        answers = {"get_fills": [fill(T0 + i, "BTC") for i in range(RECENT_FILLS)]}
        _, body, _ = run_tool("get_fills", {"symbol": "ETH"}, answers)
        assert (body["count"], body["truncated"], body["truncated_reason"]) == (0, True, "recent_fills")

    def test_a_time_range(self):
        _, body, clients = run_tool("get_fills", {"start": "2026-11-01T00:00:00Z", "end": "2026-11-02T00:00:00"},
                                    {"get_fill_history": history([fill(T0)])})
        assert (body["count"], body["truncated"], body["truncated_reason"]) == (1, False, None)
        assert clients[MAINNET].calls == [
            ("get_fill_history", (1793491200000, 1793577600000), {"address": MAIN})]  # no offset: UTC

    @pytest.mark.parametrize("reason", ["retention_window", "same_millisecond_overflow"])
    def test_a_range_hyperliquid_cut_short_says_why_even_within_the_limit(self, reason):
        answers = {"get_fill_history": history([fill(T0 + i) for i in range(5)], reason)}
        _, body, _ = run_tool("get_fills", {"start": "2026-01-01T00:00:00+02:00", "limit": 3}, answers)
        assert (body["count"], body["truncated"], body["truncated_reason"]) == (3, True, reason)
        assert body["data"][0]["time"] == "2026-11-06T21:20:00.004Z"  # the newest of the range

    @pytest.mark.parametrize("arguments, message", [
        ({"end": "2026-11-02T00:00:00Z"}, "end: only with start"),
        ({"start": "yesterday"}, "start: not an ISO 8601 time"),
        ({"start": "2026-11-02T00:00:00Z", "end": "2026-11-01T00:00:00Z"}, "end: before start"),
        ({"limit": 501}, "limit: 501 is greater than the maximum of 500"),
        ({"limit": 2.0}, "limit: 2.0 is not of type 'integer'"),       # an int, not an integral float
    ])
    def test_wrong_arguments(self, arguments, message):
        is_error, body, _ = run_tool("get_fills", arguments, {"get_fills": [], "get_fill_history": history([])})
        assert is_error and body["error"]["message"] == message


def status(order, state="filled"):
    return {"status": "order", "order": {"status": state, "statusTimestamp": T0 + 500, "order": order}}


class TestOrderStatus:
    def test_a_filled_order_has_the_sizes_of_get_open_orders(self):
        raw = status({"coin": "BTC", "side": "A", "limitPx": "85000.0", "sz": "0.0", "oid": 99, "timestamp": T0,
                      "triggerCondition": "N/A", "isTrigger": False, "triggerPx": "0.0", "reduceOnly": True,
                      "orderType": "Limit", "origSz": "0.001", "tif": "Ioc", "cloid": None})
        is_error, body, clients = run_tool("get_order_status", {"order_id": 99}, {"get_order_status": raw})
        assert not is_error and body["data"] == {
            "found": True, "status": "filled", "status_time": "2026-11-06T21:20:00.500Z", "order": {
                "order_id": 99, "cloid": None, "symbol": "BTC", "side": "sell", "type": "limit", "order_type": "Market",
                "is_market": True, "size": "0.001", "filled_size": "0.001", "reduced_size": None,
                "remaining_size": "0.000", "limit_price": "85000.0", "trigger_price": None, "slippage_cap_pct": None,
                "trigger_condition": "N/A", "reduce_only": True, "is_position_tpsl": None, "time_in_force": "Ioc",
                "created_at": "2026-11-06T21:20:00.000Z", "children": []}}
        assert clients[MAINNET].calls == [("get_order_status", (99,), {"address": MAIN})]

    def test_a_trigger_order(self):
        raw = status({"coin": "ETH", "side": "A", "limitPx": "2900.0", "sz": "0.5", "oid": 5, "timestamp": T0,
                      "triggerCondition": "Price below 3000", "isTrigger": True, "triggerPx": "3000.0",
                      "reduceOnly": True, "orderType": "Stop Market", "origSz": "0.5", "tif": None,
                      "cloid": "0x" + "1" * 32}, state="open")
        _, body, _ = run_tool("get_order_status", {"order_id": 5}, {"get_order_status": raw})
        order = body["data"]["order"]
        assert (body["data"]["status"], order["type"], order["trigger_price"], order["filled_size"],
                order["cloid"]) == ("open", "stop_loss", "3000.0", "0.0", "0x" + "1" * 32)

    def test_the_order_reads_as_in_get_open_orders(self):
        # The same order in both tools: the same fields and values (orderStatus has the format
        # of frontendOpenOrders).
        raw = {"coin": "ETH", "side": "A", "limitPx": "2900.0", "sz": "0.5", "oid": 5, "timestamp": T0,
               "triggerCondition": "Price below 3000", "isTrigger": True, "triggerPx": "3000.0",
               "reduceOnly": True, "orderType": "Stop Limit", "origSz": "0.5", "tif": None, "cloid": None}
        _, by_id, _ = run_tool("get_order_status", {"order_id": 5}, {"get_order_status": status(raw)})
        _, listed, _ = run_tool("get_open_orders", {}, {
            "get_open_orders": [_answers.parse_open_order(raw, raw["coin"])]})
        assert by_id["data"]["order"] == listed["data"][0]

    def test_an_unknown_order(self):
        _, body, _ = run_tool("get_order_status", {"order_id": 1}, {"get_order_status": {"status": "unknownOid"}})
        assert body["data"] == {"found": False}

    @pytest.mark.parametrize("arguments", [{}, {"order_id": "99"}, {"order_id": 99.0}, {"order_id": -1}])
    def test_the_order_id_is_required_and_a_whole_number(self, arguments):
        is_error, body, _ = run_tool("get_order_status", arguments, {})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"


def payment(time, usdc, symbol="BTC"):
    return FundingPayment(time=time, symbol=symbol, market=coin_market(symbol), size=Decimal("-2"),
                          funding_rate=Decimal("0.0000125"),
                          usdc=Decimal(usdc))


def pnl_report():
    """Two BTC trades (a win and a loss) and an open ETH trade, built by the library."""
    from fractrade_hl_simple import pnl

    def f(time, side, size, start, symbol="BTC", price="100", closed="0"):
        return Fill(symbol=symbol, side=side, price=Decimal(price), size=Decimal(size), closed_pnl=Decimal(closed),
                    direction="", order_id=1, crossed=True, time=time, hash="", fee=Decimal("0.123"),
                    start_position=Decimal(start), fee_token="USDC")

    fills = [f(T0 + 1, "B", "1", "0", price="100.123456789"), f(T0 + 2, "A", "1", "1", closed="10"),
             f(T0 + 3, "A", "2", "0"), f(T0 + 4, "B", "2", "-2", closed="-30.004"),
             f(T0 + 5, "B", "0.5", "0", symbol="ETH")]
    trades = pnl.perp_trades(fills, [], T0, positions=[position("ETH", "0.5")])
    return pnl.report(trades, T0, T0 + 9, False, None)


class TestPnl:
    ARGS = {"start": "2026-11-06T00:00:00Z", "end": "2026-11-07T00:00:00Z"}

    def test_trades_oldest_first_in_cents(self):
        is_error, body, clients = run_tool("get_pnl", {**self.ARGS, "symbol": "BTC", "spot": True},
                                           {"get_pnl": pnl_report()})
        assert not is_error, body
        first = body["data"][0]
        assert (first["symbol"], first["side"], first["status"], first["entry_price"], first["fees"],
                first["net_pnl"]) == ("BTC", "long", "closed", "100.123", "0.25", "9.75")
        assert [t["symbol"] for t in body["data"]] == ["BTC", "BTC", "ETH"]
        assert body["data"][2]["unrealized_pnl"] == "2.00"
        assert body["totals"]["net_pnl"] == "-20.62" and body["totals"]["unrealized_pnl"] == "2.00"
        (call,) = [c for c in clients[MAINNET].calls if c[0] == "get_pnl"]
        assert call[1][2] == "BTC" and call[2] == {"spot": True, "address": MAIN}

    def test_a_limit_keeps_the_most_recent_trades_and_the_totals_of_all(self):
        _, body, _ = run_tool("get_pnl", {**self.ARGS, "limit": 2}, {"get_pnl": pnl_report()})
        assert [(t["side"], t["symbol"]) for t in body["data"]] == [("short", "BTC"), ("long", "ETH")]
        assert (body["count"], body["truncated"], body["truncated_reason"]) == (2, True, "limit")
        assert body["totals"]["net_pnl"] == "-20.62"

    def test_by_market_each_currency_on_its_own(self):
        # A HIP-3 dex may use another collateral: its trades are never added to USDC ones.
        report = pnl_report()
        report.trades[1].currency = "USDH"
        _, body, _ = run_tool("get_pnl", {**self.ARGS, "group_by": "market"}, {"get_pnl": report})
        assert sorted((m["symbol"], m["currency"], m["trades"]) for m in body["data"]) == [
            ("BTC", "USDC", 1), ("BTC", "USDH", 1), ("ETH", "USDC", 1)]


class TestFundingPayments:
    def test_newest_first_with_the_total_of_the_whole_range(self):
        answers = {"get_user_funding": [payment(T0, "-0.5"), payment(T0 + 3600000, "0.2"), payment(T0 + 7200000, "-0.1")]}
        _, body, _ = run_tool("get_funding_payments", {"start": "2026-11-06T00:00:00Z", "end": "2026-11-07T00:00:00Z",
                                                       "limit": 2}, answers)
        assert (body["from"], body["to"], body["total_usdc"], body["count"], body["truncated"]) == (
            "2026-11-06T00:00:00.000Z", "2026-11-07T00:00:00.000Z", "-0.4", 2, True)
        assert body["data"][0] == {"time": "2026-11-06T23:20:00.000Z", "symbol": "BTC", "position_size": "-2",
                                   "funding_rate": "0.0000125", "amount": "-0.1", "currency": "USDC"}

    def test_each_currency_has_its_own_total(self):
        # A HIP-3 dex may use another collateral: its payments are never added to USDC ones.
        answers = {"get_user_funding": [payment(T0, "-0.5"), payment(T0 + 1, "0.25", symbol="xyz:NVDA")],
                   "market_specs": {"xyz:NVDA": {"collateral": "USDH"}}}
        _, body, _ = run_tool("get_funding_payments", {}, answers)
        assert (body["total_usdc"], body["other_currencies"]) == ("-0.5", {"USDH": "0.25"})
        assert [row["currency"] for row in body["data"]] == ["USDH", "USDC"]

    def test_a_history_that_cannot_be_read_completely_is_an_error_not_a_partial_total(self):
        failure = HyperliquidException("More than 500 funding payments at 1 ms: the rest of the range cannot be read")
        is_error, body, _ = run_tool("get_funding_payments", {}, {"get_user_funding": failure})
        assert is_error and body["error"]["code"] == "HYPERLIQUID_ERROR" and "cannot be read" in body["error"]["message"]

    def test_the_last_seven_days_by_default(self):
        _, body, clients = run_tool("get_funding_payments", {"symbol": "BTC"}, {"get_user_funding": []})
        (_, (start, end, symbol), _), = clients[MAINNET].calls
        assert (end - start, symbol) == (7 * 86_400_000, "BTC")

    @pytest.mark.parametrize("arguments, message", [
        ({"start": "2026-09-01T00:00:00Z", "end": "2026-11-01T00:00:00Z"}, "start: the range is longer than 31 days"),
        ({"start": "2026-11-02T00:00:00Z", "end": "2026-11-01T00:00:00Z"}, "end: before start"),
    ])
    def test_wrong_ranges(self, arguments, message):
        is_error, body, _ = run_tool("get_funding_payments", arguments, {"get_user_funding": []})
        assert is_error and body["error"]["message"] == message


class TestPortfolio:
    def test_every_period_with_the_pnl_over_the_period(self):
        raw = [["day", {"accountValueHistory": [[T0, "1000.0"], [T0 + 60000, "1010.0"]],
                        "pnlHistory": [[T0, "500.0"], [T0 + 60000, "510.0"]], "vlm": "12.5"}],
               ["perpAllTime", {"accountValueHistory": [], "pnlHistory": [], "vlm": "0.0"}]]
        _, body, _ = run_tool("get_portfolio", {}, {"get_portfolio": raw})
        assert body["data"] == {
            "day": {"account_value": "1010.0", "pnl": "10.0", "volume": "12.5",
                    "from": "2026-11-06T21:20:00.000Z", "to": "2026-11-06T21:21:00.000Z"},
            "perp_all_time": {"account_value": None, "pnl": None, "volume": "0.0", "from": None, "to": None}}


def test_every_tool_refuses_unknown_parameters():
    conf = parse_config(CONFIG, "test.toml")
    for spec in read_tools(conf, AccountRegistry(conf, {}, lambda *a: None)):
        assert spec.input_schema["additionalProperties"] is False, spec.name
