"""What is signed and sent for the money paths: orders, stops and modifies run through the real
library and the real SDK, signing included; only the HTTP transport is replaced by a
Hyperliquid that answers as its API docs describe. The tests check the action as it is posted
to /exchange, so a rounding that the SDK's own mocks would hide shows here."""

from unittest.mock import patch

import pytest

from fractrade_hl_simple import HyperliquidAccount, HyperliquidClient

KEY = "0x" + "ab" * 32
ADDRESS = "0x" + "1" * 40
# BTC: 5 size decimals (prices up to 1 decimal); kPEPE: 0 size decimals (prices up to 6).
UNIVERSE = [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
            {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10}]
POSITION = {"coin": "BTC", "entryPx": "85000", "szi": "0.002", "leverage": {"type": "cross", "value": "5"},
            "liquidationPx": None, "marginUsed": "34", "positionValue": "170", "returnOnEquity": "0.01",
            "unrealizedPnl": "1.0"}
SUMMARY = {"accountValue": "1000", "totalMarginUsed": "100", "totalNtlPos": "170", "totalRawUsd": "1000"}


class Hyperliquid:
    """API.post of the SDK: /info answered as documented; every /exchange action recorded and
    answered as resting (or filled, for an immediate-or-cancel order)."""

    def __init__(self):
        self.actions = []

    def __call__(self, api, url_path, payload=None):
        if url_path == "/exchange":
            self.actions.append(payload["action"])
            orders = payload["action"].get("orders") or [m["order"] for m in payload["action"].get("modifies", [])]
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
                {"filled": {"oid": 7, "totalSz": o["s"], "avgPx": o["p"]}} if o["t"].get("limit", {}).get("tif") == "Ioc"
                else {"resting": {"oid": 7}} for o in orders]}}}
        return {
            "meta": {"universe": UNIVERSE},
            "spotMeta": {"tokens": [], "universe": []},
            "perpDexs": [None],
            "metaAndAssetCtxs": [{"universe": UNIVERSE}, [{"funding": "0", "openInterest": "1", "markPx": "85000"},
                                                          {"funding": "0", "openInterest": "1", "markPx": "0.0112"}]],
            "allMids": {"BTC": "85000.0", "kPEPE": "0.0112"},
            "clearinghouseState": {"marginSummary": SUMMARY, "crossMarginSummary": SUMMARY, "withdrawable": "800",
                                   "assetPositions": [{"type": "oneWay", "position": POSITION}]},
        }[payload["type"]]


@pytest.fixture
def sent():
    """A client with a key on the fake Hyperliquid; yields (client, the actions sent)."""
    HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None  # this fake's markets
    hyperliquid = Hyperliquid()
    with patch("hyperliquid.api.API.post", lambda api, url_path, payload=None: hyperliquid(api, url_path, payload)):
        client = HyperliquidClient(account=HyperliquidAccount(private_key=KEY, public_address=ADDRESS),
                                   trust_env=False, cache_market_specs=False, max_retries=0)
        yield client, hyperliquid.actions
    HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None


def only_order(actions):
    [action] = actions
    assert action["type"] == "order" and action["grouping"] == "na"
    [order] = action["orders"]
    return order


@pytest.mark.parametrize("call, wire", [
    # Sizes are rounded down to the lot, never up; prices to 5 significant figures, and from
    # 100000 on to an integer (a limit of 100006 stays 100006: 5 figures would make it 100010).
    (lambda c: c.buy("BTC", 0.0012399, limit_price=84999.37),
     {"a": 0, "b": True, "p": "84999", "s": "0.00123", "r": False, "t": {"limit": {"tif": "Gtc"}}}),
    (lambda c: c.sell("BTC", 0.001, limit_price=100006),
     {"a": 0, "b": False, "p": "100006", "s": "0.001", "r": False, "t": {"limit": {"tif": "Gtc"}}}),
    (lambda c: c.buy("kPEPE", 1000.9, limit_price=0.01123456),
     {"a": 1, "b": True, "p": "0.011235", "s": "1000", "r": False, "t": {"limit": {"tif": "Gtc"}}}),
    (lambda c: c.buy("BTC", 0.001, limit_price=80000, post_only=True),
     {"a": 0, "b": True, "p": "80000", "s": "0.001", "r": False, "t": {"limit": {"tif": "Alo"}}}),
    # A market order is immediate or cancel at the mid plus (buy) or minus (sell) the slippage.
    (lambda c: c.buy("BTC", 0.001, slippage=0.05),
     {"a": 0, "b": True, "p": "89250", "s": "0.001", "r": False, "t": {"limit": {"tif": "Ioc"}}}),
    (lambda c: c.sell("BTC", 0.001, reduce_only=True, slippage=0.05),
     {"a": 0, "b": False, "p": "80750", "s": "0.001", "r": True, "t": {"limit": {"tif": "Ioc"}}}),
    # Stops and take profits of the long: reduce-only market triggers on the selling side, with
    # their limit beyond the trigger by Hyperliquid's 10 % tolerance (at the trigger it could keep
    # a triggered one from filling).
    (lambda c: c.stop_loss("BTC", 0.002, 80000.4),
     {"a": 0, "b": False, "p": "72000", "s": "0.002", "r": True,
      "t": {"trigger": {"isMarket": True, "triggerPx": "80000", "tpsl": "sl"}}}),
    (lambda c: c.take_profit("BTC", 0.002, 95000),
     {"a": 0, "b": False, "p": "85500", "s": "0.002", "r": True,
      "t": {"trigger": {"isMarket": True, "triggerPx": "95000", "tpsl": "tp"}}}),
])
def test_an_order_is_signed_at_the_markets_precision(sent, call, wire):
    client, actions = sent
    call(client)
    order = only_order(actions)
    assert {key: order[key] for key in wire} == wire
    assert len(order["c"]) == 34  # every order carries a client order id


def test_a_modify_sends_the_whole_new_order_for_the_order_id(sent):
    client, actions = sent
    client.modify_order(7, "BTC", True, 0.0012399, 79000.66, {"limit": {"tif": "Gtc"}})
    [action] = actions
    assert action["type"] == "batchModify"
    [modify] = action["modifies"]
    assert modify["oid"] == 7
    assert {key: modify["order"][key] for key in ("a", "b", "p", "s", "r", "t")} == {
        "a": 0, "b": True, "p": "79001", "s": "0.00123", "r": False, "t": {"limit": {"tif": "Gtc"}}}


@pytest.mark.parametrize("is_buy, trigger, limit_price, wire", [
    # a long stop market: on a rise, within the 10 % tolerance above its trigger
    (True, 90000, None, {"b": True, "p": "99000", "t": {"trigger": {"isMarket": True, "triggerPx": "90000", "tpsl": "sl"}}}),
    # a long stop limit: its limit
    (True, 90000, 90500, {"b": True, "p": "90500", "t": {"trigger": {"isMarket": False, "triggerPx": "90000", "tpsl": "sl"}}}),
    # a short stop market: on a fall, 10 % below its trigger
    (False, 80000, None, {"b": False, "p": "72000", "t": {"trigger": {"isMarket": True, "triggerPx": "80000", "tpsl": "sl"}}}),
])
def test_a_trigger_entry_is_a_stop_order_of_its_own(sent, is_buy, trigger, limit_price, wire):
    # Hyperliquid refuses a trigger order as the main order of a group ("Main order cannot be
    # trigger order."): it is sent alone, not reduce-only.
    client, actions = sent
    client.trigger_order("BTC", is_buy, 0.0012399, trigger, limit_price=limit_price)
    order = only_order(actions)
    assert {k: order[k] for k in ("b", "p", "s", "r", "t")} == {**wire, "s": "0.00123", "r": False}


def test_a_resting_limit_entry_is_sent_with_its_stop_and_take_profit_attached(sent):
    # As the SDK's example (examples/basic_tpsl.py) sends it: the limit order as the parent of a
    # normalTpsl group, then the take profit, then the stop, each with its limit beyond its trigger.
    client, actions = sent
    client.open_long_position("BTC", 0.0012399, stop_loss_price=80000, take_profit_price=90000, limit_price=84000)
    [action] = actions
    assert action["type"] == "order" and action["grouping"] == "normalTpsl"
    assert [(o["b"], o["p"], o["s"], o["r"], o["t"]) for o in action["orders"]] == [
        (True, "84000", "0.00123", False, {"limit": {"tif": "Gtc"}}),
        (False, "81000", "0.00123", True, {"trigger": {"isMarket": True, "triggerPx": "90000", "tpsl": "tp"}}),
        (False, "72000", "0.00123", True, {"trigger": {"isMarket": True, "triggerPx": "80000", "tpsl": "sl"}})]

