"""Shared mocked clients and MCP sessions for trade tests and audit reproductions."""

import json
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from fractrade_hl_simple.hyperliquid import HyperliquidClient  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.server import build_server  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import read_tools  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.trade import trade_tools  # noqa: E402

KEY = "0x" + "ab" * 32
OWN = "0x" + "1" * 40  # main's address; KEY is an API wallet of it (see Session)
OTHER = "0x" + "4" * 40
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


def state(*positions):
    """clearinghouseState with these (coin, size) positions."""
    summary = {"accountValue": "1000", "totalMarginUsed": "100", "totalNtlPos": "500", "totalRawUsd": "1000"}
    return {"marginSummary": summary, "crossMarginSummary": summary, "withdrawable": "800", "assetPositions": [
        {"type": "oneWay", "position": {
            "coin": coin, "entryPx": "85000", "szi": size, "leverage": {"type": "cross", "value": "10"},
            "liquidationPx": None, "marginUsed": "8.5", "positionValue": "85", "returnOnEquity": "0.01",
            "unrealizedPnl": "1.0"}} for coin, size in positions]}
