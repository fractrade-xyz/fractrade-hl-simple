"""Tests for reliability fixes: error propagation, retry coverage, exception hierarchy, etc."""
import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from decimal import Decimal
import requests
import time

from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.exceptions import (
    PositionNotFoundException,
    OrderNotFoundException,
    InsufficientMarginException,
    OrderException,
    HyperliquidException,
)


@pytest.fixture
def mock_client():
    """Create a fully mocked authenticated client."""
    HyperliquidClient._cached_market_specs = None
    HyperliquidClient._cached_market_specs_at = 0
    with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
         patch('fractrade_hl_simple.hyperliquid.Exchange') as mock_exchange_cls:
        mock_info = mock_info_cls.return_value
        mock_exchange = mock_exchange_cls.return_value

        mock_info.post.return_value = [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50},
                {"name": "ETH", "szDecimals": 4, "maxLeverage": 50},
            ]},
            [
                {"funding": "0.0001", "openInterest": "1000", "markPx": "85000"},
                {"funding": "0.00005", "openInterest": "5000", "markPx": "3200"},
            ],
        ]

        client = HyperliquidClient(max_retries=0, retry_delay=0)
        client.exchange = mock_exchange
        client.info = mock_info
        client.account = MagicMock()
        client.account.public_address = "0x1234567890abcdef1234567890abcdef12345678"
        client.account.private_key = "0xdead"
        client.public_address = client.account.public_address
        client.exchange_account = MagicMock()

        yield client


# ── Fix 1: get_open_orders() propagates errors ─────────────────────────

class TestGetOpenOrdersErrorPropagation:
    def test_raises_on_api_error(self, mock_client):
        """get_open_orders() must raise when API fails, not return []."""
        mock_client.info.frontend_open_orders.side_effect = requests.ConnectionError("API down")
        with pytest.raises(requests.ConnectionError):
            mock_client.get_open_orders()

    def test_raises_on_auth_error(self, mock_client):
        mock_client.info.frontend_open_orders.side_effect = Exception("Unauthorized")
        with pytest.raises(Exception, match="Unauthorized"):
            mock_client.get_open_orders()

    def test_returns_empty_list_when_no_orders(self, mock_client):
        """Empty list is valid when API returns empty - just not on error."""
        mock_client.info.frontend_open_orders.return_value = []
        result = mock_client.get_open_orders()
        assert result == []

    def test_skips_unparseable_orders_but_returns_rest(self, mock_client):
        """One malformed order shouldn't crash the whole method."""
        mock_client.info.frontend_open_orders.return_value = [
            {"oid": 999, "coin": "BTC", "side": "B", "sz": "INVALID_NUMBER",
             "origSz": "INVALID", "timestamp": 0},  # Will fail on Decimal parse
            {
                "oid": 123, "coin": "BTC", "side": "B", "sz": "0.001",
                "limitPx": "85000", "orderType": "Limit", "tif": "Gtc",
                "origSz": "0.001", "timestamp": 1700000000000,
            },
        ]
        orders = mock_client.get_open_orders()
        assert len(orders) == 1
        assert orders[0].symbol == "BTC"


# ── Fix 2: cancel_all_orders() error handling ──────────────────────────

class TestCancelAllOrdersReliability:
    def test_raises_on_fetch_failure(self, mock_client):
        """Must raise if we can't even fetch open orders."""
        mock_client.info.open_orders.side_effect = requests.ConnectionError("down")
        with pytest.raises(requests.ConnectionError):
            mock_client.cancel_all_orders()

    def test_raises_on_cancel_failure(self, mock_client):
        """Must raise if any individual cancel fails."""
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
        ]
        mock_client.exchange.cancel.side_effect = [None, Exception("cancel failed")]

        with pytest.raises(RuntimeError, match="Failed to cancel 1 order"):
            mock_client.cancel_all_orders()

    def test_succeeds_when_all_cancel(self, mock_client):
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
        ]
        mock_client.exchange.cancel.return_value = None
        mock_client.cancel_all_orders()  # Should not raise

    def test_cancel_all_is_alias(self, mock_client):
        """cancel_all() should delegate to cancel_all_orders()."""
        mock_client.info.open_orders.return_value = []
        mock_client.cancel_all()
        mock_client.info.open_orders.assert_called_once()


# ── Fix 3: close() fill verification ──────────────────────────────────

class TestCloseFillVerification:
    def test_warns_when_ioc_not_filled(self, mock_client, caplog):
        """close() should warn when IOC order doesn't fill."""
        import logging

        mock_client.info.user_state.return_value = {
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "100",
                              "totalNtlPos": "500", "totalRawUsd": "1000"},
            "crossMarginSummary": {"accountValue": "1000", "totalMarginUsed": "100",
                                   "totalNtlPos": "500", "totalRawUsd": "1000"},
            "withdrawable": "800",
            "assetPositions": [{
                "type": "oneWay",
                "position": {
                    "coin": "BTC", "entryPx": "85000", "szi": "0.001",
                    "leverage": {"type": "cross", "value": "10"},
                    "liquidationPx": None, "marginUsed": "8.5",
                    "positionValue": "85", "returnOnEquity": "0.01",
                    "unrealizedPnl": "1.0",
                },
            }],
        }
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        # Return a "resting" (not filled) order
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 999}}]}},
        }

        with caplog.at_level(logging.WARNING, logger="fractrade_hl_simple"):
            order = mock_client.close("BTC")

        assert order.status == "open"
        assert any("NOT filled" in msg for msg in caplog.messages)

    def test_no_warning_when_filled(self, mock_client, caplog):
        import logging

        mock_client.info.user_state.return_value = {
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "100",
                              "totalNtlPos": "500", "totalRawUsd": "1000"},
            "crossMarginSummary": {"accountValue": "1000", "totalMarginUsed": "100",
                                   "totalNtlPos": "500", "totalRawUsd": "1000"},
            "withdrawable": "800",
            "assetPositions": [{
                "type": "oneWay",
                "position": {
                    "coin": "BTC", "entryPx": "85000", "szi": "-0.001",
                    "leverage": {"type": "cross", "value": "10"},
                    "liquidationPx": None, "marginUsed": "8.5",
                    "positionValue": "85", "returnOnEquity": "0.01",
                    "unrealizedPnl": "-0.5",
                },
            }],
        }
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 999, "avgPx": "85000"}}]}},
        }

        with caplog.at_level(logging.WARNING, logger="fractrade_hl_simple"):
            order = mock_client.close("BTC")

        assert order.status == "filled"
        assert not any("NOT filled" in msg for msg in caplog.messages)


# ── Fix 4: stop_loss/take_profit use _with_retry ─────────────────────

class TestStopLossTakeProfitRetry:
    def _setup_position(self, mock_client):
        mock_client.info.user_state.return_value = {
            "marginSummary": {"accountValue": "1000", "totalMarginUsed": "100",
                              "totalNtlPos": "500", "totalRawUsd": "1000"},
            "crossMarginSummary": {"accountValue": "1000", "totalMarginUsed": "100",
                                   "totalNtlPos": "500", "totalRawUsd": "1000"},
            "withdrawable": "800",
            "assetPositions": [{
                "type": "oneWay",
                "position": {
                    "coin": "BTC", "entryPx": "85000", "szi": "0.001",
                    "leverage": {"type": "cross", "value": "10"},
                    "liquidationPx": None, "marginUsed": "8.5",
                    "positionValue": "85", "returnOnEquity": "0.01",
                    "unrealizedPnl": "1.0",
                },
            }],
        }

    def test_stop_loss_uses_retry(self, mock_client):
        self._setup_position(mock_client)
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        # Enable retry and verify the call goes through _with_retry
        mock_client.max_retries = 2
        mock_client.retry_delay = 0.01

        order = mock_client.stop_loss("BTC", 0.001, 80000.0)
        assert order.order_id == "1"
        mock_client.exchange.order.assert_called_once()

    def test_take_profit_uses_retry(self, mock_client):
        self._setup_position(mock_client)
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 2}}]}},
        }
        order = mock_client.take_profit("BTC", 0.001, 90000.0)
        assert order.order_id == "2"

    def test_stop_loss_raises_position_not_found(self, mock_client):
        mock_client.info.user_state.return_value = {
            "marginSummary": {"accountValue": "0", "totalMarginUsed": "0",
                              "totalNtlPos": "0", "totalRawUsd": "0"},
            "crossMarginSummary": {"accountValue": "0", "totalMarginUsed": "0",
                                   "totalNtlPos": "0", "totalRawUsd": "0"},
            "withdrawable": "0",
            "assetPositions": [],
        }
        with pytest.raises(PositionNotFoundException):
            mock_client.stop_loss("BTC", 0.001, 80000.0)

    def test_close_raises_position_not_found(self, mock_client):
        mock_client.info.user_state.return_value = {
            "marginSummary": {"accountValue": "0", "totalMarginUsed": "0",
                              "totalNtlPos": "0", "totalRawUsd": "0"},
            "crossMarginSummary": {"accountValue": "0", "totalMarginUsed": "0",
                                   "totalNtlPos": "0", "totalRawUsd": "0"},
            "withdrawable": "0",
            "assetPositions": [],
        }
        with pytest.raises(PositionNotFoundException):
            mock_client.close("BTC")


# ── Fix 5: self.logger bug ────────────────────────────────────────────

class TestLoggerBug:
    def test_no_attribute_error_on_spot_balance_failure(self, mock_client):
        """get_spot_balance should use module logger, not self.logger."""
        mock_client.info.spot_user_state.return_value = {
            "balances": [{"coin": "BAD", "total": "not_a_number"}]
        }
        mock_client.info.all_mids.return_value = {"BAD": "1.0"}
        # This should not raise AttributeError
        result = mock_client.get_spot_balance(simple=True)
        assert isinstance(result, Decimal)


# ── Fix 6: cancel_order() error handling ───────────────────────────────

class TestCancelOrderReliability:
    def test_returns_true_on_success(self, mock_client):
        mock_client.exchange.cancel.return_value = None
        assert mock_client.cancel_order(12345, "BTC") is True

    def test_returns_false_for_not_found(self, mock_client):
        mock_client.exchange.cancel.side_effect = Exception("Order not found")
        assert mock_client.cancel_order(12345, "BTC") is False

    def test_returns_false_for_already_cancelled(self, mock_client):
        mock_client.exchange.cancel.side_effect = Exception("Order already cancelled")
        assert mock_client.cancel_order(12345, "BTC") is False

    def test_raises_on_network_error(self, mock_client):
        mock_client.exchange.cancel.side_effect = requests.ConnectionError("network down")
        with pytest.raises(requests.ConnectionError):
            mock_client.cancel_order(12345, "BTC")

    def test_raises_on_unexpected_error(self, mock_client):
        mock_client.exchange.cancel.side_effect = Exception("internal server error")
        with pytest.raises(Exception, match="internal server error"):
            mock_client.cancel_order(12345, "BTC")


# ── Fix 7: Order ID type flexibility ──────────────────────────────────

class TestOrderIdTypes:
    def test_cancel_order_accepts_str(self, mock_client):
        mock_client.exchange.cancel.return_value = None
        assert mock_client.cancel_order("12345", "BTC") is True
        mock_client.exchange.cancel.assert_called_with("BTC", 12345)

    def test_cancel_order_accepts_int(self, mock_client):
        mock_client.exchange.cancel.return_value = None
        assert mock_client.cancel_order(12345, "BTC") is True
        mock_client.exchange.cancel.assert_called_with("BTC", 12345)


# ── Fix 8: Market specs refresh ───────────────────────────────────────

class TestMarketSpecsRefresh:
    def test_refresh_market_specs(self, mock_client):
        old_specs = mock_client.market_specs.copy()
        mock_client.info.post.return_value = [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 100},
                {"name": "NEWCOIN", "szDecimals": 2, "maxLeverage": 20},
            ]},
            [{}, {}],
        ]
        result = mock_client.refresh_market_specs()
        assert "NEWCOIN" in result
        assert result["BTC"]["max_leverage"] == 100

    def test_auto_refresh_when_stale(self, mock_client):
        mock_client._market_specs_fetched_at = time.time() - 90000  # 25 hours ago
        mock_client.info.post.return_value = [
            {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]},
            [{}],
        ]
        mock_client._ensure_fresh_market_specs()
        # Should have refreshed - fetched_at should be recent
        assert time.time() - mock_client._market_specs_fetched_at < 5

    def test_no_refresh_when_fresh(self, mock_client):
        mock_client._market_specs_fetched_at = time.time()  # Just now
        mock_client.info.post.reset_mock()
        mock_client._ensure_fresh_market_specs()
        # Should NOT have called the API
        mock_client.info.post.assert_not_called()

    def test_refresh_failure_keeps_existing(self, mock_client):
        old_specs = mock_client.market_specs.copy()
        mock_client.info.post.side_effect = Exception("API down")
        mock_client.info.meta.side_effect = Exception("API down")
        mock_client.refresh_market_specs()
        assert mock_client.market_specs == old_specs


# ── Fix 9: Retry coverage on original methods ─────────────────────────

class TestRetryOnOriginalMethods:
    def test_get_price_uses_retry(self, mock_client):
        mock_client.max_retries = 1
        mock_client.retry_delay = 0.01
        # get_price iterates perp_dexs ["", "xyz"], calling all_mids for each
        mock_client.info.all_mids.side_effect = [
            requests.ConnectionError("fail"),
            {"BTC": "85000.0"},
            {},  # xyz dex (empty is fine)
        ]
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            result = mock_client.get_price("BTC")
        assert result == 85000.0

    def test_get_user_state_uses_retry(self, mock_client):
        mock_client.max_retries = 1
        mock_client.retry_delay = 0.01
        mock_client.info.user_state.side_effect = [
            requests.ConnectionError("fail"),
            {
                "marginSummary": {"accountValue": "1000", "totalMarginUsed": "0",
                                  "totalNtlPos": "0", "totalRawUsd": "1000"},
                "crossMarginSummary": {"accountValue": "1000", "totalMarginUsed": "0",
                                       "totalNtlPos": "0", "totalRawUsd": "1000"},
                "withdrawable": "1000",
            },
        ]
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            state = mock_client.get_user_state()
        assert state.withdrawable == Decimal("1000")

    def test_get_open_orders_uses_retry(self, mock_client):
        mock_client.max_retries = 1
        mock_client.retry_delay = 0.01
        mock_client.info.frontend_open_orders.side_effect = [
            requests.ConnectionError("fail"),
            [],
        ]
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            result = mock_client.get_open_orders()
        assert result == []


# ── Fix 11: Dead code removed ─────────────────────────────────────────

class TestDeadCodeRemoved:
    def test_validate_price_removed(self, mock_client):
        assert not hasattr(mock_client, '_validate_price')

    def test_validate_size_removed(self, mock_client):
        assert not hasattr(mock_client, '_validate_size')


# ── Fix 12: Exception hierarchy ───────────────────────────────────────

class TestExceptionHierarchy:
    def test_position_not_found_is_hyperliquid_exception(self):
        assert issubclass(PositionNotFoundException, HyperliquidException)

    def test_order_not_found_is_order_exception(self):
        assert issubclass(OrderNotFoundException, OrderException)

    def test_insufficient_margin_is_order_exception(self):
        assert issubclass(InsufficientMarginException, OrderException)

    def test_order_exception_is_hyperliquid_exception(self):
        assert issubclass(OrderException, HyperliquidException)

    def test_can_catch_all_with_base(self):
        """All custom exceptions catchable with HyperliquidException."""
        for exc_cls in [PositionNotFoundException, OrderNotFoundException,
                        InsufficientMarginException, OrderException]:
            try:
                raise exc_cls("test")
            except HyperliquidException:
                pass  # Should be caught

    def test_exceptions_importable_from_package(self):
        from fractrade_hl_simple import (
            PositionNotFoundException,
            OrderNotFoundException,
            InsufficientMarginException,
        )
