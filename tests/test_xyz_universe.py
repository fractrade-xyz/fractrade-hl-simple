"""Tests for extended perp universe (xyz: symbols) support."""
import time

import pytest
import requests
from hyperliquid.utils import constants
from unittest.mock import patch, MagicMock
from decimal import Decimal
from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.exceptions import (HyperliquidException, InvalidArgumentException, NetworkException,
                                          UnknownSymbolException)


# Sample xyz dex meta response
XYZ_META = {
    "universe": [
        {"name": "xyz:XYZ100", "szDecimals": 4, "maxLeverage": 30},
        {"name": "xyz:TSLA", "szDecimals": 3, "maxLeverage": 10},
        {"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25},
        {"name": "xyz:NVDA", "szDecimals": 3, "maxLeverage": 20},
    ]
}

XYZ_CTXS = [
    {"funding": "0.00001", "openInterest": "500", "markPx": "100.0"},
    {"funding": "-0.00006", "openInterest": "56000", "markPx": "380.0"},
    {"funding": "0.00002", "openInterest": "3000", "markPx": "4430.0"},
    {"funding": "0.00003", "openInterest": "10000", "markPx": "130.0"},
]

# Standard (default dex) meta response
DEFAULT_META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "onlyIsolated": False},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 50, "onlyIsolated": False},
    ]
}

DEFAULT_CTXS = [
    {"funding": "0.0001", "openInterest": "1000", "markPx": "85000"},
    {"funding": "0.00005", "openInterest": "5000", "markPx": "3200"},
]


@pytest.fixture
def mock_client():
    """Create a fully mocked authenticated client with both default and xyz dex data."""
    HyperliquidClient._cached_market_specs = None
    HyperliquidClient._cached_market_specs_at = 0

    with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
         patch('fractrade_hl_simple.hyperliquid.Exchange') as mock_exchange_cls:
        mock_info = mock_info_cls.return_value
        mock_exchange = mock_exchange_cls.return_value

        # Mock the post method used by _fetch_market_specs for each dex
        def mock_post(endpoint, payload):
            if payload.get("type") == "metaAndAssetCtxs":
                dex = payload.get("dex", "")
                if dex == "xyz":
                    return [XYZ_META, XYZ_CTXS]
                return [DEFAULT_META, DEFAULT_CTXS]
            return {}

        mock_info.post.side_effect = mock_post

        # Mock meta() for get_market_info
        def mock_meta(dex=""):
            if dex == "xyz":
                return XYZ_META
            return DEFAULT_META

        mock_info.meta.side_effect = mock_meta

        client = HyperliquidClient(max_retries=0, retry_delay=0, cache_market_specs=False, extended_universe=True)

        # Set up auth
        client.exchange = mock_exchange
        client.info = mock_info
        mock_info.name_to_coin = {name: name for name in client.market_specs}  # the SDK's symbol table
        client.account = MagicMock()
        client.account.public_address = "0x1234567890abcdef1234567890abcdef12345678"
        client.account.private_key = "0xdead"
        client.public_address = client.account.public_address
        client.exchange_account = MagicMock()

        # Re-assign the mock methods after replacing client.info
        client.info.post.side_effect = mock_post
        client.info.meta.side_effect = mock_meta

        yield client


# ── Market Specs ──────────────────────────────────────────────────────

class TestXyzMarketSpecs:
    def test_fetch_includes_xyz_symbols(self, mock_client):
        assert "BTC" in mock_client.market_specs
        assert "ETH" in mock_client.market_specs
        assert "xyz:TSLA" in mock_client.market_specs
        assert "xyz:GOLD" in mock_client.market_specs
        assert "xyz:NVDA" in mock_client.market_specs

    def test_xyz_specs_have_correct_values(self, mock_client):
        tsla = mock_client.market_specs["xyz:TSLA"]
        assert tsla["size_decimals"] == 3
        assert tsla["max_leverage"] == 10
        assert tsla["mark_price"] == "380.0"
        assert tsla["funding"] == "-0.00006"

    def test_xyz_specs_coexist_with_standard(self, mock_client):
        btc = mock_client.market_specs["BTC"]
        assert btc["size_decimals"] == 5
        assert btc["max_leverage"] == 50

        gold = mock_client.market_specs["xyz:GOLD"]
        assert gold["size_decimals"] == 4
        assert gold["max_leverage"] == 25

    def test_total_market_count(self, mock_client):
        # 2 standard + 4 xyz
        assert len(mock_client.market_specs) == 6

    def test_extended_client_does_not_reuse_specs_of_a_default_client(self):
        # The cache used to ignore the dex list: an xyz client created after a default one
        # got default-dex specs only, and sized xyz orders with 0 decimals.
        HyperliquidClient._cached_market_specs = None
        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info_cls.return_value.post.side_effect = lambda endpoint, payload: (
                [XYZ_META, XYZ_CTXS] if payload.get("dex") == "xyz" else [DEFAULT_META, DEFAULT_CTXS]
            )
            HyperliquidClient(max_retries=0)
            client = HyperliquidClient(max_retries=0, extended_universe=True)
        assert client.market_specs["xyz:NVDA"]["size_decimals"] == 3

    def test_refresh_while_xyz_metadata_is_unavailable_keeps_the_xyz_specs(self, mock_client):
        # A failed dex used to be skipped silently, dropping its markets from the specs.
        def mock_post(endpoint, payload):
            if payload.get("dex") == "xyz":
                raise requests.ConnectionError("Connection aborted")
            return [DEFAULT_META, DEFAULT_CTXS]

        mock_client.info.post.side_effect = mock_post
        mock_client.info.meta.side_effect = requests.ConnectionError("Connection aborted")
        with patch("fractrade_hl_simple.hyperliquid.Info", return_value=mock_client.info), \
                pytest.raises(NetworkException):
            mock_client.refresh_market_specs()  # raises, never a refresh without the xyz markets
        assert mock_client.market_specs["xyz:NVDA"]["size_decimals"] == 3

    def test_set_leverage_refuses_cross_margin_on_an_isolated_only_market(self, mock_client):
        # xyz:HOOD as in mainnet metadata (2026-11-13): isolated margin only.
        hood = {"szDecimals": 3, "name": "xyz:HOOD", "maxLeverage": 10, "onlyIsolated": True,
                "marginMode": "noCross"}
        mock_client.info.post.side_effect = lambda endpoint, payload: (
            [{"universe": [hood]}, [{}]] if payload.get("dex") == "xyz" else [DEFAULT_META, DEFAULT_CTXS]
        )
        with patch("fractrade_hl_simple.hyperliquid.Info", return_value=mock_client.info):
            mock_client.refresh_market_specs()
        mock_client.info.name_to_coin["xyz:HOOD"] = "xyz:HOOD"  # as the SDK's refreshed Info knows it
        with pytest.raises(ValueError, match="does not allow cross margin"):
            mock_client.set_leverage("xyz:HOOD", 5)
        mock_client.exchange.update_leverage.assert_not_called()


# ── Price Queries ─────────────────────────────────────────────────────

class TestXyzPrices:

    def test_get_price_xyz_not_found(self, mock_client):
        mock_client.info.all_mids.side_effect = lambda dex="": (
            {"xyz:TSLA": "380.0"} if dex == "xyz"
            else {"BTC": "85000.0"}
        )

        with pytest.raises(UnknownSymbolException, match="Unknown symbol 'xyz:FAKE'"):
            mock_client.get_price("xyz:FAKE")


# ── Market Info ───────────────────────────────────────────────────────

class TestXyzMarketInfo:
    def test_get_market_info_xyz_symbol(self, mock_client):
        info = mock_client.get_market_info("xyz:TSLA")
        assert info["name"] == "xyz:TSLA"
        assert info["szDecimals"] == 3

    def test_get_market_info_all_includes_xyz(self, mock_client):
        markets = mock_client.get_market_info()
        names = [m["name"] for m in markets]
        assert "BTC" in names
        assert "xyz:TSLA" in names
        assert "xyz:GOLD" in names

    def test_get_market_info_xyz_not_found(self, mock_client):
        with pytest.raises(UnknownSymbolException, match="Unknown symbol 'xyz:DOESNOTEXIST'"):
            mock_client.get_market_info("xyz:DOESNOTEXIST")


# ── Trading xyz Symbols ───────────────────────────────────────────────

class TestXyzTrading:
    def test_buy_xyz_symbol(self, mock_client):
        mock_client.info.all_mids.side_effect = lambda dex="": (
            {"xyz:TSLA": "380.0"} if dex == "xyz"
            else {"BTC": "85000.0"}
        )
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "380.5", "totalSz": "1.0"}}]}},
        }

        order = mock_client.buy("xyz:TSLA", 1.0)
        assert order.symbol == "xyz:TSLA"
        assert order.status == "filled"

        call_kwargs = mock_client.exchange.order.call_args
        assert call_kwargs.kwargs.get("name") or call_kwargs[1].get("name") == "xyz:TSLA"

    def test_sell_xyz_symbol(self, mock_client):
        mock_client.info.all_mids.side_effect = lambda dex="": (
            {"xyz:GOLD": "4430.0"} if dex == "xyz"
            else {"BTC": "85000.0"}
        )
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 2, "avgPx": "4425.0", "totalSz": "0.5"}}]}},
        }

        order = mock_client.sell("xyz:GOLD", 0.5)
        assert order.symbol == "xyz:GOLD"
        assert order.is_buy is False

    def test_limit_order_xyz_symbol(self, mock_client):
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 3}}]}},
        }

        order = mock_client.buy("xyz:NVDA", 2.0, limit_price=125.0)
        assert order.symbol == "xyz:NVDA"
        assert order.status == "open"

    def test_set_leverage_xyz_symbol(self, mock_client):
        mock_client.exchange.update_leverage.return_value = {"status": "ok"}
        result = mock_client.set_leverage("xyz:TSLA", 5)
        mock_client.exchange.update_leverage.assert_called_once_with(5, "xyz:TSLA", True)
        assert result == {"status": "ok"}

    def test_set_leverage_exceeds_xyz_max(self, mock_client):
        with pytest.raises(ValueError, match="Max leverage"):
            mock_client.set_leverage("xyz:TSLA", 20)  # max is 10


# ── SDK Initialization ────────────────────────────────────────────────

class TestXyzSdkInit:
    def test_default_is_crypto_only(self):
        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info_cls.return_value.post.return_value = [DEFAULT_META, DEFAULT_CTXS]
            HyperliquidClient(max_retries=0)
            call_kwargs = mock_info_cls.call_args
            assert call_kwargs.kwargs.get("perp_dexs") == [""]

    def test_extended_universe_enables_xyz(self):
        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info_cls.return_value.post.return_value = [DEFAULT_META, DEFAULT_CTXS]
            HyperliquidClient(max_retries=0, extended_universe=True)
            call_kwargs = mock_info_cls.call_args
            assert call_kwargs.kwargs.get("perp_dexs") == ["", "xyz"]

# ── Other HIP-3 dexes (perp_dexs) ─────────────────────────────────────

PARA_META = {"universe": [{"name": "para:TOTAL2", "szDecimals": 2, "maxLeverage": 10}]}
PARA_CTXS = [{"funding": "0.00001", "openInterest": "100", "markPx": "1200.0"}]
METAS = {"": [DEFAULT_META, DEFAULT_CTXS], "xyz": [XYZ_META, XYZ_CTXS], "para": [PARA_META, PARA_CTXS]}
MIDS = {"": {"BTC": "85000.0"}, "xyz": {"xyz:TSLA": "380.0"}, "para": {"para:TOTAL2": "1200.0"}}


@pytest.fixture
def para_client():
    """A keyless client of the default dex, xyz and para, with the SDK's Info mocked."""
    HyperliquidClient._cached_market_specs = None
    with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
         patch('fractrade_hl_simple.hyperliquid.Exchange'):
        info = mock_info_cls.return_value
        info.post.side_effect = lambda endpoint, payload: METAS[payload.get("dex", "")]
        info.all_mids.side_effect = lambda dex="": MIDS[dex]
        info.frontend_open_orders.side_effect = lambda address, dex="": []
        client = HyperliquidClient(max_retries=0, trust_env=False, perp_dexs=["xyz", "para"])
        yield client, mock_info_cls


SDK_META = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}
SPOT_META = {"tokens": [], "universe": []}


def sdk_answers(listed, default_meta=SDK_META):
    """API.post for the real SDK Info: a network whose HIP-3 dexes are `listed`, each without
    markets."""
    def post(self, url_path, payload=None):
        kind = payload["type"]
        if kind == "spotMeta":
            return SPOT_META
        if kind == "perpDexs":
            return [None] + [{"name": name, "fullName": name.upper()} for name in listed]
        if kind == "meta":
            return default_meta if payload.get("dex", "") == "" else {"universe": []}
        raise AssertionError(f"unexpected request {payload}")
    return post


class TestPerpDexs:
    def test_the_sdk_gets_the_default_dex_and_the_dexes_asked_for(self, para_client):
        client, info_cls = para_client
        assert info_cls.call_args.kwargs["perp_dexs"] == ["", "xyz", "para"]
        assert client.market_specs["para:TOTAL2"]["size_decimals"] == 2  # specs of every dex

    def test_the_sdk_form_with_the_default_dex_is_taken_as_it_is(self):
        with patch('fractrade_hl_simple.hyperliquid.Info') as info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            info_cls.return_value.post.side_effect = lambda endpoint, payload: METAS[payload.get("dex", "")]
            client = HyperliquidClient(max_retries=0, trust_env=False, cache_market_specs=False,
                                       perp_dexs=["", "para", "para"])
        assert client.perp_dexs == ["", "para"]

    @pytest.mark.parametrize("perp_dexs", ["para", [None], {"para"}])
    def test_anything_but_a_list_of_names_is_refused(self, perp_dexs):
        # A bare string would otherwise be read as the dexes "p", "a", "r", "a".
        with pytest.raises(InvalidArgumentException, match="perp_dexs must be a list"):
            HyperliquidClient(trust_env=False, perp_dexs=perp_dexs)

    def test_extended_universe_and_perp_dexs_are_not_combined(self):
        with pytest.raises(InvalidArgumentException, match="not both"):
            HyperliquidClient(trust_env=False, extended_universe=True, perp_dexs=["para"])

    def test_a_dex_the_network_does_not_have_is_an_invalid_argument(self):
        # The real SDK fails with a bare KeyError('nope') while it builds its tables.
        with patch("hyperliquid.api.API.post", sdk_answers(listed=["xyz"])):
            with pytest.raises(InvalidArgumentException, match="'nope' is not a perp dex on mainnet") as caught:
                HyperliquidClient(trust_env=False, cache_market_specs=False, perp_dexs=["nope"])
        assert isinstance(caught.value.__cause__, KeyError)

    def test_a_dex_the_network_lacks_is_found_with_cached_metadata_too(self):
        HyperliquidClient._cached_key = (constants.MAINNET_API_URL, ("", "nope"))
        HyperliquidClient._cached_meta, HyperliquidClient._cached_spot_meta = SDK_META, SPOT_META
        HyperliquidClient._cached_market_specs, HyperliquidClient._cached_market_specs_at = {"BTC": {}}, time.time()
        try:
            with patch("hyperliquid.api.API.post", sdk_answers(listed=["xyz"])):
                with pytest.raises(InvalidArgumentException, match="'nope' is not a perp dex"):
                    HyperliquidClient(trust_env=False, perp_dexs=["nope"])
        finally:
            HyperliquidClient._cached_key = HyperliquidClient._cached_market_specs = None

    def test_a_metadata_key_error_is_not_taken_for_an_unknown_dex(self):
        # A dex named "name" exists; the default dex's metadata lacks a market's "name". The
        # SDK's KeyError('name') is a metadata problem and is raised as it is.
        broken = {"universe": [{"szDecimals": 5, "maxLeverage": 50}]}
        with patch("hyperliquid.api.API.post", sdk_answers(listed=["name"], default_meta=broken)):
            with pytest.raises(KeyError) as caught:
                HyperliquidClient(trust_env=False, cache_market_specs=False, perp_dexs=["name"])
        assert not isinstance(caught.value, InvalidArgumentException)

    def test_a_price_reads_only_the_dex_of_its_symbol(self, para_client):
        # As the SDK reads a market order's price: one request, not one per dex.
        client, _ = para_client
        client.info.all_mids.reset_mock()
        assert client.get_price("para:TOTAL2") == 1200.0
        assert client.get_price("BTC") == 85000.0
        assert [c.kwargs["dex"] for c in client.info.all_mids.call_args_list] == ["para", ""]
        assert set(client.get_price()) == {"BTC", "xyz:TSLA", "para:TOTAL2"}

    def test_a_symbol_of_a_dex_the_client_does_not_cover_names_perp_dexs(self, para_client):
        client, _ = para_client
        client.info.all_mids.reset_mock()
        with pytest.raises(UnknownSymbolException, match="perp_dexs including 'io'"):
            client.get_price("io:OAI")
        client.info.all_mids.assert_not_called()

    def test_open_orders_of_a_symbol_read_only_its_dex(self, para_client):
        client, _ = para_client
        address = "0x" + "1" * 40
        client.get_open_orders("xyz:TSLA", address=address)
        client.get_open_orders("io:OAI", address=address)  # not covered: nothing to read
        client.get_open_orders(address=address)
        assert [c.kwargs["dex"] for c in client.info.frontend_open_orders.call_args_list] == ["xyz", "", "xyz", "para"]

    def test_the_network_dexes_are_listed_without_the_default_dex(self, para_client):
        client, _ = para_client
        client.info.perp_dexs.return_value = [None, {"name": "xyz", "fullName": "XYZ", "deployer": "0x0"},
                                              {"name": "para", "fullName": "Paragon"}]
        assert client.get_perp_dexs() == [{"name": "xyz", "full_name": "XYZ"}, {"name": "para", "full_name": "Paragon"}]

    def test_cancel_all_orders_of_a_symbol_reads_only_its_dex(self, mock_client):
        mock_client.info.open_orders.side_effect = lambda address, dex="": (
            [{"coin": "xyz:TSLA", "oid": 7}] if dex == "xyz" else [{"coin": "BTC", "oid": 8}])
        mock_client._cancel_all = MagicMock()
        mock_client.cancel_all_orders("xyz:TSLA")
        assert [c.kwargs["dex"] for c in mock_client.info.open_orders.call_args_list] == ["xyz"]
        mock_client._cancel_all.assert_called_once_with([{"coin": "xyz:TSLA", "oid": 7}])


class TestGetLeverage:
    ADDRESS = "0x" + "ab" * 20

    def answer(self, client, leverage):
        client.info.name_to_coin = {"BTC": "BTC", "para:TOTAL2": "para:TOTAL2"}  # the SDK's markets (mocked Info)
        client.info.post.side_effect = lambda endpoint, payload: {"leverage": leverage} \
            if payload["type"] == "activeAssetData" else METAS[payload.get("dex", "")]

    def test_the_leverage_of_a_hip3_market(self, para_client):
        client, _ = para_client
        self.answer(client, {"type": "isolated", "value": 3})
        leverage = client.get_leverage("para:TOTAL2", address=self.ADDRESS)
        assert (leverage.type, leverage.value) == ("isolated", Decimal(3))
        client.info.post.assert_called_with(
            "/info", {"type": "activeAssetData", "user": self.ADDRESS, "coin": "para:TOTAL2"})

    def test_an_unknown_market_sends_no_request(self, para_client):
        client, _ = para_client
        self.answer(client, {"type": "cross", "value": 3})
        client.info.post.reset_mock()
        with pytest.raises(UnknownSymbolException):
            client.get_leverage("NOPE", address=self.ADDRESS)
        client.info.post.assert_not_called()

    @pytest.mark.parametrize("leverage", [None, {"type": "cross"}, {"type": "cross", "value": 0},
                                          {"type": "cross", "value": True}, {"type": "spot", "value": 3},
                                          {"type": "cross", "value": "NaN"}])
    def test_an_answer_in_no_documented_format_is_an_error_not_a_leverage(self, para_client, leverage):
        # A margin is the order's value divided by the leverage: 0 or a missing one must not pass.
        client, _ = para_client
        self.answer(client, leverage)
        with pytest.raises(HyperliquidException, match="activeAssetData for BTC"):
            client.get_leverage("BTC", address=self.ADDRESS)


class TestGetFeeRates:
    ADDRESS = "0x" + "ab" * 20

    def client(self, para_client, growth=False, scale=None, cross="0.00045", add="0.00015", discount="0.0"):
        client, _ = para_client
        client.info.name_to_coin = {"BTC": "BTC", "para:TOTAL2": "para:TOTAL2"}
        client.market_specs["para:TOTAL2"].update(growth_mode=growth, deployer_fee_scale=scale)
        fees = {"userCrossRate": cross, "userAddRate": add, "activeReferralDiscount": discount}
        client.info.post.side_effect = lambda endpoint, payload: fees \
            if payload["type"] == "userFees" else METAS[payload.get("dex", "")]
        return client

    def test_hyperliquids_own_perps_pay_the_wallets_rates(self, para_client):
        rates = self.client(para_client).get_fee_rates("BTC", address=self.ADDRESS)
        assert (rates.taker, rates.maker, rates.dex, rates.growth_mode) == (Decimal("0.00045"), Decimal("0.00015"),
                                                                            None, False)

    @pytest.mark.parametrize("growth, scale, taker", [
        (True, "1.0", "0.00009"),   # xyz:NVDA live: 0.045 % / 10 × (1 + 1) = 0.009 %, as its fills paid
        (False, "0.5", "0.000675"),  # the deployer adds half
        (False, "2", "0.0018"),     # above 1: the protocol's part raised to the deployer's, 2 × 2
    ])
    def test_a_hip3_market_is_scaled_by_growth_mode_and_the_deployers_fee_scale(self, para_client, growth, scale, taker):
        rates = self.client(para_client, growth, scale).get_fee_rates("para:TOTAL2", address=self.ADDRESS)
        assert rates.taker == Decimal(taker) and rates.dex == "para"

    @pytest.mark.parametrize("scale", [None, "x", "-1"])
    def test_a_hip3_market_without_a_fee_scale_is_an_error_not_a_lower_fee(self, para_client, scale):
        # Every HIP-3 market has one (2026-11); without it the deployer's share is unknown.
        with pytest.raises(HyperliquidException, match="fee rates are not established"):
            self.client(para_client, False, scale).get_fee_rates("para:TOTAL2", address=self.ADDRESS)

    def test_a_maker_rebate_is_only_cut_by_growth_mode(self, para_client):
        rates = self.client(para_client, True, "1.0", add="-0.00002", discount="0.04").get_fee_rates(
            "para:TOTAL2", address=self.ADDRESS)
        assert rates.maker == Decimal("-0.000002")  # not the referral discount either

    def test_the_referral_discount_is_taken_off_the_fees(self, para_client):
        # 4 % off (the fee formula of Hyperliquid's docs): 0.045 % becomes 0.0432 %. It was left out.
        rates = self.client(para_client, discount="0.04").get_fee_rates("BTC", address=self.ADDRESS)
        assert (rates.taker, rates.maker) == (Decimal("0.000432"), Decimal("0.000144"))

    @pytest.mark.parametrize("answer", [{}, {"userCrossRate": "x", "userAddRate": "0"},
                                        {"userCrossRate": "NaN", "userAddRate": "0"}])
    def test_an_answer_in_no_documented_format_is_an_error(self, para_client, answer):
        client = self.client(para_client)
        client.info.post.side_effect = lambda endpoint, payload: answer
        with pytest.raises(HyperliquidException, match="userFees"):
            client.get_fee_rates("BTC", address=self.ADDRESS)

    def test_the_market_specs_carry_growth_mode_and_the_fee_scale(self):
        meta = {"universe": [{"name": "para:TOTAL2", "szDecimals": 2, "maxLeverage": 10, "growthMode": "enabled",
                              "deployerFeeScale": "1.0"}]}
        metas = {**METAS, "para": [meta, PARA_CTXS]}
        with patch('fractrade_hl_simple.hyperliquid.Info') as info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            info_cls.return_value.post.side_effect = lambda endpoint, payload: metas[payload.get("dex", "")]
            client = HyperliquidClient(max_retries=0, trust_env=False, cache_market_specs=False, perp_dexs=["para"])
        spec = client.market_specs["para:TOTAL2"]
        assert (spec["growth_mode"], spec["deployer_fee_scale"]) == (True, "1.0")
        assert client.market_specs["BTC"]["growth_mode"] is False
