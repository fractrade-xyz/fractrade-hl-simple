"""The market read tools: which data, which network, output shapes, arguments."""

import json
from decimal import Decimal

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
from hyperliquid.utils import constants  # noqa: E402
from mcp import Client  # noqa: E402

from fractrade_hl_simple.exceptions import InvalidArgumentException  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.server import build_server  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import XYZ_NOTE, read_tools  # noqa: E402
from fractrade_hl_simple.models import Market, SpotMarket  # noqa: E402

MAINNET, TESTNET = constants.MAINNET_API_URL, constants.TESTNET_API_URL
CONFIG = f'[accounts.main]\naddress = "0x{"1" * 40}"\nperp_dexs = ["xyz"]\n'


def market(symbol, volume, funding="0.0001", delisted=False, **fields):
    values = dict(symbol=symbol, dex=symbol.split(":")[0] if ":" in symbol else None, size_decimals=3, max_leverage=20,
                  margin_mode="cross", delisted=delisted, at_open_interest_cap=False, mark_price=Decimal("100.5"),
                  mid_price=None if delisted else Decimal("100.4"), oracle_price=Decimal("100.6"),
                  previous_day_price=Decimal("100"), funding=Decimal(funding), premium=Decimal("-0.0001"),
                  open_interest=Decimal("1000"), day_volume_usd=Decimal(volume))
    return Market(**{**values, **fields})


MARKETS = [
    market("ETH", "500", funding="0.0002"),
    market("BTC", "900", funding="-0.0005"),
    market("xyz:NVDA", "700", funding="0.00001"),
    market("xyz:GOLD", "100", funding="0.0003", margin_mode="no_cross", at_open_interest_cap=True),
    market("OLD", "0", delisted=True),
]


def pair(symbol, quote, volume, mid="1.01"):
    base = symbol.split("/")[0]
    return SpotMarket(symbol=symbol, coin=f"@{len(symbol)}", base=base, quote=quote, size_decimals=2,
                      mark_price=Decimal("1.0"), mid_price=None if mid is None else Decimal(mid),
                      previous_day_price=Decimal("0.9"), day_volume=Decimal(volume))


SPOT = [pair("PURR", "USDC", "900"), pair("HYPE", "USDC", "5000"), pair("HYPE/USDT0", "USDT0", "100", mid=None)]


class FakeMarketClient:
    def __init__(self, book=None):
        self.book, self.calls = book, []

    def get_markets(self):
        self.calls.append(("get_markets",))
        return MARKETS

    def get_spot_markets(self):
        self.calls.append(("get_spot_markets",))
        return SPOT

    def get_order_book(self, symbol, **kwargs):
        self.calls.append(("get_order_book", symbol, kwargs))
        return self.book

    def get_spot_order_book(self, token, **kwargs):
        self.calls.append(("get_spot_order_book", token, kwargs))
        return self.book


def run_tool(name, arguments, book=None, make_client=None):
    clients = {}

    def fake_clients(base_url, account, default_slippage, perp_dexs):
        assert account is None  # market data never uses a client with a key
        return clients.setdefault(base_url, FakeMarketClient(book))

    make_client = make_client or fake_clients

    conf = parse_config(CONFIG, "test.toml")
    server = build_server(read_tools(conf, AccountRegistry(conf, {}, make_client)))

    async def call():
        async with Client(server, mode="2026-07-28") as client:
            result = await client.call_tool(name, arguments)
            return result.is_error, json.loads(result.content[0].text)

    is_error, body = anyio.run(call)
    return is_error, body, clients


@pytest.mark.parametrize("tool, arguments", [
    ("get_prices", {"dex": "default"}), ("list_markets", {}), ("get_market_info", {"symbol": "BTC"}),
    ("get_funding_rates", {}),
])
def test_market_tools_read_mainnet_unless_told_otherwise(tool, arguments):
    _, body, clients = run_tool(tool, arguments)
    assert body["base_url"] == MAINNET and list(clients) == [MAINNET]
    _, body, clients = run_tool(tool, {**arguments, "base_url": TESTNET})
    assert body["base_url"] == TESTNET and list(clients) == [TESTNET]


class TestSpot:
    def test_spot_prices_of_the_pairs_given(self):
        _, body, clients = run_tool("get_prices", {"market": "spot", "symbols": ["HYPE/USDT0", "HYPE"]})
        assert body["data"] == [{"symbol": "HYPE/USDT0", "mid_price": None, "mark_price": "1.0"},
                                {"symbol": "HYPE", "mid_price": "1.01", "mark_price": "1.0"}]
        assert clients[MAINNET].calls == [("get_spot_markets",)]  # spot never reads the perp markets

    def test_spot_prices_of_the_most_traded_pairs(self):
        _, body, _ = run_tool("get_prices", {"market": "spot", "limit": 2})
        assert ([p["symbol"] for p in body["data"]], body["truncated"]) == (["HYPE", "PURR"], True)

    def test_spot_markets_most_traded_first_with_their_quote(self):
        _, body, _ = run_tool("list_markets", {"market": "spot", "query": "hype"})
        assert body["data"] == [
            {"symbol": "HYPE", "base": "HYPE", "quote": "USDC", "size_decimals": 2, "mark_price": "1.0",
             "mid_price": "1.01", "day_volume": "5000"},
            {"symbol": "HYPE/USDT0", "base": "HYPE", "quote": "USDT0", "size_decimals": 2, "mark_price": "1.0",
             "mid_price": None, "day_volume": "100"}]

    @pytest.mark.parametrize("tool", ["get_prices", "list_markets"])
    def test_dex_is_a_perp_filter(self, tool):
        is_error, body, clients = run_tool(tool, {"market": "spot", "dex": "xyz"})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT" and "dex" in body["error"]["message"]
        assert clients == {}  # refused before any client is made

    def test_an_unknown_pair_gets_the_pairs_of_its_token_suggested(self):
        is_error, body, _ = run_tool("get_prices", {"market": "spot", "symbols": ["HYPE/USDC"]})
        assert is_error and body["error"]["code"] == "UNKNOWN_SYMBOL"
        assert "did you mean 'HYPE', 'HYPE/USDT0'?" in body["error"]["message"]


class TestPrices:
    def test_the_symbols_given_in_their_order(self):
        _, body, _ = run_tool("get_prices", {"symbols": ["xyz:NVDA", "BTC"]})
        assert body["data"] == [{"symbol": "xyz:NVDA", "mid_price": "100.4", "mark_price": "100.5"},
                                {"symbol": "BTC", "mid_price": "100.4", "mark_price": "100.5"}]

    def test_a_dex_most_traded_first_without_delisted_markets(self):
        _, body, _ = run_tool("get_prices", {"dex": "default", "limit": 1})
        assert ([p["symbol"] for p in body["data"]], body["truncated"]) == (["BTC"], True)
        _, body, _ = run_tool("get_prices", {"dex": "xyz"})
        assert [p["symbol"] for p in body["data"]] == ["xyz:NVDA", "xyz:GOLD"]

    @pytest.mark.parametrize("tool", ["get_prices", "list_markets"])
    def test_a_dex_that_is_not_enabled_is_refused_before_any_client_is_made(self, tool):
        # An empty list would read as "no markets there".
        is_error, body, clients = run_tool(tool, {"dex": "para"})
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"
        assert "'para' is not enabled" in body["error"]["message"]
        assert body["error"]["hint"] == "enabled: default, xyz; enable_perp_dexs adds a HIP-3 dex"
        assert clients == {}

    def test_never_every_market_and_refused_before_any_client_is_made(self):
        def no_client(base_url, account, default_slippage, perp_dexs):
            raise AssertionError("no client for an argument error")  # would come back as INTERNAL

        is_error, body, _ = run_tool("get_prices", {}, make_client=no_client)
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT" and "list_markets" in body["error"]["hint"]

    def test_a_delisted_market_asked_for_by_name_is_left_out(self):
        _, body, _ = run_tool("get_prices", {"symbols": ["OLD", "BTC"]})
        assert ([p["symbol"] for p in body["data"]], body["count"]) == (["BTC"], 1)

    def test_an_unknown_symbol_names_a_similar_one(self):
        is_error, body, _ = run_tool("get_prices", {"symbols": ["BTC", "nvda"]})
        assert is_error and body["error"]["code"] == "UNKNOWN_SYMBOL"
        assert body["error"]["message"] == "Unknown symbol 'nvda'; did you mean 'xyz:NVDA'?"

    @pytest.mark.parametrize("arguments", [{"symbols": []}, {"symbols": ["BTC", "BTC"]}, {"dex": ""},
                                           {"symbols": ["BTC"] * 51}])
    def test_wrong_arguments(self, arguments):
        is_error, body, _ = run_tool("get_prices", arguments)
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"


class TestMarkets:
    def test_open_markets_most_traded_first(self):
        _, body, _ = run_tool("list_markets", {})
        assert [m["symbol"] for m in body["data"]] == ["BTC", "xyz:NVDA", "ETH", "xyz:GOLD"]
        assert body["data"][3] == {
            "symbol": "xyz:GOLD", "dex": "xyz", "max_leverage": 20, "margin_mode": "no_cross", "size_decimals": 3,
            "mark_price": "100.5", "funding_hourly": "0.0003", "open_interest": "1000", "day_volume_usd": "100",
            "at_open_interest_cap": True}

    def test_a_query_and_a_limit(self):
        _, body, _ = run_tool("list_markets", {"query": "XYZ:", "limit": 1})
        assert ([m["symbol"] for m in body["data"]], body["count"], body["truncated"]) == (["xyz:NVDA"], 1, True)

    def test_one_market_in_full(self):
        _, body, _ = run_tool("get_market_info", {"symbol": "xyz:NVDA"})
        data = body["data"]
        assert (data["mid_price"], data["oracle_price"], data["previous_day_price"], data["change_24h_percent"],
                data["delisted"], data["note"]) == ("100.4", "100.6", "100", 0.5, False, XYZ_NOTE)
        _, body, _ = run_tool("get_market_info", {"symbol": "BTC"})
        assert body["data"]["note"] is None

    def test_a_delisted_market_is_still_described(self):
        _, body, _ = run_tool("get_market_info", {"symbol": "OLD"})
        assert (body["data"]["delisted"], body["data"]["mid_price"]) == (True, None)


# What the library's get_order_book(..., decimal=True) returns.
BOOK = {"symbol": "BTC", "timestamp": 1794000000000, "bids": [
    {"price": Decimal("0.122"), "size": Decimal("10.0"), "num_orders": 2},
    {"price": Decimal("0.121"), "size": Decimal("90071992.54740993"), "num_orders": 1}],
    "asks": [{"price": Decimal("0.123"), "size": Decimal("0.00001"), "num_orders": 3}]}


class TestOrderBook:
    def test_levels_spread_and_basis_points_without_float_artifacts(self):
        _, body, clients = run_tool("get_order_book", {"symbol": "BTC"}, book=BOOK)
        assert body["data"] == {
            "symbol": "BTC", "time": "2026-11-06T21:20:00.000Z", "best_bid": "0.122", "best_ask": "0.123",
            "mid_price": "0.1225", "spread": "0.001", "spread_bps": 81.63,
            "bids": [{"price": "0.122", "size": "10.0", "orders": 2},
                     {"price": "0.121", "size": "90071992.54740993", "orders": 1}],  # a float would lose a digit
            "asks": [{"price": "0.123", "size": "0.00001", "orders": 3}]}
        assert clients[MAINNET].calls == [("get_order_book", "BTC", {"decimal": True})]

    def test_depth_and_spot(self):
        _, body, clients = run_tool("get_order_book", {"symbol": "HYPE", "market": "spot", "depth": 1}, book=BOOK)
        assert (len(body["data"]["bids"]), len(body["data"]["asks"])) == (1, 1)
        assert clients[MAINNET].calls == [("get_spot_order_book", "HYPE", {"decimal": True})]

    def test_a_side_without_orders(self):
        _, body, _ = run_tool("get_order_book", {"symbol": "BTC"}, book={**BOOK, "asks": []})
        assert (body["data"]["best_ask"], body["data"]["mid_price"], body["data"]["spread_bps"]) == (None, None, None)

    @pytest.mark.parametrize("arguments", [{}, {"symbol": "BTC", "depth": 21}, {"symbol": "BTC", "depth": 0}])
    def test_wrong_arguments(self, arguments):
        is_error, body, _ = run_tool("get_order_book", arguments, book=BOOK)
        assert is_error and body["error"]["code"] == "INVALID_ARGUMENT"


class TestFundingRates:
    def test_largest_first_by_size_without_delisted_markets(self):
        _, body, _ = run_tool("get_funding_rates", {"top": 2})
        assert ([r["symbol"] for r in body["data"]], body["truncated"]) == (["BTC", "xyz:GOLD"], True)
        assert body["data"][0] == {"symbol": "BTC", "funding_hourly": "-0.0005", "funding_annualized": "-4.3800",
                                   "mark_price": "100.5"}

    def test_a_delisted_market_asked_for_by_name_is_left_out(self):
        _, body, _ = run_tool("get_funding_rates", {"symbols": ["OLD", "ETH"]})
        assert [r["symbol"] for r in body["data"]] == ["ETH"]

    def test_symbols_and_a_threshold(self):
        _, body, _ = run_tool("get_funding_rates", {"symbols": ["ETH", "xyz:NVDA"], "threshold": 0.0001})
        assert [r["symbol"] for r in body["data"]] == ["ETH"]


class DexClient(FakeMarketClient):
    """A market client whose markets are those of its dexes."""

    def __init__(self, perp_dexs):
        super().__init__(None)
        self.perp_dexs = perp_dexs

    def get_markets(self):
        every = [*MARKETS, market("para:TOTAL2", "50"), market("default:ABC", "800")]
        return [m for m in every if ":" not in m.symbol or m.symbol.split(":")[0] in self.perp_dexs]


class TestEnablePerpDexs:
    def run(self, *calls, make_client=None):
        """The results of several tool calls on one server, in order. A client knows the markets
        of its own dexes only, as the library's does."""
        made = []

        def fake_clients(base_url, account, default_slippage, perp_dexs):
            made.append((base_url, perp_dexs))
            return DexClient(perp_dexs)

        conf = parse_config(CONFIG, "test.toml")
        server = build_server(read_tools(conf, AccountRegistry(conf, {}, make_client or fake_clients)))

        async def call():
            async with Client(server, mode="2026-07-28") as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                results = []
                for name, arguments in calls:
                    result = await client.call_tool(name, arguments)
                    results.append((result.is_error, json.loads(result.content[0].text)))
                return tools, results

        tools, results = anyio.run(call)
        return tools, results, made

    def test_a_dex_enabled_for_the_session_can_be_filtered_by_at_once(self):
        tools, results, made = self.run(("get_prices", {"dex": "para"}),
                                        ("enable_perp_dexs", {"dexes": ["para"]}),
                                        ("get_prices", {"dex": "para"}))
        assert results[0][0] and results[0][1]["error"]["code"] == "INVALID_ARGUMENT"
        assert results[1] == (False, {
            "base_url": MAINNET, "perp_dexs": ["xyz", "para"], "enabled": ["para"],
            "note": "until the server restarts; to keep them, add perp_dexs to the config ([defaults] or the account)"})
        assert results[2] == (False, {"base_url": MAINNET, "count": 1, "truncated": False,
                                      "data": [{"symbol": "para:TOTAL2", "mid_price": "100.4", "mark_price": "100.5"}]})
        assert made == [(MAINNET, ("xyz", "para"))]

    def test_it_changes_what_is_read_not_anything_on_hyperliquid(self):
        tools, _, _ = self.run()
        hints = tools["enable_perp_dexs"].annotations
        assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint) == (False, False, True)

    def test_a_dex_the_network_does_not_have_is_refused_naming_the_dexes_it_has(self):
        # The library's message points to its get_perp_dexs(), which the agent cannot call.
        class Network(FakeMarketClient):
            def get_perp_dexs(self):
                return [{"name": "xyz", "full_name": "XYZ"}, {"name": "para", "full_name": "Paragon"}]

        def factory(base_url, account, default_slippage, perp_dexs):
            if "nope" in perp_dexs:
                raise InvalidArgumentException("'nope' is not a perp dex on mainnet; get_perp_dexs() lists them")
            return Network(None)

        _, results, _ = self.run(("enable_perp_dexs", {"dexes": ["para", "nope"]}), ("get_prices", {"dex": "para"}),
                                 make_client=factory)
        error = results[0][1]["error"]
        assert (error["code"], error["message"], error["hint"]) == (
            "INVALID_ARGUMENT", f"not a perp dex at {MAINNET}: nope", "its HIP-3 dexes: xyz, para")
        assert results[1][1]["error"]["code"] == "INVALID_ARGUMENT"  # para was not enabled either

    @pytest.mark.parametrize("dexes", [[], ["xyz", "xyz"], ["x y"], [""]])
    def test_wrong_names_are_refused(self, dexes):
        _, results, made = self.run(("enable_perp_dexs", {"dexes": dexes}))
        assert results[0][1]["error"]["code"] == "INVALID_ARGUMENT" and made == []


@pytest.mark.parametrize("tool, arguments", [
    ("get_market_info", {"symbol": "io:ANTH"}), ("get_order_book", {"symbol": "io:ANTH"}),
    ("get_prices", {"symbols": ["BTC", "io:ANTH"]}), ("get_funding_rates", {"symbols": ["io:ANTH"]}),
])
def test_a_symbol_of_a_dex_that_is_not_enabled_says_how_to_enable_it(tool, arguments):
    # "Unknown symbol" alone would read as a typo, or as a market that does not exist.
    is_error, body, clients = run_tool(tool, arguments)
    assert is_error and body["error"]["code"] == "UNKNOWN_SYMBOL"
    assert "the io dex is not enabled; enable_perp_dexs adds it" in body["error"]["message"]
    assert clients == {}  # known without a request


def test_the_default_filter_is_hyperliquids_own_perps_not_a_hip3_dex_named_default():
    conf = parse_config(f'[accounts.main]\naddress = "0x{"1" * 40}"\nperp_dexs = ["default"]\n', "test.toml")
    server = build_server(read_tools(conf, AccountRegistry(conf, {}, lambda *args: DexClient(args[3]))))

    async def call():
        async with Client(server, mode="2026-07-28") as client:
            result = await client.call_tool("list_markets", {"dex": "default"})
            return json.loads(result.content[0].text)

    assert [m["symbol"] for m in anyio.run(call)["data"]] == ["BTC", "ETH"]
