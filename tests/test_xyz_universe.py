"""Tests for extended perp universe (xyz: symbols) support."""
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal
from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.models import MARKET_SPECS


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

    def test_fallback_market_specs_include_xyz(self):
        xyz_symbols = [k for k in MARKET_SPECS if k.startswith("xyz:")]
        assert len(xyz_symbols) > 0
        assert "xyz:TSLA" in MARKET_SPECS
        assert "xyz:GOLD" in MARKET_SPECS
        assert "xyz:NVDA" in MARKET_SPECS
        assert "xyz:SP500" in MARKET_SPECS
        assert MARKET_SPECS["xyz:TSLA"]["size_decimals"] == 3

    def test_xyz_dex_failure_doesnt_break_init(self):
        """If the xyz dex meta fetch fails, standard symbols should still load."""
        HyperliquidClient._cached_market_specs = None
        HyperliquidClient._cached_market_specs_at = 0

        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info = mock_info_cls.return_value

            call_count = [0]
            def mock_post(endpoint, payload):
                if payload.get("type") == "metaAndAssetCtxs":
                    dex = payload.get("dex", "")
                    if dex == "xyz":
                        raise Exception("xyz dex unavailable")
                    return [DEFAULT_META, DEFAULT_CTXS]
                return {}

            def mock_meta(dex=""):
                if dex == "xyz":
                    raise Exception("xyz dex unavailable")
                return DEFAULT_META

            mock_info.post.side_effect = mock_post
            mock_info.meta.side_effect = mock_meta

            client = HyperliquidClient(max_retries=0, cache_market_specs=False)
            assert "BTC" in client.market_specs
            assert "xyz:TSLA" not in client.market_specs


# ── Price Queries ─────────────────────────────────────────────────────

class TestXyzPrices:
    def test_get_price_xyz_symbol(self, mock_client):
        mock_client.info.all_mids.side_effect = lambda dex="": (
            {"xyz:TSLA": "380.0", "xyz:GOLD": "4430.0"} if dex == "xyz"
            else {"BTC": "85000.0", "ETH": "3200.0"}
        )

        price = mock_client.get_price("xyz:TSLA")
        assert price == 380.0

    def test_get_price_all_includes_xyz(self, mock_client):
        mock_client.info.all_mids.side_effect = lambda dex="": (
            {"xyz:TSLA": "380.0", "xyz:GOLD": "4430.0"} if dex == "xyz"
            else {"BTC": "85000.0", "ETH": "3200.0"}
        )

        prices = mock_client.get_price()
        assert "BTC" in prices
        assert "xyz:TSLA" in prices
        assert "xyz:GOLD" in prices
        assert len(prices) == 4

    def test_get_price_xyz_not_found(self, mock_client):
        mock_client.info.all_mids.side_effect = lambda dex="": (
            {"xyz:TSLA": "380.0"} if dex == "xyz"
            else {"BTC": "85000.0"}
        )

        with pytest.raises(ValueError, match="xyz:FAKE not found"):
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
        with pytest.raises(ValueError, match="not found"):
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

    def test_exchange_gets_extended_universe(self):
        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange') as mock_exchange_cls:
            mock_info_cls.return_value.post.return_value = [DEFAULT_META, DEFAULT_CTXS]

            from fractrade_hl_simple.models import HyperliquidAccount
            account = HyperliquidAccount(
                private_key="0x" + "ab" * 32,
                public_address="0x1234567890abcdef1234567890abcdef12345678",
            )
            HyperliquidClient(account=account, max_retries=0, extended_universe=True)
            call_kwargs = mock_exchange_cls.call_args
            assert call_kwargs.kwargs.get("perp_dexs") == ["", "xyz"]
