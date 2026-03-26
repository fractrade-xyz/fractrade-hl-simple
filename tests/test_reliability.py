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
        """Must raise if bulk cancel fails."""
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
        ]
        mock_client.exchange.bulk_cancel.side_effect = Exception("cancel failed")

        with pytest.raises(Exception, match="cancel failed"):
            mock_client.cancel_all_orders()

    def test_succeeds_when_all_cancel(self, mock_client):
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
        ]
        mock_client.exchange.bulk_cancel.return_value = None
        mock_client.cancel_all_orders()  # Should not raise

    def test_uses_bulk_cancel(self, mock_client):
        """cancel_all_orders should use a single bulk_cancel call."""
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
            {"coin": "SOL", "oid": 3},
        ]
        mock_client.exchange.bulk_cancel.return_value = None
        mock_client.cancel_all_orders()
        mock_client.exchange.bulk_cancel.assert_called_once_with([
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
            {"coin": "SOL", "oid": 3},
        ])

    def test_no_api_call_when_no_orders(self, mock_client):
        """Should not call bulk_cancel when there are no orders."""
        mock_client.info.open_orders.return_value = []
        mock_client.cancel_all_orders()
        mock_client.exchange.bulk_cancel.assert_not_called()

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


# ── get_spot_balance prices parameter ────────────────────────────────

class TestGetSpotBalancePrices:
    def test_uses_provided_prices(self, mock_client):
        """get_spot_balance should use provided prices instead of calling get_price()."""
        mock_client.info.spot_user_state.return_value = {
            "balances": [
                {"coin": "HYPE", "total": "10", "hold": "0", "entryNtl": "0"},
            ]
        }
        external_prices = {"HYPE": 40.0}
        result = mock_client.get_spot_balance(simple=True, prices=external_prices)
        assert result == Decimal("400")
        # all_mids should NOT have been called
        mock_client.info.all_mids.assert_not_called()

    def test_fetches_prices_when_not_provided(self, mock_client):
        """get_spot_balance should fetch prices when none provided."""
        mock_client.info.spot_user_state.return_value = {
            "balances": [
                {"coin": "HYPE", "total": "10", "hold": "0", "entryNtl": "0"},
            ]
        }
        mock_client.info.all_mids.return_value = {"HYPE": "40.0"}
        result = mock_client.get_spot_balance(simple=True)
        assert result == Decimal("400")
        mock_client.info.all_mids.assert_called()

    def test_stablecoin_price_defaults_to_one(self, mock_client):
        """USDC/USDT should default to $1 when not in price dict."""
        mock_client.info.spot_user_state.return_value = {
            "balances": [
                {"coin": "USDC", "total": "10000", "hold": "0", "entryNtl": "0"},
            ]
        }
        mock_client.info.all_mids.return_value = {}
        result = mock_client.get_spot_balance(simple=True)
        assert result == Decimal("10000")


# ── open_long/short_position price reuse ─────────────────────────────

class TestPositionPriceReuse:
    USER_STATE = {
        "marginSummary": {"accountValue": "10000", "totalMarginUsed": "100",
                          "totalNtlPos": "500", "totalRawUsd": "10000"},
        "crossMarginSummary": {"accountValue": "10000", "totalMarginUsed": "100",
                               "totalNtlPos": "500", "totalRawUsd": "10000"},
        "withdrawable": "9000",
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

    def _setup(self, mock_client):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.info.user_state.return_value = self.USER_STATE
        # First call: entry order (buy/sell). Subsequent: SL/TP trigger orders.
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }

    def test_open_long_no_extra_get_price(self, mock_client):
        """open_long_position should not call get_price() again for SL/TP validation."""
        self._setup(mock_client)
        mock_client.open_long_position("BTC", 0.001, stop_loss_price=80000.0, take_profit_price=90000.0)
        # all_mids called once by buy() for slippage, not again for SL/TP validation
        assert mock_client.info.all_mids.call_count == 1

    def test_open_short_no_extra_get_price(self, mock_client):
        """open_short_position should not call get_price() again for SL/TP validation."""
        self._setup(mock_client)
        # For short, szi should be negative
        short_state = {**self.USER_STATE, "assetPositions": [{
            "type": "oneWay",
            "position": {
                "coin": "BTC", "entryPx": "85000", "szi": "-0.001",
                "leverage": {"type": "cross", "value": "10"},
                "liquidationPx": None, "marginUsed": "8.5",
                "positionValue": "85", "returnOnEquity": "-0.01",
                "unrealizedPnl": "-1.0",
            },
        }]}
        mock_client.info.user_state.return_value = short_state
        mock_client.open_short_position("BTC", 0.001, stop_loss_price=90000.0, take_profit_price=80000.0)
        assert mock_client.info.all_mids.call_count == 1


# ── maker_order / maker_buy / maker_sell ─────────────────────────────

class TestMakerOrder:
    """Tests for maker_order, maker_buy, maker_sell."""

    BOOK = {
        "levels": [
            [{"n": 1, "px": "85000.0", "sz": "1.0"}],
            [{"n": 1, "px": "85001.0", "sz": "1.0"}],
        ]
    }
    ORDER_STATUS_OPEN = {
        "status": "order", "order": {"order": {"sz": "0.001", "origSz": "0.001"}, "status": "open"}
    }

    def test_fills_immediately(self, mock_client):
        """If post_only order fills on first try, return immediately."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85000", "totalSz": "0.001"}}]}},
        }
        order = mock_client.maker_buy("BTC", 0.001, timeout=5)
        assert order.status == "filled"
        assert float(order.filled_size) == 0.001

    def test_reprices_then_fills(self, mock_client):
        """Places post_only, not filled, cancels, reprices, fills on second try."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.side_effect = [
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 2, "avgPx": "85000", "totalSz": "0.001"}}]}}},
        ]
        # First cycle: cancel succeeds, status shows open → reprice
        # Second cycle: fills immediately
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_OPEN
        mock_client.exchange.cancel.return_value = None

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_order("BTC", True, 0.001, timeout=10, reprice_interval=1)
        assert order.status == "filled"

    def test_fallback_ioc_on_timeout(self, mock_client):
        """After timeout, falls back to IOC order."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_OPEN
        mock_client.exchange.cancel.return_value = True
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}

        call_count = [0]
        def order_side_effect(*args, **kwargs):
            call_count[0] += 1
            if kwargs.get("order_type", {}).get("limit", {}).get("tif") == "Ioc":
                return {"status": "ok", "response": {"data": {"statuses": [
                    {"filled": {"oid": 99, "avgPx": "85001", "totalSz": "0.001"}}
                ]}}}
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": call_count[0]}}]}}}

        mock_client.exchange.order.side_effect = order_side_effect

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with patch('fractrade_hl_simple.hyperliquid.time.time') as mock_time:
                mock_time.side_effect = [0, 0, 100, 100, 100, 100, 100, 100, 100, 100, 100]
                order = mock_client.maker_buy("BTC", 0.001, timeout=1, reprice_interval=0.5)

        assert order.status == "filled"

    def test_fallback_market(self, mock_client):
        """With fallback='market', uses buy()/sell() for remaining."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_OPEN
        mock_client.exchange.cancel.return_value = True
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}

        call_count = [0]
        def order_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                return {"status": "ok", "response": {"data": {"statuses": [
                    {"filled": {"oid": 99, "avgPx": "85001", "totalSz": "0.001"}}
                ]}}}
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}

        mock_client.exchange.order.side_effect = order_side_effect

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with patch('fractrade_hl_simple.hyperliquid.time.time') as mock_time:
                mock_time.return_value = 100  # Always past deadline except first
                mock_time.side_effect = [0, 0, 0, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100]
                order = mock_client.maker_sell("BTC", 0.001, timeout=1, reprice_interval=0.5, fallback="market")
        assert order.status == "filled"
        assert order.is_maker is False

    def test_fallback_cancel(self, mock_client):
        """With fallback='cancel', returns last order without IOC."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
        }
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_OPEN
        mock_client.exchange.cancel.return_value = True

        # Use explicit timeout to skip auto-detect, then mock time to expire after 1 order
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with patch('fractrade_hl_simple.hyperliquid.time.time') as mock_time:
                # 0=deadline calc, 0=loop check, 0..=inner loop, 100=loop check (expired)
                mock_time.side_effect = [0, 0, 0, 0, 100, 100, 100, 100, 100]
                order = mock_client.maker_buy("BTC", 0.001, timeout=1, reprice_interval=0.5, fallback="cancel")

        assert order.status == "open"
        assert mock_client.exchange.order.call_count == 1

    def test_invalid_fallback_raises(self, mock_client):
        with pytest.raises(ValueError, match="fallback must be"):
            mock_client.maker_order("BTC", True, 0.001, fallback="invalid")

    def test_requires_auth(self, mock_client):
        mock_client.account = None
        with pytest.raises(RuntimeError, match="authentication"):
            mock_client.maker_buy("BTC", 0.001)

    def test_buy_uses_best_bid(self, mock_client):
        """maker_buy should place at best bid."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85000", "totalSz": "0.001"}}]}}
        }
        order = mock_client.maker_buy("BTC", 0.001, timeout=5)
        call_args = mock_client.exchange.order.call_args
        assert call_args[1]["limit_px"] == 85000.0

    def test_sell_uses_best_ask(self, mock_client):
        """maker_sell should place at best ask."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85001", "totalSz": "0.001"}}]}}
        }
        order = mock_client.maker_sell("BTC", 0.001, timeout=5)
        call_args = mock_client.exchange.order.call_args
        assert call_args[1]["limit_px"] == 85001.0

    def test_post_only_rejection_retries(self, mock_client):
        """If post_only is rejected, retries on next interval."""
        mock_client.info.l2_snapshot.return_value = self.BOOK

        call_count = [0]
        def order_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ValueError("Post-only order would cross spread")
            return {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"oid": 2, "avgPx": "85000", "totalSz": "0.001"}}
            ]}}}

        mock_client.exchange.order.side_effect = order_side_effect

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=1)
        assert order.status == "filled"
        assert call_count[0] == 2

    def test_no_bid_retries(self, mock_client):
        """When order book has no bids, retries."""
        call_count = [0]
        def l2_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"levels": [[], [{"n": 1, "px": "85001.0", "sz": "1.0"}]]}
            return {"levels": [[{"n": 1, "px": "85000.0", "sz": "1.0"}], [{"n": 1, "px": "85001.0", "sz": "1.0"}]]}

        mock_client.info.l2_snapshot.side_effect = l2_side_effect
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85000", "totalSz": "0.001"}}]}}
        }
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=1)
        assert order.status == "filled"

    def test_cancel_fails_order_was_filled(self, mock_client):
        """If cancel fails (order not found), status check detects fill."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
        }
        # Cancel fails (order filled between place and cancel), status shows filled
        mock_client.exchange.cancel.side_effect = Exception("Order not found")
        mock_client.info.query_order_by_oid.return_value = {
            "status": "order", "order": {"order": {"sz": "0.001", "origSz": "0.001"}, "status": "filled"}
        }

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=0.5)
        assert order.status == "filled"
        assert order.is_maker is True

    def test_reduce_only_passed_through(self, mock_client):
        """reduce_only is forwarded to both maker and fallback orders."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85000", "totalSz": "0.001"}}]}}
        }
        mock_client.maker_buy("BTC", 0.001, timeout=5, reduce_only=True)
        call_args = mock_client.exchange.order.call_args
        assert call_args[1]["reduce_only"] is True

    def test_auto_detect_tight_spread(self, mock_client):
        """Auto-detects short timeout for tight spreads."""
        mock_client.info.l2_snapshot.return_value = {
            "levels": [
                [{"n": 1, "px": "85000.0", "sz": "1.0"}],
                [{"n": 1, "px": "85001.0", "sz": "1.0"}],  # 1/85000 = 0.1 bps
            ]
        }
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85000", "totalSz": "0.001"}}]}}
        }
        # Don't specify timeout — let auto-detect work
        # Auto-detect calls get_order_book once, then the main loop calls it again
        order = mock_client.maker_buy("BTC", 0.001)
        assert order.status == "filled"

    def test_auto_detect_wide_spread(self, mock_client):
        """Auto-detects longer timeout for wide spreads."""
        mock_client.info.l2_snapshot.return_value = {
            "levels": [
                [{"n": 1, "px": "0.450", "sz": "100.0"}],
                [{"n": 1, "px": "0.460", "sz": "100.0"}],  # 0.01/0.455 = ~220 bps
            ]
        }
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "0.450", "totalSz": "30"}}]}}
        }
        order = mock_client.maker_buy("DYM", 30)
        assert order.status == "filled"

    def test_no_order_placed_raises(self, mock_client):
        """If loop exits with no orders placed, raises RuntimeError."""
        mock_client.info.l2_snapshot.return_value = {
            "levels": [[], []]  # Empty book
        }
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with patch('fractrade_hl_simple.hyperliquid.time.time') as mock_time:
                mock_time.side_effect = [0, 0, 100, 100]
                with pytest.raises(RuntimeError, match="no order placed"):
                    mock_client.maker_buy("BTC", 0.001, timeout=1, fallback="cancel")

    def test_maker_buy_wrapper(self, mock_client):
        """maker_buy is a convenience wrapper for maker_order(is_buy=True)."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85000", "totalSz": "0.001"}}]}}
        }
        order = mock_client.maker_buy("BTC", 0.001, timeout=5)
        assert order.is_buy is True

    def test_maker_sell_wrapper(self, mock_client):
        """maker_sell is a convenience wrapper for maker_order(is_buy=False)."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1, "avgPx": "85001", "totalSz": "0.001"}}]}}
        }
        order = mock_client.maker_sell("BTC", 0.001, timeout=5)
        assert order.is_buy is False

    def test_importable_from_package(self):
        """maker_order, maker_buy, maker_sell are importable from the package."""
        from fractrade_hl_simple import maker_order, maker_buy, maker_sell
