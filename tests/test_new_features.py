"""Tests for new features: leverage, order tracking, bulk orders, slippage, funding history, portfolio."""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from decimal import Decimal
from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.models import Fill


@pytest.fixture
def mock_client():
    """Create a fully mocked authenticated client for unit tests."""
    HyperliquidClient._cached_market_specs = None
    HyperliquidClient._cached_market_specs_at = 0
    with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
         patch('fractrade_hl_simple.hyperliquid.Exchange') as mock_exchange_cls:
        mock_info = mock_info_cls.return_value
        mock_exchange = mock_exchange_cls.return_value

        # Mock info.post for market specs initialization (_fetch_market_specs uses info.post)
        mock_info.post.return_value = [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "onlyIsolated": False},
                {"name": "ETH", "szDecimals": 4, "maxLeverage": 50, "onlyIsolated": False},
            ]},
            [
                {"funding": "0.0001", "openInterest": "1000", "markPx": "85000"},
                {"funding": "0.00005", "openInterest": "5000", "markPx": "3200"},
            ],
        ]

        client = HyperliquidClient(max_retries=0, retry_delay=0)

        # Mock the exchange and info objects on the client
        client.exchange = mock_exchange
        client.info = mock_info

        # Set up auth
        client.account = MagicMock()
        client.account.public_address = "0x1234567890abcdef1234567890abcdef12345678"
        client.account.private_key = "0xdead"
        client.public_address = client.account.public_address
        client.exchange_account = MagicMock()

        yield client


# ── Configurable Slippage ──────────────────────────────────────────────

class TestConfigurableSlippage:
    def test_default_slippage_init(self):
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient(default_slippage=0.02)
        assert client.default_slippage == 0.02

    def test_invalid_slippage_init(self):
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            with pytest.raises(ValueError, match="default_slippage"):
                HyperliquidClient(default_slippage=0.0)
            with pytest.raises(ValueError, match="default_slippage"):
                HyperliquidClient(default_slippage=0.6)

    def test_slippage_applied_to_market_buy(self, mock_client):
        mock_client.default_slippage = 0.02
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "86700"}}]}},
        }

        mock_client.buy("BTC", 0.001)

        # Verify the limit_px passed to exchange.order uses 2% slippage
        call_kwargs = mock_client.exchange.order.call_args
        limit_px = call_kwargs.kwargs.get("limit_px") or call_kwargs[1].get("limit_px")
        if limit_px is None:
            limit_px = call_kwargs[0][3]  # positional arg
        # 85000 * 1.02 = 86700
        assert 86600 < limit_px < 86800

    def test_slippage_override_per_call(self, mock_client):
        mock_client.default_slippage = 0.05
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85850"}}]}},
        }

        mock_client.buy("BTC", 0.001, slippage=0.01)

        call_kwargs = mock_client.exchange.order.call_args
        limit_px = call_kwargs.kwargs.get("limit_px") or call_kwargs[1].get("limit_px")
        if limit_px is None:
            limit_px = call_kwargs[0][3]
        # 85000 * 1.01 = 85850
        assert 85800 < limit_px < 85900

    def test_invalid_slippage_per_call(self, mock_client):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        with pytest.raises(ValueError, match="slippage"):
            mock_client.buy("BTC", 0.001, slippage=0.0)
        with pytest.raises(ValueError, match="slippage"):
            mock_client.buy("BTC", 0.001, slippage=0.6)


# ── Leverage Management ────────────────────────────────────────────────

class TestLeverageManagement:
    def test_set_leverage_cross(self, mock_client):
        mock_client.exchange.update_leverage.return_value = {"status": "ok"}
        result = mock_client.set_leverage("BTC", 10)
        mock_client.exchange.update_leverage.assert_called_once_with(10, "BTC", True)
        assert result == {"status": "ok"}

    def test_set_leverage_isolated(self, mock_client):
        mock_client.exchange.update_leverage.return_value = {"status": "ok"}
        result = mock_client.set_leverage("ETH", 5, is_cross=False)
        mock_client.exchange.update_leverage.assert_called_once_with(5, "ETH", False)
        assert result == {"status": "ok"}

    def test_set_leverage_invalid_value(self, mock_client):
        with pytest.raises(ValueError, match="Leverage must be >= 1"):
            mock_client.set_leverage("BTC", 0)

    def test_set_leverage_exceeds_max(self, mock_client):
        with pytest.raises(ValueError, match="Max leverage"):
            mock_client.set_leverage("BTC", 100)

    def test_set_leverage_requires_auth(self):
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
            client.market_specs = {}
        with pytest.raises(RuntimeError):
            client.set_leverage("BTC", 10)

    def test_add_isolated_margin(self, mock_client):
        mock_client.exchange.update_isolated_margin.return_value = {"status": "ok"}
        result = mock_client.add_isolated_margin("ETH", 100.0)
        mock_client.exchange.update_isolated_margin.assert_called_once_with(100.0, "ETH")
        assert result == {"status": "ok"}


# ── Order Tracking (Fills) ─────────────────────────────────────────────

SAMPLE_FILLS = [
    {
        "coin": "BTC",
        "side": "B",
        "px": "85000.5",
        "sz": "0.001",
        "closedPnl": "0",
        "dir": "Open Long",
        "oid": 12345,
        "crossed": True,
        "time": 1700000000000,
        "hash": "0xabc123",
        "fee": "0.085",
    },
    {
        "coin": "ETH",
        "side": "A",
        "px": "3200.0",
        "sz": "0.1",
        "closedPnl": "10.5",
        "dir": "Close Short",
        "oid": 67890,
        "crossed": False,
        "time": 1700000001000,
        "hash": "0xdef456",
        "fee": "0.032",
    },
]


class TestOrderTracking:
    def test_get_fills_all(self, mock_client):
        mock_client.info.user_fills.return_value = SAMPLE_FILLS
        fills = mock_client.get_fills()
        assert len(fills) == 2
        assert all(isinstance(f, Fill) for f in fills)
        assert fills[0].symbol == "BTC"
        assert fills[0].price == Decimal("85000.5")
        assert fills[0].order_id == 12345
        assert fills[0].crossed is True
        assert fills[0].fee == Decimal("0.085")

    def test_get_fills_filtered(self, mock_client):
        mock_client.info.user_fills.return_value = SAMPLE_FILLS
        fills = mock_client.get_fills("ETH")
        assert len(fills) == 1
        assert fills[0].symbol == "ETH"
        assert fills[0].direction == "Close Short"

    def test_get_fills_by_time(self, mock_client):
        mock_client.info.user_fills_by_time.return_value = [SAMPLE_FILLS[0]]
        fills = mock_client.get_fills_by_time(start_time=1700000000000)
        assert len(fills) == 1
        assert fills[0].symbol == "BTC"
        mock_client.info.user_fills_by_time.assert_called_once_with(
            mock_client.public_address, 1700000000000, None
        )

    def test_get_fills_by_time_with_end(self, mock_client):
        mock_client.info.user_fills_by_time.return_value = SAMPLE_FILLS
        fills = mock_client.get_fills_by_time(
            start_time=1700000000000, end_time=1700000002000, symbol="BTC"
        )
        assert len(fills) == 1

    def test_get_order_status(self, mock_client):
        mock_client.info.query_order_by_oid.return_value = {"status": "filled", "oid": 12345}
        result = mock_client.get_order_status(12345)
        assert result["status"] == "filled"
        mock_client.info.query_order_by_oid.assert_called_once_with(
            mock_client.public_address, 12345
        )

    def test_get_fills_requires_auth(self):
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
        with pytest.raises(RuntimeError):
            client.get_fills()

    def test_fill_without_fee(self, mock_client):
        fill_no_fee = {
            "coin": "BTC", "side": "B", "px": "85000", "sz": "0.001",
            "closedPnl": "0", "dir": "Open Long", "oid": 111,
            "crossed": True, "time": 1700000000000, "hash": "0x111",
        }
        mock_client.info.user_fills.return_value = [fill_no_fee]
        fills = mock_client.get_fills()
        assert fills[0].fee is None


# ── Bulk Orders ────────────────────────────────────────────────────────

class TestBulkOrders:
    def test_bulk_order(self, mock_client):
        mock_client.exchange.bulk_orders.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [
                {"resting": {"oid": 1}},
                {"resting": {"oid": 2}},
            ]}},
        }
        orders = [
            {"symbol": "BTC", "is_buy": True, "size": 0.001, "limit_price": 80000.0},
            {"symbol": "ETH", "is_buy": False, "size": 0.01, "limit_price": 3500.0},
        ]
        result = mock_client.bulk_order(orders)
        assert result["status"] == "ok"
        mock_client.exchange.bulk_orders.assert_called_once()

        # Verify order requests were formatted correctly
        call_args = mock_client.exchange.bulk_orders.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[0]["coin"] == "BTC"
        assert call_args[0]["is_buy"] is True
        assert call_args[1]["coin"] == "ETH"
        assert call_args[1]["is_buy"] is False

    def test_bulk_cancel(self, mock_client):
        mock_client.exchange.bulk_cancel.return_value = {"status": "ok"}
        cancels = [
            {"symbol": "BTC", "order_id": "12345"},
            {"symbol": "ETH", "order_id": 67890},
        ]
        result = mock_client.bulk_cancel(cancels)
        assert result["status"] == "ok"
        mock_client.exchange.bulk_cancel.assert_called_once_with([
            {"coin": "BTC", "oid": 12345},
            {"coin": "ETH", "oid": 67890},
        ])

    def test_bulk_order_requires_auth(self):
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
        with pytest.raises(RuntimeError):
            client.bulk_order([])

    def test_bulk_order_validates_orders(self, mock_client):
        with pytest.raises((ValueError, KeyError)):
            mock_client.bulk_order([{"symbol": "BTC"}])  # missing required fields


# ── Funding History ────────────────────────────────────────────────────

class TestFundingHistory:
    def test_get_funding_history(self, mock_client):
        mock_client.info.funding_history.return_value = [
            {"time": 1700000000000, "fundingRate": "0.0001", "premium": "0.00005"},
            {"time": 1700003600000, "fundingRate": "-0.00005", "premium": "-0.00002"},
        ]
        result = mock_client.get_funding_history("BTC", start_time=1700000000000)
        assert len(result) == 2
        assert result[0]["funding_rate"] == 0.0001
        assert result[0]["time"] == 1700000000000
        assert result[1]["premium"] == -0.00002
        mock_client.info.funding_history.assert_called_once_with("BTC", 1700000000000, None)

    def test_get_funding_history_with_end_time(self, mock_client):
        mock_client.info.funding_history.return_value = []
        mock_client.get_funding_history("ETH", 1700000000000, 1700100000000)
        mock_client.info.funding_history.assert_called_once_with(
            "ETH", 1700000000000, 1700100000000
        )


# ── Funding Rate URL Fix ──────────────────────────────────────────────

class TestFundingRatesFix:
    def test_uses_base_url(self, mock_client):
        """Verify get_funding_rates uses self.base_url instead of hardcoded URL."""
        mock_client.base_url = "https://api.hyperliquid-testnet.xyz"
        mock_client.info.meta.return_value = {"universe": [{"name": "BTC"}]}

        with patch('fractrade_hl_simple.hyperliquid.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = [
                ["BTC", [["HlPerp", {"fundingRate": "0.0001"}]]]
            ]
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            mock_client.get_funding_rates()

            # Verify it used the testnet URL
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            assert "testnet" in call_url


# ── Meta + Asset Context ──────────────────────────────────────────────

class TestMetaAssetContext:
    def test_fetch_market_specs_uses_meta_and_asset_ctxs(self):
        # Clear class-level cache to ensure fresh fetch
        HyperliquidClient._cached_market_specs = None
        HyperliquidClient._cached_market_specs_at = 0
        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info = mock_info_cls.return_value
            mock_info.post.return_value = [
                {"universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "onlyIsolated": False},
                ]},
                [{"funding": "0.0001", "openInterest": "1000", "markPx": "85000"}],
            ]
            client = HyperliquidClient(max_retries=0, cache_market_specs=False)
            assert "BTC" in client.market_specs
            assert client.market_specs["BTC"]["size_decimals"] == 5
            assert client.market_specs["BTC"]["max_leverage"] == 50
            assert client.market_specs["BTC"]["funding"] == "0.0001"
            assert client.market_specs["BTC"]["mark_price"] == "85000"

    def test_fallback_to_meta_on_error(self):
        HyperliquidClient._cached_market_specs = None
        HyperliquidClient._cached_market_specs_at = 0
        with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info = mock_info_cls.return_value
            mock_info.post.side_effect = Exception("not available")
            mock_info.meta.return_value = {
                "universe": [{"name": "BTC", "szDecimals": 5}]
            }
            client = HyperliquidClient(max_retries=0, cache_market_specs=False)
            assert "BTC" in client.market_specs
            assert client.market_specs["BTC"]["size_decimals"] == 5


# ── Portfolio ──────────────────────────────────────────────────────────

class TestPortfolio:
    def test_get_portfolio(self, mock_client):
        with patch('fractrade_hl_simple.hyperliquid.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"pnl": "1000.0", "volume": "50000.0"}
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            result = mock_client.get_portfolio()
            assert result["pnl"] == "1000.0"

            call_kwargs = mock_post.call_args
            assert call_kwargs.kwargs["json"]["type"] == "portfolio"
            assert call_kwargs.kwargs["json"]["user"] == mock_client.public_address

    def test_get_portfolio_with_address(self, mock_client):
        with patch('fractrade_hl_simple.hyperliquid.requests.post') as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {}
            mock_response.raise_for_status.return_value = None
            mock_post.return_value = mock_response

            mock_client.get_portfolio("0xother_address")
            call_kwargs = mock_post.call_args
            assert call_kwargs.kwargs["json"]["user"] == "0xother_address"

    def test_get_portfolio_requires_auth_or_address(self):
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
            client.base_url = "https://api.hyperliquid.xyz"
        with pytest.raises(RuntimeError):
            client.get_portfolio()
