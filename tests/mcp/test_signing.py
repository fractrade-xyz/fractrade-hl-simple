"""What the trade tools sign: every tool runs through the real library and the real SDK,
signing included; only the HTTP transport is replaced by a Hyperliquid that answers as its
API docs describe. Every signature is recorded where the SDK makes it (`sign_inner`, which
every signing path of the SDK goes through), and every trading action before it is hashed
(`sign_l1_action`), so a withdrawal or transfer shows here whatever name, URL or payload
it is reached by. A trading action is signed as the EIP-712 type "Agent"; withdrawals,
transfers and approvals as "HyperliquidTransaction:..." types."""

from contextlib import ExitStack
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

from hyperliquid import exchange  # noqa: E402
from hyperliquid.utils import signing  # noqa: E402

from fractrade_hl_simple.hyperliquid import HyperliquidClient  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.server import make_client  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.trade import trade_tools  # noqa: E402

KEY = "0x" + "ab" * 32
MAIN = "0x" + "1" * 40
CLOID = "0x" + "c" * 32
# The trading actions an API wallet signs for its account; nothing else may ever be sent.
TRADING_ACTIONS = {"order", "cancel", "cancelByCloid", "batchModify", "updateLeverage"}
STOP = {"coin": "BTC", "side": "A", "limitPx": "80000.0", "sz": "0.002", "oid": 6, "timestamp": 1_700_000_000_000,
        "origSz": "0.002", "triggerPx": "80000.0", "isTrigger": True, "orderType": "Stop Market",
        "reduceOnly": True, "tif": None, "cloid": None}
SUMMARY = {"accountValue": "1000", "totalMarginUsed": "100", "totalNtlPos": "170", "totalRawUsd": "1000"}
POSITION = {"coin": "BTC", "entryPx": "85000", "szi": "0.002", "leverage": {"type": "cross", "value": "5"},
            "liquidationPx": None, "marginUsed": "34", "positionValue": "170", "returnOnEquity": "0.01",
            "unrealizedPnl": "1.0"}


class Hyperliquid:
    """API.post of the SDK: every request is recorded before anything else; /info is answered
    as documented, /exchange accepts any action (as it would a valid transfer)."""

    def __init__(self, role):
        self.role, self.requests, self.unanswered = role, [], []
        self.trigger = None  # the last stop or take profit placed: it rests, as order 9

    def __call__(self, api, url_path, payload=None):
        self.requests.append((url_path, payload))
        if url_path == "/exchange":
            return self.exchange(payload["action"])
        kind = payload.get("type") if isinstance(payload, dict) else None
        answer = self.info(kind)
        if answer is None:
            self.unanswered.append((url_path, payload))  # asserted after: a caught error cannot hide it
            return {}
        return answer

    def info(self, kind):
        if kind == "spotMeta":
            return {"tokens": [], "universe": []}
        if kind == "perpDexs":
            return [None]
        if kind == "meta":
            return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}
        if kind == "metaAndAssetCtxs":
            return [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
                    [{"funding": "0.0001", "openInterest": "1000", "markPx": "85000"}]]
        if kind == "allMids":
            return {"BTC": "85000.0"}
        if kind == "userRole":
            return self.role
        if kind == "activeAssetData":
            return {"leverage": {"type": "cross", "value": 3}}
        if kind == "clearinghouseState":
            return {"marginSummary": SUMMARY, "crossMarginSummary": SUMMARY, "withdrawable": "800",
                    "assetPositions": [{"type": "oneWay", "position": POSITION}]}
        if kind == "frontendOpenOrders":
            return [STOP] + ([self.trigger] if self.trigger else [])
        if kind == "openOrders":
            return [{"coin": "BTC", "oid": 6}]
        return None

    def exchange(self, action):
        kind = action["type"]
        if kind in ("cancel", "cancelByCloid"):
            return {"status": "ok", "response": {"type": "cancel", "data": {
                "statuses": ["success"] * len(action["cancels"])}}}
        if kind not in ("order", "batchModify"):  # updateLeverage, and anything else: accepted, as a transfer would be
            return {"status": "ok", "response": {"type": "default"}}
        orders = action["orders"] if kind == "order" else [m["order"] for m in action["modifies"]]
        for o in orders:
            if "trigger" in o["t"]:
                self.trigger = {**STOP, "oid": 9, "side": "B" if o["b"] else "A", "sz": o["s"], "origSz": o["s"],
                                "triggerPx": o["t"]["trigger"]["triggerPx"], "reduceOnly": o["r"],
                                "orderType": "Stop Market" if o["t"]["trigger"]["tpsl"] == "sl" else "Take Profit Market"}
        statuses = [{"filled": {"oid": 9, "totalSz": o["s"], "avgPx": o["p"]}} if o["t"].get("limit", {}).get("tif") == "Ioc"
                    else {"resting": {"oid": 9}} for o in orders]
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}


CALLS = [
    ("buy", {"symbol": "BTC", "size": 0.001}),
    ("buy", {"symbol": "BTC", "size": 0.001, "limit_price": 80000, "cloid": CLOID}),
    ("sell", {"symbol": "BTC", "size": 0.001, "limit_price": 90000, "post_only": True}),
    ("open_position", {"symbol": "BTC", "side": "long", "size": 0.002,
                       "stop_losses": [{"price": 70000, "fraction": 0.5}, {"price": 69000, "fraction": 0.5}],
                       "take_profits": [{"price": 99000, "fraction": 1}]}),
    ("place_trigger_entry", {"symbol": "BTC", "side": "long", "size": 0.001, "trigger_price": 90000}),
    ("open_position", {"symbol": "BTC", "side": "long", "size": 0.001, "limit_price": 84000,
                       "stop_losses": [{"price": 82000, "fraction": 1}], "take_profits": [{"price": 90000, "fraction": 1}]}),
    ("modify_order", {"order_id": 7, "symbol": "BTC", "is_buy": True, "size": 0.001, "price": 79000,  # a limit order
                      "order_type": {"limit": {"tif": "Gtc"}}}),
    ("place_orders", {"orders": [{"symbol": "BTC", "is_buy": True, "size": 0.001, "limit_price": 80000},
                                 {"symbol": "BTC", "is_buy": False, "size": 0.001, "limit_price": 90000,
                                  "post_only": True, "cloid": "0x" + "d" * 32}]}),
    ("place_ladder", {"symbol": "BTC", "is_buy": True, "from_price": 80000, "to_price": 79000, "steps": 3,
                      "notional_usd": 100}),
    ("cancel_order", {"order_id": 9, "symbol": "BTC"}),
    ("cancel_order_by_cloid", {"cloid": CLOID, "symbol": "BTC"}),
    ("cancel_orders", {"orders": [{"order_id": 9, "symbol": "BTC"}, {"order_id": 10, "symbol": "BTC"}]}),
    ("stop_loss", {"symbol": "BTC", "size": 0.002, "trigger_price": 80000}),
    ("take_profit", {"symbol": "BTC", "size": 0.002, "trigger_price": 95000}),
    ("update_stop_loss", {"symbol": "BTC", "new_price": 81000}),
    ("update_take_profit", {"symbol": "BTC", "new_price": 96000}),
    # The fake's stop already is this level: kept, nothing to sign.
    ("sync_protection", {"symbol": "BTC", "stop_losses": [{"price": 80000, "fraction": 1}]}),
    ("set_leverage", {"symbol": "BTC", "leverage": 3}),
    ("close", {"symbol": "BTC"}),
    ("close_all_positions", {}),
    ("cancel_all_orders", {}),
    ("kill_switch", {}),
]


class Signatures:
    """The SDK's signing, recorded: each signature's EIP-712 type, each trading action's type."""

    def __init__(self):
        self.types, self.actions = [], []

    def patches(self):
        inner, l1 = signing.sign_inner, signing.sign_l1_action

        def sign_inner(wallet, data):
            self.types.append(data["primaryType"])
            return inner(wallet, data)

        def sign_l1_action(wallet, action, *args, **kwargs):
            # Recorded before its shape is relied on (a multi-sig envelope is a list), and
            # signed as it would be: a wrapper that failed here would hide the signature.
            self.actions.append(action.get("type") if isinstance(action, dict) else f"<{type(action).__name__}>")
            return l1(wallet, action, *args, **kwargs)

        return [patch.object(signing, "sign_inner", sign_inner), patch.object(signing, "sign_l1_action", sign_l1_action),
                patch.object(exchange, "sign_l1_action", sign_l1_action)]


def run_every_tool(role):
    """Run CALLS through the trade tools: the signatures, the requests and each call's error code."""
    conf = parse_config(f'''
[accounts.main]
address = "{MAIN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 1000
max_leverage = 5
''', "test.toml")
    hyperliquid, signatures = Hyperliquid(role), Signatures()
    registry = AccountRegistry(conf, {"main": KEY}, make_client, shared_state())
    tools = {spec.name: spec for spec in trade_tools(registry)}
    assert {name for name, _ in CALLS} == set(tools)  # every trade tool is run
    outcomes = []
    HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None  # this fake's markets
    try:
        with ExitStack() as stack:
            stack.enter_context(patch("hyperliquid.api.API.post",
                                      lambda api, url_path, payload=None: hyperliquid(api, url_path, payload)))
            for applied in signatures.patches():
                stack.enter_context(applied)
            for name, arguments in CALLS:
                try:
                    tools[name].run(arguments)
                    outcomes.append((name, None))
                except Exception as error:  # recorded: an error is an outcome too
                    outcomes.append((name, getattr(error, "code", type(error).__name__)))
    finally:
        HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None
    return signatures, hyperliquid, outcomes


def test_every_tool_signs_trading_actions_only():
    signatures, hyperliquid, outcomes = run_every_tool({"role": "agent", "data": {"user": MAIN}})
    assert [outcome for outcome in outcomes if outcome[1] is not None] == []  # every call went through
    assert hyperliquid.unanswered == []
    # Every signature is a trading action's (EIP-712 "Agent"); never a user-signed one.
    assert set(signatures.types) == {"Agent"} and len(signatures.types) == len(signatures.actions)
    assert set(signatures.actions) == TRADING_ACTIONS  # every trading action exercised, nothing else
    sent = [payload for path, payload in hyperliquid.requests if path != "/info"]
    assert {path for path, _ in hyperliquid.requests} == {"/info", "/exchange"}
    assert len(sent) == len(signatures.types)  # each signature sent once, to /exchange
    assert all(payload["vaultAddress"] is None for payload in sent)


def test_a_key_that_is_no_api_wallet_signs_nothing():
    # A wallet of its own can withdraw: not one signature is made with it, by any tool.
    signatures, hyperliquid, outcomes = run_every_tool({"role": "user"})
    assert signatures.types == [] and [path for path, _ in hyperliquid.requests if path != "/info"] == []
    refused = {name for name, code in outcomes if code == "SIGNER_NOT_AUTHORIZED"}
    assert refused == {name for name, _ in CALLS} - {"kill_switch"}  # the kill switch signs nothing


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


class ThreeDexes(Hyperliquid):
    """Hyperliquid with two HIP-3 dexes listed, xyz and para, each with its own markets: their
    asset ids are 110000 and 120000 plus the market's place in its dex (SDK Info)."""
    UNIVERSES = {"": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}],
                 "xyz": [{"name": "xyz:NVDA", "szDecimals": 3, "maxLeverage": 10},
                         {"name": "xyz:TSLA", "szDecimals": 3, "maxLeverage": 10}],
                 "para": [{"name": "para:ABC", "szDecimals": 1, "maxLeverage": 5}]}

    def __call__(self, api, url_path, payload=None):
        kind = payload.get("type") if url_path == "/info" and isinstance(payload, dict) else None
        dex = payload.get("dex") or "" if kind else ""
        if kind == "perpDexs":
            answer = [None, {"name": "xyz"}, {"name": "para"}]
        elif kind == "meta":
            answer = {"universe": self.UNIVERSES[dex]}
        elif kind == "metaAndAssetCtxs":
            answer = [{"universe": self.UNIVERSES[dex]},
                      [{"funding": "0", "openInterest": "1", "markPx": "100"} for _ in self.UNIVERSES[dex]]]
        else:
            return super().__call__(api, url_path, payload)
        self.requests.append((url_path, payload))
        return answer


def test_a_batch_on_three_dexes_signs_each_order_and_cancel_for_its_own_market():
    conf = parse_config(f'''
[accounts.main]
address = "{MAIN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 1000
max_leverage = 5
perp_dexs = ["xyz", "para"]
''', "test.toml")
    hyperliquid = ThreeDexes({"role": "agent", "data": {"user": MAIN}})
    registry = AccountRegistry(conf, {"main": KEY}, make_client, shared_state())
    tools = {spec.name: spec for spec in trade_tools(registry)}
    HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None  # this fake's markets
    try:
        with patch("hyperliquid.api.API.post", lambda api, url_path, payload=None: hyperliquid(api, url_path, payload)):
            tools["place_orders"].run({"orders": [
                {"symbol": "xyz:TSLA", "is_buy": True, "size": 0.1, "limit_price": 100, "cloid": CLOID},
                {"symbol": "BTC", "is_buy": True, "size": 0.001, "limit_price": 80000, "reduce_only": True},
                {"symbol": "para:ABC", "is_buy": True, "size": 1, "limit_price": 50, "post_only": True}]})
            tools["cancel_orders"].run({"orders": [{"order_id": 7, "symbol": "para:ABC"},
                                                   {"order_id": 8, "symbol": "xyz:NVDA"}]})
    finally:
        HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None
    actions = [payload["action"] for path, payload in hyperliquid.requests if path == "/exchange"]
    assert [action["type"] for action in actions] == ["order", "cancel"]
    orders = actions[0]["orders"]
    assert [(o["a"], o["b"], o["p"], o["s"], o["r"], o["t"]) for o in orders] == [
        (110001, True, "100", "0.1", False, {"limit": {"tif": "Gtc"}}),
        (0, True, "80000", "0.001", True, {"limit": {"tif": "Gtc"}}),
        (120000, True, "50", "1", False, {"limit": {"tif": "Alo"}})]
    assert orders[0]["c"] == CLOID and all(len(o["c"]) == 34 for o in orders)
    assert actions[1]["cancels"] == [{"a": 120000, "o": 7}, {"a": 110000, "o": 8}]
