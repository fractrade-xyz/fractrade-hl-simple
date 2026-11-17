"""Tests for reliability fixes: error propagation, retry coverage, exception hierarchy, etc."""
import re

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
import decimal
from decimal import Decimal
import requests
import time
from types import SimpleNamespace

from hyperliquid.utils.error import ClientError, ServerError

from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.exceptions import (
    ActionOutcomeUnknownException,
    ActionRejectedException,
    AuthenticationRequiredException,
    ClosePositionsIncompleteException,
    ClientErrorException,
    InvalidArgumentException,
    OrderRejectedException,
    TimeoutException,
    NetworkException,
    PositionNotClosedException,
    UnknownSymbolException,
    PositionNotFoundException,
    OrderNotFoundException,
    InsufficientMarginException,
    OrderException,
    HyperliquidException,
    ProtectionOrderException,
    ProtectionReplacementIncompleteException,
    OrderOutcomeUnknownException,
    ServerErrorException,
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
        mock_info.name_to_coin = {name: name for name in client.market_specs}  # the SDK's symbol table
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

    def test_a_malformed_order_is_not_silently_dropped(self, mock_client):
        # Hyperliquid documents the order format; an order that does not parse is an API change
        # or a bug and must show, not vanish (has_active_orders would say False).
        mock_client.info.frontend_open_orders.return_value = [
            {"oid": 999, "coin": "BTC", "side": "B", "sz": "INVALID_NUMBER",
             "origSz": "INVALID", "timestamp": 0},  # Will fail on Decimal parse
            {
                "oid": 123, "coin": "BTC", "side": "B", "sz": "0.001",
                "limitPx": "85000", "orderType": "Limit", "tif": "Gtc",
                "origSz": "0.001", "timestamp": 1700000000000,
            },
        ]
        with pytest.raises(decimal.InvalidOperation):
            mock_client.get_open_orders()

    def test_partly_filled_order_reports_original_filled_and_remaining_size(self, mock_client):
        # frontendOpenOrders: "sz" is the size still open, "origSz" the size placed.
        mock_client.info.frontend_open_orders.return_value = [{
            "oid": 123, "coin": "BTC", "side": "B", "sz": "0.0006", "limitPx": "85000",
            "orderType": "Limit", "tif": "Gtc", "origSz": "0.001", "timestamp": 1700000000000,
        }]
        order = mock_client.get_open_orders()[0]
        assert order.size == Decimal("0.001")
        assert order.filled_size == Decimal("0.0004")
        assert order.remaining_size == Decimal("0.0006")


# ── Fix 2: cancel_all_orders() error handling ──────────────────────────

def _cancel_response(*statuses):
    """Shape of a successful bulk cancel response from the exchange."""
    return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": list(statuses)}}}


class TestCancelUnknownSymbol:
    @pytest.mark.parametrize("symbol", ["btc", "xyz:NVDA", "NOPE"])
    def test_a_cancel_of_an_unknown_market_raises_before_signing(self, mock_client, symbol):
        # The SDK looked the symbol up while signing and failed with a bare KeyError.
        with pytest.raises(UnknownSymbolException):
            mock_client.cancel_order(4, symbol)
        with pytest.raises(UnknownSymbolException):
            mock_client.cancel_order_by_cloid("0x" + "c" * 32, symbol)
        mock_client.exchange.cancel.assert_not_called()
        mock_client.exchange.cancel_by_cloid.assert_not_called()

    @pytest.mark.parametrize("symbol", ["BTCC", "btc", "xyz:NVDA"])
    def test_cancel_all_of_an_unknown_market_raises_instead_of_canceling_nothing(self, mock_client, symbol):
        # Filtered by exact symbol, a typo matched no order: it returned as if all were canceled.
        mock_client.info.open_orders.return_value = [{"coin": "BTC", "oid": 4}]
        with pytest.raises(UnknownSymbolException):
            mock_client.cancel_all_orders(symbol)
        mock_client.info.open_orders.assert_not_called()
        mock_client.exchange.bulk_cancel.assert_not_called()


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
        mock_client.exchange.bulk_cancel.return_value = _cancel_response("success", "success")
        mock_client.cancel_all_orders()  # Should not raise

    def test_only_perp_orders_are_canceled(self, mock_client):
        # The default dex's open orders include spot and outcome orders; they used to be
        # canceled too, although spot_cancel_all_orders() exists for spot.
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1}, {"coin": "@107", "oid": 2}, {"coin": "PURR/USDC", "oid": 3},
            {"coin": "#12", "oid": 4}, {"coin": "xyz:NVDA", "oid": 5},
        ]
        mock_client.exchange.bulk_cancel.return_value = _cancel_response("success", "success")
        mock_client.cancel_all_orders()
        (cancels,), _ = mock_client.exchange.bulk_cancel.call_args
        assert [c["oid"] for c in cancels] == [1, 5]

    def test_only_spot_orders_open_cancels_nothing(self, mock_client):
        mock_client.info.open_orders.return_value = [{"coin": "@107", "oid": 2}]
        mock_client.cancel_all_orders()
        mock_client.exchange.bulk_cancel.assert_not_called()

    def test_an_order_that_filled_before_the_cancel_counts_as_done(self, mock_client):
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
        ]
        # The ETH order filled between listing and canceling. Error text and shape as in the
        # exchange endpoint docs ("200: OK Error Response" for cancel).
        mock_client.exchange.bulk_cancel.return_value = _cancel_response(
            "success", {"error": "Order was never placed, already canceled, or filled."}
        )
        mock_client.cancel_all_orders()  # nothing is left open: no error

    def test_raises_when_the_exchange_does_not_confirm_the_cancel(self, mock_client):
        # The response used to be discarded, so a cancel the exchange never
        # confirmed looked successful. A body that is not JSON comes back from the SDK as
        # {"error": "Could not parse JSON: ..."} (SDK api.py): a server error, retried
        # (a cancel can be repeated safely), then raised.
        mock_client.max_retries = 1
        mock_client.info.open_orders.return_value = [{"coin": "BTC", "oid": 1}]
        mock_client.exchange.bulk_cancel.return_value = {
            "error": "Could not parse JSON: <html>502 Bad Gateway</html>"
        }
        with patch("time.sleep"), pytest.raises(ServerErrorException, match="not JSON"):
            mock_client.cancel_all_orders()
        assert mock_client.exchange.bulk_cancel.call_count == 2

    def test_uses_bulk_cancel(self, mock_client):
        """cancel_all_orders should use a single bulk_cancel call."""
        mock_client.info.open_orders.return_value = [
            {"coin": "BTC", "oid": 1},
            {"coin": "ETH", "oid": 2},
            {"coin": "SOL", "oid": 3},
        ]
        mock_client.exchange.bulk_cancel.return_value = _cancel_response(
            "success", "success", "success"
        )
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

class TestRejections:
    """Exchange rejections raise the library's exceptions with Hyperliquid's text (API docs,
    "Error responses"); they used to be plain ValueError("Order error: ..."), which the MCP
    server could only show as an internal error."""

    def test_an_order_rejected_for_lack_of_margin(self, mock_client):
        mock_client.exchange.order.return_value = _order_response({"error": "Insufficient margin to place order."})
        with pytest.raises(InsufficientMarginException) as error:
            mock_client.buy("BTC", 0.001, limit_price=80000.0)
        assert (error.value.code, error.value.reason, str(error.value)) == (
            "INSUFFICIENT_MARGIN", "Insufficient margin to place order.",
            "BTC order rejected: Insufficient margin to place order.")
        assert isinstance(error.value, ValueError) and isinstance(error.value, OrderException)

    @pytest.mark.parametrize("response", [
        {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"error": "Order must have minimum value of $10."}]}}},
        {"status": "err", "response": "Order must have minimum value of $10."},  # refused as a whole
    ])
    def test_an_order_rejected_for_another_reason_keeps_the_text(self, mock_client, response):
        mock_client.exchange.order.return_value = response
        with pytest.raises(OrderRejectedException, match=r"minimum value of \$10") as error:
            mock_client.buy("BTC", 0.001, limit_price=80000.0)
        assert error.value.code == "ORDER_REJECTED" and not isinstance(error.value, InsufficientMarginException)

    def test_a_stop_loss_and_a_spot_order_are_read_the_same_way(self, mock_client):
        mock_client.exchange.order.return_value = _order_response({"error": "Invalid TP/SL price."})
        mock_client.get_positions = lambda: [SimpleNamespace(symbol="BTC", size=Decimal("0.01"), is_short=False)]
        with pytest.raises(OrderRejectedException, match="BTC stop loss rejected: Invalid TP/SL price."):
            mock_client.stop_loss("BTC", 0.01, 80000.0)

    def test_a_modify_answered_in_no_documented_format_is_an_unknown_outcome(self, mock_client):
        # The modify may have gone through: never reported as a failure.
        mock_client.exchange.modify_order.return_value = {"status": "ok", "response": {"type": "default"}}
        with pytest.raises(ActionOutcomeUnknownException) as error:
            mock_client.modify_order("10", "BTC", True, 0.001, 80000.0, {"limit": {"tif": "Gtc"}})
        assert error.value.action == "BTC order modify" and not error.value.retryable

    def test_a_refused_leverage_change(self, mock_client):
        mock_client.exchange.update_leverage.return_value = {"status": "err", "response": "Invalid leverage value"}
        with pytest.raises(ActionRejectedException, match="BTC leverage update rejected: Invalid leverage value"):
            mock_client.set_leverage("BTC", 5)

    def test_a_transfer_signed_by_an_api_wallet(self, mock_client):
        mock_client.exchange.usd_class_transfer.return_value = {"status": "err", "response": "Must deposit before performing actions."}
        with pytest.raises(ActionRejectedException, match="use the main wallet's private key"):
            mock_client.transfer_to_spot(10.0)

    def test_without_a_key_every_signed_method_raises_the_same_exception(self, mock_client):
        mock_client.account = None
        mock_client.exchange_account = None
        for call in (lambda: mock_client.buy("BTC", 0.001), lambda: mock_client.set_leverage("BTC", 5),
                     lambda: mock_client.update_stop_loss("BTC", 80000.0), lambda: mock_client.close_all_positions()):
            with pytest.raises(AuthenticationRequiredException, match="requires authentication") as error:
                call()
            assert isinstance(error.value, RuntimeError) and error.value.code == "AUTHENTICATION_REQUIRED"

    def test_invalid_arguments(self, mock_client):
        with pytest.raises(InvalidArgumentException):
            mock_client.set_leverage("BTC", 0)
        for bad in ("oops", 10.9, True, float("inf"), "1e3"):  # 10.9 used to become order 10
            with pytest.raises(InvalidArgumentException, match="order_id"):
                mock_client.modify_order(bad, "BTC", True, 0.001, 80000.0, {"limit": {"tif": "Gtc"}})
        mock_client.exchange.modify_order.assert_not_called()

    @pytest.mark.parametrize("status", [
        {"filled": {}}, {"resting": {}}, {"filled": {"oid": 1, "avgPx": "1"}},  # fields missing
        {"resting": {"oid": None}}, {"filled": {"oid": 1, "totalSz": None, "avgPx": "1"}},  # unusable values
        {"filled": {"oid": True, "totalSz": "1", "avgPx": "1"}}, {"filled": {"oid": 1, "totalSz": "NaN", "avgPx": "1"}},
    ])
    def test_a_status_without_its_documented_fields_is_an_unknown_outcome(self, mock_client, status):
        # It used to pass as definite and then fail with a raw KeyError.
        mock_client.exchange.modify_order.return_value = _order_response(status)
        with pytest.raises(ActionOutcomeUnknownException):
            mock_client.modify_order("10", "BTC", True, 0.001, 80000.0, {"limit": {"tif": "Gtc"}})
        assert not mock_client._is_definite_order_response(_order_response(status))  # placement: looked up by cloid

    @pytest.mark.parametrize("call, sdk", [
        (lambda c: c.set_leverage("BTC", 5), "update_leverage"),
        (lambda c: c.transfer_to_spot(10.0), "usd_class_transfer"),
        (lambda c: c.add_isolated_margin("BTC", 10.0), "update_isolated_margin"),
    ])
    def test_an_action_answered_in_no_documented_format_is_an_unknown_outcome(self, mock_client, call, sdk):
        getattr(mock_client.exchange, sdk).return_value = {}
        with pytest.raises(ActionOutcomeUnknownException):
            call(mock_client)

    def test_failures_after_the_retries_are_the_librarys_exceptions(self, mock_client):
        mock_client.exchange.order.side_effect = requests.ConnectTimeout("connect timed out")
        with pytest.raises(TimeoutException):
            mock_client.buy("BTC", 0.001, limit_price=80000.0)
        mock_client.exchange.order.side_effect = ClientError(400, None, "Bad request", None)
        with pytest.raises(ClientErrorException):
            mock_client.buy("BTC", 0.001, limit_price=80000.0)

    def test_stop_losses_and_take_profits_have_their_type(self, mock_client):
        mock_client.get_positions = lambda: [SimpleNamespace(symbol="BTC", size=Decimal("0.01"), is_short=False)]
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 8}})
        assert mock_client.stop_loss("BTC", 0.01, 80000.0).type == "stop_loss"
        assert mock_client.take_profit("BTC", 0.01, 95000.0).type == "take_profit"


class TestMarketOrderTimeInForce:
    @pytest.mark.parametrize("limit_price, expected", [(None, "Ioc"), (80000.0, "Gtc")])
    def test_create_order_sends_a_market_order_as_ioc_by_default(self, mock_client, limit_price, expected):
        # Without a price it used to send Gtc: what did not fill at once rested in the book.
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 3}})
        mock_client.create_order("BTC", 0.001, True, limit_price=limit_price)
        assert mock_client.exchange.order.call_args.kwargs["order_type"] == {"limit": {"tif": expected}}

    def test_an_explicit_time_in_force_is_kept(self, mock_client):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 3}})
        mock_client.create_order("BTC", 0.001, True, time_in_force="Gtc")
        assert mock_client.exchange.order.call_args.kwargs["order_type"] == {"limit": {"tif": "Gtc"}}


class TestPrepareOrder:
    """prepare_order reports the size and price an order is signed with, so they can be
    checked before it is sent (the MCP server's guards)."""

    @staticmethod
    def sent(mock_client):
        """The SDK order call, without the client order id (a new one per order)."""
        return {k: v for k, v in mock_client.exchange.order.call_args.kwargs.items() if k != "cloid"}

    @pytest.mark.parametrize("is_buy, price", [(True, 86701.0), (False, 83300.0)])
    def test_a_market_order_placed_at_the_prepared_price_is_the_order_buy_and_sell_send(
            self, mock_client, is_buy, price):
        # mid 85000.5 at 2 %: 86700.51 is sent as 86701 (5 significant figures).
        mock_client.info.all_mids.return_value = {"BTC": "85000.5"}
        mock_client.exchange.order.return_value = _order_response(
            {"filled": {"oid": 3, "totalSz": "0.00123", "avgPx": "85000.0"}})
        prepared = mock_client.prepare_order("BTC", 0.001239, is_buy, slippage=0.02)
        (mock_client.buy if is_buy else mock_client.sell)("BTC", 0.001239, reduce_only=True, slippage=0.02)
        by_method = self.sent(mock_client)
        mock_client.create_order("BTC", prepared[0], is_buy, limit_price=prepared[1], reduce_only=True,
                                 time_in_force="Ioc")
        assert self.sent(mock_client) == by_method
        assert (by_method["sz"], by_method["limit_px"]) == prepared == (0.00123, price)

    def test_a_prepared_limit_price_comes_back_unchanged(self, mock_client):
        prepared = mock_client.prepare_order("BTC", 0.001, True, limit_price=85000.55)
        assert prepared == (0.001, 85001.0)
        assert mock_client.prepare_order("BTC", *prepared[:1], True, limit_price=prepared[1]) == prepared
        mock_client.info.all_mids.assert_not_called()  # a limit order reads no price

    @pytest.mark.parametrize("size, price", [
        (0.001, 0.0), (0.001, -5.0), (0.001, float("nan")), (0.001, float("inf")),
        (0.001, 1e-7),  # positive, but 0 at BTC's price precision
        (0.001, 1.7976931348623157e308),  # finite, but infinite once rounded to 5 significant figures
        (float("nan"), 80000.0), (float("inf"), 80000.0), ("0.001", 80000.0),
    ])
    def test_a_size_or_price_that_is_no_positive_number_is_refused_before_sending(self, mock_client, size, price):
        # A NaN size and a price of 0, below 0 or NaN went through the rounding and were signed.
        with pytest.raises(InvalidArgumentException):
            mock_client.create_order("BTC", size, True, limit_price=price)
        mock_client.exchange.order.assert_not_called()

    @pytest.mark.parametrize("trigger_price", [0.0, None])
    def test_a_stop_loss_without_a_usable_price_is_refused_before_sending(self, mock_client, trigger_price):
        # None reached the SDK, which raised a raw TypeError while building the order.
        TestStopLossTakeProfitRetry()._setup_position(mock_client)
        with pytest.raises(InvalidArgumentException, match="price"):
            mock_client.stop_loss("BTC", 0.001, trigger_price)
        mock_client.exchange.order.assert_not_called()

    def test_a_modify_or_bulk_order_without_a_price_is_refused_before_sending(self, mock_client):
        with pytest.raises(InvalidArgumentException, match="price"):
            mock_client.modify_order(5, "BTC", True, 0.001, None, {"limit": {"tif": "Gtc"}})
        with pytest.raises(InvalidArgumentException, match="price"):
            mock_client.bulk_order([{"symbol": "BTC", "is_buy": True, "size": 0.001, "limit_price": None}])
        mock_client.exchange.modify_order.assert_not_called()
        mock_client.exchange.bulk_orders.assert_not_called()


class TestModifyOrder:
    """modify_order reads the answer like the answer to an order (the SDK sends a batchModify;
    Hyperliquid documents the order statuses resting, filled and error)."""

    LIMIT = {"limit": {"tif": "Gtc"}}

    def test_a_modified_order_that_rests(self, mock_client):
        mock_client.exchange.modify_order.return_value = _order_response({"resting": {"oid": 11}})
        order = mock_client.modify_order("10", "BTC", True, 0.001, 80000.0, self.LIMIT)
        assert (order.order_id, order.status, order.type, order.limit_price) == (11, "open", "limit", Decimal("80000.0"))
        assert mock_client.exchange.modify_order.call_args.kwargs["oid"] == 10

    def test_a_modify_whose_new_price_matched_at_once_is_reported_filled(self, mock_client):
        # It used to raise ValueError("Failed to modify order"), although the order had executed.
        mock_client.exchange.modify_order.return_value = _order_response(
            {"filled": {"totalSz": "0.001", "avgPx": "85010.0", "oid": 10}})
        order = mock_client.modify_order("10", "BTC", True, 0.001, 86000.0, self.LIMIT)
        assert (order.status, order.filled_size, order.average_fill_price) == (
            "filled", Decimal("0.001"), Decimal("85010.0"))

    def test_a_rejected_modify_keeps_the_exchanges_text(self, mock_client):
        mock_client.exchange.modify_order.return_value = _order_response(
            {"error": "Order was never placed, already canceled, or filled."})
        with pytest.raises(ValueError, match="already canceled, or filled"):
            mock_client.modify_order("10", "BTC", True, 0.001, 80000.0, self.LIMIT)

    def test_errors_keep_their_type(self, mock_client):
        # Every error used to become ValueError("Failed to modify order: ...").
        with pytest.raises(UnknownSymbolException):
            mock_client.modify_order("10", "NOPE", True, 0.001, 80000.0, self.LIMIT)
        mock_client.exchange.modify_order.assert_not_called()


class TestCloseAllPositions:
    POSITIONS = [SimpleNamespace(symbol=symbol, size=Decimal(size)) for symbol, size in
                 (("BTC", "0.01"), ("ETH", "-0.5"), ("SOL", "3"))]

    def closing(self, mock_client, outcomes):
        mock_client.get_positions = lambda: self.POSITIONS
        tried = []

        def close(symbol, position):
            tried.append(symbol)
            outcome = outcomes[symbol]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        mock_client.close = close
        return tried

    def test_every_position_closed(self, mock_client):
        orders = {symbol: SimpleNamespace(order_id=str(i)) for i, symbol in enumerate(("BTC", "ETH", "SOL"))}
        self.closing(mock_client, orders)
        assert mock_client.close_all_positions() == orders

    def test_failures_are_reported_after_every_position_was_tried(self, mock_client):
        # They used to be logged and left out of the result, which looked like a closed position.
        partial = PositionNotClosedException(
            "ETH", SimpleNamespace(order_id="7", filled_size=Decimal("0.3")), Decimal("0.2"))
        btc = SimpleNamespace(order_id="1")
        tried = self.closing(mock_client, {"BTC": btc, "ETH": partial,
                                           "SOL": ValueError("Order error: Insufficient margin to place order.")})
        with pytest.raises(ClosePositionsIncompleteException) as error:
            mock_client.close_all_positions()
        assert tried == ["BTC", "ETH", "SOL"]
        assert error.value.closed == {"BTC": btc} and list(error.value.failed) == ["ETH", "SOL"]
        assert str(error.value) == ("Closed BTC; not closed: ETH (0.2 still open), "
                                    "SOL (Order error: Insufficient margin to place order.)")


class TestCloseFillVerification:
    def test_a_rejected_close_raises_with_the_whole_position_still_open(self, mock_client):
        """An IOC that matches nothing is rejected (IocCancel in the API docs); it used to log a
        warning and return the order as if the position were closed."""

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
        mock_client.exchange.order.return_value = _order_response(
            {"error": "Order could not immediately match against any resting orders."})

        with pytest.raises(PositionNotClosedException) as error:
            mock_client.close("BTC")
        assert (error.value.remaining, error.value.order) == (Decimal("0.001"), None)
        assert isinstance(error.value.__cause__, OrderRejectedException)
        assert str(error.value) == ("Closed nothing of 0.001 BTC; the closing order was rejected: "
                                    "Order could not immediately match against any resting orders.")

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
            "response": {"data": {"statuses": [{"filled": {"totalSz": "0.001", "avgPx": "85000", "oid": 999}}]}},
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
        assert order.order_id == 1
        mock_client.exchange.order.assert_called_once()

    def test_take_profit_uses_retry(self, mock_client):
        self._setup_position(mock_client)
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 2}}]}},
        }
        order = mock_client.take_profit("BTC", 0.001, 90000.0)
        assert order.order_id == 2

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

    @pytest.mark.parametrize("filled", ["0.01", "0.010"])
    def test_close_returns_the_order_when_the_whole_position_closed(self, mock_client, filled):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = _order_response(
            {"filled": {"totalSz": filled, "avgPx": "84990.0", "oid": 5}})
        order = mock_client.close("BTC", position=SimpleNamespace(symbol="BTC", size=Decimal("0.01")))
        assert (order.order_id, order.filled_size) == (5, Decimal(filled))

    def test_a_partly_filled_close_raises_with_what_is_still_open(self, mock_client):
        # Hyperliquid reports a partly filled IOC order as "filled", with the smaller size.
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = _order_response(
            {"filled": {"totalSz": "0.006", "avgPx": "84990.0", "oid": 5}})
        with pytest.raises(PositionNotClosedException) as error:
            mock_client.close("BTC", position=SimpleNamespace(symbol="BTC", size=Decimal("-0.01")))
        assert (error.value.symbol, error.value.remaining, error.value.order.filled_size) == (
            "BTC", Decimal("0.004"), Decimal("0.006"))
        assert error.value.code == "POSITION_NOT_CLOSED" and not error.value.retryable
        assert str(error.value) == "Closed 0.006 of 0.010 BTC; 0.004 still open (order 5)"
        assert mock_client.exchange.order.call_args.kwargs["is_buy"] is True  # a short is closed by buying

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

class TestMalformedSpotBalance:
    def test_a_malformed_balance_is_not_silently_dropped(self, mock_client):
        # It was skipped with a warning, so the total balance came out too low.
        mock_client.info.spot_user_state.return_value = {
            "balances": [{"coin": "BAD", "total": "not_a_number"}]
        }
        mock_client.info.all_mids.return_value = {"BAD": "1.0"}
        with pytest.raises(decimal.InvalidOperation):
            mock_client.get_spot_balance(simple=True)


# ── Fix 6: cancel_order() error handling ───────────────────────────────

class TestCancelOrderReliability:
    def test_returns_true_on_success(self, mock_client):
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        assert mock_client.cancel_order(12345, "BTC") is True

    def test_returns_false_for_an_order_that_already_filled(self, mock_client):
        # The SDK returns the exchange's per-order error instead of raising it; this used to
        # be reported as a successful cancel. Text and shape from the exchange endpoint docs.
        mock_client.exchange.cancel.return_value = _cancel_response(
            {"error": "Order was never placed, already canceled, or filled."}
        )
        assert mock_client.cancel_order(12345, "BTC") is False
        mock_client.info.query_order_by_oid.assert_not_called()  # answered at once: nothing to look up

    def test_an_http_404_is_an_error_not_an_order_that_is_gone(self, mock_client):
        # E.g. a proxy answering 404 "Not Found": the order may still be open. The error text
        # used to be matched ("not found") and the cancel reported as an order already gone.
        mock_client.exchange.cancel.side_effect = ClientError(404, None, "Not Found", None)
        with pytest.raises(ClientErrorException, match="404"):
            mock_client.cancel_order(12345, "BTC")

    @pytest.mark.parametrize("ended, canceled", [("canceled", True), ("filled", False)])
    def test_a_cancel_sent_again_after_its_answer_was_lost_reports_how_the_order_ended(
            self, mock_client, ended, canceled):
        # The first cancel went through, but its answer was lost; the one sent again finds the
        # order gone. That was reported as False (not canceled: filled, as it reads) even when
        # the first attempt had canceled it.
        mock_client.max_retries = 1
        mock_client.exchange.cancel.side_effect = [
            requests.ReadTimeout("no answer"),
            _cancel_response({"error": "Order was never placed, already canceled, or filled."})]
        mock_client.info.query_order_by_oid.return_value = {
            "status": "order", "order": {"order": {"oid": 12345}, "status": ended}}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            assert mock_client.cancel_order(12345, "BTC") is canceled
        mock_client.info.query_order_by_oid.assert_called_once_with(mock_client.public_address, 12345)

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
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        assert mock_client.cancel_order("12345", "BTC") is True
        mock_client.exchange.cancel.assert_called_with("BTC", 12345)

    def test_cancel_order_accepts_int(self, mock_client):
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        assert mock_client.cancel_order(12345, "BTC") is True
        mock_client.exchange.cancel.assert_called_with("BTC", 12345)

    @pytest.mark.parametrize("order_id", [10.9, True, -1, "12a", "", None])
    def test_an_id_that_is_no_whole_number_is_refused_before_anything_is_sent(self, mock_client, order_id):
        # int(10.9) is 10 and int(True) is 1: the cancel went to another order.
        with pytest.raises(InvalidArgumentException, match="order_id"):
            mock_client.cancel_order(order_id, "BTC")
        mock_client.exchange.cancel.assert_not_called()


CLOID = "0x" + "ab" * 16


def _open_order(oid, cloid):
    """An order of frontendOpenOrders, as the info endpoint docs show it."""
    return {"coin": "BTC", "side": "B", "limitPx": "80000.0", "sz": "0.001", "origSz": "0.001", "oid": oid,
            "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc", "isTrigger": False,
            "triggerPx": "0.0", "reduceOnly": False, "cloid": cloid}


class TestClientOrderIds:
    """Every order is placed with a client order id (cloid), the caller's or a random one; the
    Order carries the one that was sent, so the order can be looked up and canceled by it."""

    def sent_cloid(self, mock_client):
        return mock_client.exchange.order.call_args.kwargs["cloid"].to_raw()

    def test_the_callers_cloid_is_sent_and_is_the_orders(self, mock_client):
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 5}})
        order = mock_client.buy("BTC", 0.001, 80000.0, cloid=CLOID)
        assert self.sent_cloid(mock_client) == CLOID
        assert (order.order_id, order.cloid) == (5, CLOID)

    def test_without_one_each_order_gets_its_own(self, mock_client):
        mock_client.exchange.order.return_value = _order_response(
            {"filled": {"oid": 6, "totalSz": "0.001", "avgPx": "80000.0"}})
        first = mock_client.buy("BTC", 0.001, 80000.0)
        assert first.cloid == self.sent_cloid(mock_client)
        second = mock_client.buy("BTC", 0.001, 80000.0)
        assert second.cloid == self.sent_cloid(mock_client) != first.cloid

    def test_a_stop_loss_carries_its_cloid(self, mock_client):
        TestStopLossTakeProfitRetry()._setup_position(mock_client)
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 7}})
        order = mock_client.stop_loss("BTC", 0.001, 80000.0, cloid=CLOID)
        assert (self.sent_cloid(mock_client), order.cloid, order.type) == (CLOID, CLOID, "stop_loss")

    @pytest.mark.parametrize("cloid", ["0x" + "g" * 32, "0x" + "1" * 31, "1" * 34, 123, ""])
    def test_a_malformed_cloid_is_refused_before_anything_is_sent(self, mock_client, cloid):
        # The SDK's Cloid checks only "0x" and the length: "0x" and 32 "g" would be signed.
        with pytest.raises(InvalidArgumentException, match="cloid"):
            mock_client.buy("BTC", 0.001, 80000.0, cloid=cloid)
        mock_client.exchange.order.assert_not_called()

    def test_open_orders_carry_their_cloid(self, mock_client):
        mock_client.info.frontend_open_orders.return_value = [_open_order(5, CLOID), _open_order(6, None)]
        assert [(o.order_id, o.cloid) for o in mock_client.get_open_orders()] == [(5, CLOID), (6, None)]

    def test_status_lookup_by_cloid(self, mock_client):
        mock_client.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        assert mock_client.get_order_status_by_cloid(CLOID) == {"status": "unknownOid"}
        user, cloid = mock_client.info.query_order_by_cloid.call_args.args
        assert (user, cloid.to_raw()) == (mock_client.public_address, CLOID)

    @pytest.mark.parametrize("status, canceled", [
        ("success", True),
        ({"error": "Order was never placed, already canceled, or filled."}, False),
    ])
    def test_cancel_by_cloid(self, mock_client, status, canceled):
        mock_client.exchange.cancel_by_cloid.return_value = _cancel_response(status)
        assert mock_client.cancel_order_by_cloid(CLOID, "BTC") is canceled
        coin, cloid = mock_client.exchange.cancel_by_cloid.call_args.args
        assert (coin, cloid.to_raw()) == ("BTC", CLOID)
        mock_client.exchange.cancel.assert_not_called()

    def test_lookup_and_cancel_refuse_a_malformed_cloid(self, mock_client):
        with pytest.raises(InvalidArgumentException, match="cloid"):
            mock_client.cancel_order_by_cloid("0x12", "BTC")
        with pytest.raises(InvalidArgumentException, match="cloid"):
            mock_client.get_order_status_by_cloid(None)
        mock_client.exchange.cancel_by_cloid.assert_not_called()
        mock_client.info.query_order_by_cloid.assert_not_called()


# ── Fix 8: Market specs refresh ───────────────────────────────────────

class TestMarketSpecsRefresh:
    def test_refresh_makes_new_markets_known_to_the_sdk_too(self, mock_client):
        # The SDK places orders through Info's symbol tables: a refresh that updated only the
        # library's specs left new markets failing inside the SDK with a KeyError.
        mock_client.info.post.return_value = [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 100},
                {"name": "NEWCOIN", "szDecimals": 2, "maxLeverage": 20},
            ]},
            [{}, {}],
        ]
        fresh_info = mock_client.info
        mock_client.info = MagicMock()  # the Info of the client's creation
        with patch("fractrade_hl_simple.hyperliquid.Info", return_value=fresh_info) as info_class:
            result = mock_client.refresh_market_specs()
        assert info_class.call_count == 1  # a new Info: the SDK builds its symbol tables anew
        assert mock_client.info is fresh_info and mock_client.exchange.info is fresh_info
        assert "NEWCOIN" in result and result["BTC"]["max_leverage"] == 100

    def test_a_failed_refresh_raises_and_leaves_the_client_as_it_was(self, mock_client):
        old_specs, old_info = mock_client.market_specs, mock_client.info
        fresh_info = MagicMock()
        fresh_info.post.side_effect = requests.ConnectionError("Connection aborted")
        fresh_info.meta.side_effect = requests.ConnectionError("Connection aborted")
        with patch("fractrade_hl_simple.hyperliquid.Info", return_value=fresh_info), \
                pytest.raises(NetworkException):
            mock_client.refresh_market_specs()
        assert mock_client.market_specs is old_specs and mock_client.info is old_info
        assert mock_client.exchange.info is not fresh_info

    @pytest.mark.parametrize("response, error", [
        ({"unexpected": "shape"}, ValueError),                         # not [meta, contexts]
        ([{"universe": [{"name": "BTC", "maxLeverage": 50}]}, [{}]], KeyError),  # no szDecimals
        ([{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 50}]}, []], ValueError),  # contexts missing
    ])
    def test_a_malformed_response_shows_as_itself_and_changes_nothing(self, mock_client, response, error):
        # It used to fall back to meta() or to invented defaults (3 size decimals).
        old_specs = mock_client.market_specs
        fresh_info = MagicMock()
        fresh_info.post.return_value = response
        with patch("fractrade_hl_simple.hyperliquid.Info", return_value=fresh_info), pytest.raises(error):
            mock_client.refresh_market_specs()
        assert mock_client.market_specs is old_specs
        fresh_info.meta.assert_called()  # the metadata request of the refresh itself, not a fallback
        assert all(call.args[1]["type"] == "metaAndAssetCtxs" for call in fresh_info.post.call_args_list)

    def test_an_order_needs_a_market_the_sdk_knows_too(self, mock_client):
        # Specs and the SDK's symbol table come from requests a moment apart: a market listed in
        # between must be unknown, not a KeyError inside the SDK.
        mock_client.market_specs = {**mock_client.market_specs, "NEWCOIN": {"size_decimals": 2}}
        with pytest.raises(UnknownSymbolException, match="refresh_market_specs"):
            mock_client._validate_and_format_order("NEWCOIN", 1.0, 1.0)

    def test_a_client_is_not_created_without_its_markets(self):
        # As in the SDK. It used to start with a built-in table of market specs instead.
        HyperliquidClient._cached_market_specs = None
        with patch("fractrade_hl_simple.hyperliquid.Info") as info_class, \
                patch("fractrade_hl_simple.hyperliquid.Exchange"):
            info_class.return_value.post.side_effect = requests.ConnectionError("Connection aborted")
            with pytest.raises(NetworkException):
                HyperliquidClient(max_retries=0, retry_delay=0, cache_market_specs=False)

    def test_orders_never_reload_the_markets_on_their_own(self, mock_client):
        mock_client._market_specs_fetched_at = 0  # however old the specs are
        mock_client.info.post.reset_mock()
        mock_client.info.meta.reset_mock()  # called while the client was created
        mock_client._validate_and_format_order("BTC", 0.001, 85000.0)
        with pytest.raises(UnknownSymbolException, match="refresh_market_specs"):
            mock_client._validate_and_format_order("NEWCOIN", 1.0, 1.0)
        mock_client.info.post.assert_not_called()
        mock_client.info.meta.assert_not_called()


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
                "assetPositions": [],  # always in the documented response
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
        for error in [PositionNotFoundException("test"), OrderNotFoundException("test"),
                      InsufficientMarginException("BTC order", "Insufficient margin to place order."),
                      OrderException("test")]:
            try:
                raise error
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

    def test_token_without_a_perp_market_is_priced_from_its_spot_pair(self, mock_client):
        # SPCXD has no perp market; its USDC pair is @465 (mainnet spotMeta, 2026-11-13).
        # allMids keys spot pairs by pair id, so the token used to be valued at $0.
        mock_client.info.spot_user_state.return_value = {
            "balances": [{"coin": "SPCXD", "total": "4", "hold": "0", "entryNtl": "0"}]
        }
        mock_client.info.name_to_coin = {"SPCXD/USDC": "@465"}
        mock_client.info.all_mids.return_value = {"@465": "2.5"}
        assert mock_client.get_spot_balance(simple=True) == Decimal("10")


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
        # First call: the market (IOC) entry fills. Then the SL/TP trigger orders rest.
        mock_client.exchange.order.side_effect = [
            {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"oid": 1, "totalSz": "0.001", "avgPx": "85000"}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 2}}]}}},
            {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 3}}]}}},
        ]

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


# ── Stop loss / take profit side and open_*_position protection ──

def _btc_position_state(szi):
    """clearinghouseState with one BTC position of size `szi` (negative for a short)."""
    state = TestPositionPriceReuse.USER_STATE
    position = {**state["assetPositions"][0]["position"], "szi": szi}
    return {**state, "assetPositions": [{"type": "oneWay", "position": position}]}


def _order_response(status):
    """Exchange response for one order, shaped as in the exchange endpoint docs."""
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}


class TestProtectionOrders:
    def _setup(self, mock_client, szi):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.info.user_state.return_value = _btc_position_state(szi)

    def test_stop_loss_and_take_profit_close_a_short_without_is_buy(self, mock_client):
        self._setup(mock_client, "-0.001")
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 2}})
        mock_client.stop_loss("BTC", 0.001, 90000.0)
        mock_client.take_profit("BTC", 0.001, 80000.0)
        sides = [call.kwargs["is_buy"] for call in mock_client.exchange.order.call_args_list]
        assert sides == [True, True]

    def test_explicit_side_that_would_not_close_the_position_raises(self, mock_client):
        # What open_short_position used to send: a sell stop for a short.
        self._setup(mock_client, "-0.001")
        with pytest.raises(ValueError, match="does not close the short BTC position"):
            mock_client.stop_loss("BTC", 0.001, 90000.0, is_buy=False)
        mock_client.exchange.order.assert_not_called()

    def test_open_short_position_protects_with_buy_orders(self, mock_client):
        self._setup(mock_client, "-0.001")
        mock_client.exchange.order.side_effect = [
            _order_response({"filled": {"totalSz": "0.001", "avgPx": "84990.0", "oid": 1}}),
            _order_response({"resting": {"oid": 2}}),
            _order_response({"resting": {"oid": 3}}),
        ]
        orders = mock_client.open_short_position(
            "BTC", 0.001, stop_loss_price=90000.0, take_profit_price=80000.0
        )
        sides = [call.kwargs["is_buy"] for call in mock_client.exchange.order.call_args_list]
        assert sides == [False, True, True]  # sell entry, buy stop loss, buy take profit
        assert set(orders) == {"entry", "stop_loss", "take_profit"}

    def test_swapped_stop_loss_and_take_profit_are_rejected_before_the_entry(self, mock_client):
        self._setup(mock_client, "0.001")
        with pytest.raises(ValueError, match="Stop loss price must be below entry price"):
            mock_client.open_long_position(
                "BTC", 0.001, stop_loss_price=90000.0, take_profit_price=80000.0
            )
        mock_client.exchange.order.assert_not_called()

    @pytest.mark.parametrize(
        ("limit_price", "stop_loss_price"),
        [
            pytest.param(None, -1.0, id="negative-stop"),
            pytest.param(None, 0.0, id="zero-stop"),
            pytest.param(None, 0.01, id="stop-rounds-to-zero"),  # BTC prices have 1 decimal
            pytest.param(None, True, id="bool-stop"),  # passed the first check as 1, then the stop raised after the entry
            pytest.param(None, float("nan"), id="nan-stop"),
            # Above the market (85000) but below the 5% slippage limit of the market entry:
            # it would close the new long at once.
            pytest.param(None, 87000.0, id="market-entry-stop-above-the-market"),
            # Both are sent as 85000 (5 significant figures): the stop would sit on the entry.
            pytest.param(85000.4, 85000.3, id="stop-equals-entry-after-rounding"),
        ],
    )
    def test_invalid_stop_loss_is_rejected_before_the_entry(
        self, mock_client, limit_price, stop_loss_price
    ):
        self._setup(mock_client, "0.001")
        with pytest.raises(ValueError):
            mock_client.open_long_position(
                "BTC", 0.001, stop_loss_price=stop_loss_price, limit_price=limit_price
            )
        mock_client.exchange.order.assert_not_called()

    def test_resting_limit_entry_is_reported_without_placing_a_stop_loss(self, mock_client):
        # Flat account, buy limit below the market: the entry rests and there is no position
        # yet that a stop loss could protect.
        self._setup(mock_client, "0.001")
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 7}})
        with pytest.raises(ProtectionOrderException) as excinfo:
            mock_client.open_long_position(
                "BTC", 0.001, stop_loss_price=75000.0, limit_price=80000.0
            )
        assert mock_client.exchange.order.call_count == 1  # the entry only
        assert excinfo.value.orders["entry"].status == "open"

    def test_lost_stop_loss_response_after_a_partial_fill_reports_the_entry(self, mock_client):
        # The market entry fills 0.0001 of 0.001 BTC; the stop loss is sized to the fill. The
        # connection drops while it is sent, and the exchange does not know its cloid.
        self._setup(mock_client, "0.0001")
        mock_client.exchange.order.side_effect = [
            _order_response({"filled": {"totalSz": "0.0001", "avgPx": "85010.0", "oid": 1}}),
            requests.ConnectionError("Connection aborted"),
        ]
        mock_client.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"), \
                pytest.raises(ProtectionOrderException) as excinfo:
            mock_client.open_long_position("BTC", 0.001, stop_loss_price=80000.0)
        assert mock_client.exchange.order.call_args_list[1].kwargs["sz"] == 0.0001
        assert mock_client.exchange.order.call_count == 2  # the stop loss is not sent twice
        assert excinfo.value.leg == "stop_loss"
        assert list(excinfo.value.orders) == ["entry"]
        assert isinstance(excinfo.value.__cause__, OrderOutcomeUnknownException)


# ── Writes whose response is lost are never sent twice ─────

def _order_status(oid, status, orig_sz="0.001", sz="0.0"):
    """info.query_order_by_cloid response, shaped as in the info endpoint docs."""
    return {
        "status": "order",
        "order": {
            "order": {"coin": "BTC", "side": "B", "limitPx": "86000.0", "sz": sz, "oid": oid,
                      "timestamp": 1700000000000, "origSz": orig_sz, "tif": "Gtc"},
            "status": status,
            "statusTimestamp": 1700000000001,
        },
    }



class TestSeveralProtectionLevels:
    """open_*_position with stop_losses / take_profits: lists of (price, fraction)."""

    def _setup(self, mock_client, szi, fills="0.01"):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.info.user_state.return_value = _btc_position_state(szi)
        mock_client.exchange.order.side_effect = [
            _order_response({"filled": {"totalSz": fills, "avgPx": "84990.0", "oid": 1}}),
            *(_order_response({"resting": {"oid": 2 + i}}) for i in range(5)),
        ]

    def legs(self, mock_client):
        return [(c.kwargs["sz"], c.kwargs["order_type"]["trigger"]["triggerPx"], c.kwargs["is_buy"])
                for c in mock_client.exchange.order.call_args_list[1:]]

    def test_each_level_closes_its_fraction_of_the_filled_size(self, mock_client):
        self._setup(mock_client, "-0.01")
        orders = mock_client.open_short_position(
            "BTC", 0.01, stop_losses=[(90000.0, 0.5), (92000.0, 0.5)],
            take_profits=[(80000.0, 0.4), (78000.0, 0.4), (76000.0, 0.2)])
        assert list(orders) == ["entry", "stop_loss_1", "stop_loss_2", "take_profit_1", "take_profit_2",
                                "take_profit_3"]
        assert self.legs(mock_client) == [(0.005, 90000.0, True), (0.005, 92000.0, True), (0.004, 80000.0, True),
                                          (0.004, 78000.0, True), (0.002, 76000.0, True)]

    def test_after_a_partial_fill_the_last_level_takes_what_rounding_left(self, mock_client):
        # 0.00333 of 0.01 filled: 0.33 of it rounds down to 0.00109; the stop nearest the price
        # (a short's lowest, 90000) takes the rest, 0.00115, so the stops cover all of it.
        self._setup(mock_client, "-0.00333", fills="0.00333")
        mock_client.open_short_position("BTC", 0.01, stop_losses=[(90000.0, 0.34), (91000.0, 0.33), (92000.0, 0.33)])
        assert [size for size, _, _ in self.legs(mock_client)] == [0.00115, 0.00109, 0.00109]

    def test_a_partial_fill_never_loses_more_at_its_stops_than_the_full_size(self):
        # The remainder went to the last stop: with a far one last, a 0.99 fill put 0.03 on it,
        # more than the full size's 0.02, and lost more than the size that was checked.
        from fractrade_hl_simple.hyperliquid import split_size
        legs = [("stop_loss_1", "stop_loss", 9900, Decimal("0.49")), ("stop_loss_2", "stop_loss", 9890, Decimal("0.49")),
                ("stop_loss_3", "stop_loss", 5000, Decimal("0.02"))]

        def lost(size):  # a long from 10000, no fees: the move to each stop
            return sum((10000 - price) * part for (_, _, price, _), part in zip(legs, split_size(size, legs, 2, True)))

        sizes = [Decimal(n) / 100 for n in range(1, 101)]
        assert all(lost(a) <= lost(b) for a, b in zip(sizes, sizes[1:]))
        # 0.48 + 0.48 + 0.01 rounded down; the nearest stop (a long's highest, 9900) takes the 0.02 left.
        assert split_size(Decimal("0.99"), legs, 2, True) == [Decimal("0.50"), Decimal("0.48"), Decimal("0.01")]

    def test_levels_that_round_to_one_price_split_as_the_servers_plan_does(self, mock_client):
        # 80000.01 and 80000.04 are both sent as 80000.0: the remainder goes to the first, as in
        # plan_trade (which only sees the rounded prices), not to the higher raw one.
        self._setup(mock_client, "0.00027", fills="0.00027")
        mock_client.open_long_position("BTC", 0.00027, stop_losses=[(80000.01, 0.5), (80000.04, 0.5)])
        assert [size for size, _, _ in self.legs(mock_client)] == [0.00014, 0.00013]

    @pytest.mark.parametrize("kwargs, says", [
        ({"stop_losses": [(80000.0, 0.6), (79000.0, 0.6)]}, "add up to more than 1"),
        ({"stop_losses": [(80000.0, 0)]}, "fraction must be above 0"),
        ({"stop_losses": [(80000.0, 1.5)]}, "fraction must be above 0"),
        ({"stop_losses": []}, "is empty"),
        ({"stop_losses": [80000.0]}, "each is (price, fraction)"),
        ({"stop_loss_price": 80000.0, "stop_losses": [(79000.0, 1)]}, "not both"),
        ({"take_profits": [(90000.0, 0.5), (80000.0, 0.5)]}, "Take profit 2 price must be above"),
        # 0.0001 BTC at 80000 is 8 USD: a partial stop Hyperliquid would refuse after the entry.
        ({"stop_losses": [(80000.0, 0.1), (79000.0, 0.9)]}, "less than Hyperliquid's minimum order"),
    ])
    def test_invalid_levels_are_refused_before_the_entry(self, mock_client, kwargs, says):
        self._setup(mock_client, "0.001")
        with pytest.raises(ValueError, match=re.escape(says)):
            mock_client.open_long_position("BTC", 0.001, **kwargs)
        mock_client.exchange.order.assert_not_called()

    def test_a_level_for_the_whole_size_is_held_to_the_minimum_too(self, mock_client):
        # Hyperliquid may exempt a reduce-only order closing a whole position; with a position
        # already open, a "whole" level is a partial close. Nothing relies on the exemption.
        self._setup(mock_client, "0.0001", fills="0.0001")
        with pytest.raises(ValueError, match="less than Hyperliquid's minimum order"):
            mock_client.open_long_position("BTC", 0.0001, stop_losses=[(80000.0, 1)])
        mock_client.exchange.order.assert_not_called()

    def test_the_minimum_is_checked_at_the_size_as_sent(self, mock_client):
        # 0.000339 is sent as 0.00033: its 0.6 is 0.00019, worth 9.50 at 50000, not 0.00020.
        self._setup(mock_client, "0.00033", fills="0.00033")
        with pytest.raises(ValueError, match="0.00019 at 50000"):
            mock_client.open_long_position("BTC", 0.000339, stop_losses=[(50000.0, 0.6), (79000.0, 0.4)])
        mock_client.exchange.order.assert_not_called()

    def test_an_ioc_entrys_levels_are_checked_against_the_market_not_its_limit(self, mock_client):
        # Mid 85000, the entry's limit 86700: a stop at 86000 is above the market (crossed).
        self._setup(mock_client, "0.01")
        with pytest.raises(ValueError, match="Stop loss 1 price must be below"):
            mock_client.open_long_position("BTC", 0.01, limit_price=86700.0, time_in_force="Ioc",
                                           stop_losses=[(86000.0, 1)])
        mock_client.exchange.order.assert_not_called()
        orders = mock_client.open_long_position("BTC", 0.01, limit_price=86700.0, time_in_force="Ioc",
                                                stop_losses=[(80000.0, 1)], take_profits=[(86000.0, 1)])
        assert set(orders) == {"entry", "stop_loss_1", "take_profit_1"}

    def test_a_level_that_executes_at_once_is_an_error_with_the_rest_missing(self, mock_client):
        # The price crossed the stop meanwhile: it executes at once, for 0.0002 of its 0.001.
        self._setup(mock_client, "0.001", fills="0.001")
        mock_client.exchange.order.side_effect = [
            _order_response({"filled": {"totalSz": "0.001", "avgPx": "84990.0", "oid": 1}}),
            _order_response({"filled": {"totalSz": "0.0002", "avgPx": "79990.0", "oid": 2}}),
        ]
        with pytest.raises(ProtectionOrderException) as caught:
            mock_client.open_long_position("BTC", 0.001, stop_losses=[(80000.0, 1)], take_profits=[(90000.0, 1)])
        assert [(m["leg"], m["size"]) for m in caught.value.missing] == [
            ("stop_loss_1", Decimal("0.0008")), ("take_profit_1", Decimal("0.001"))]
        assert "executed at once" in str(caught.value.__cause__)

    def test_a_level_whose_outcome_is_unknown_carries_its_cloid(self, mock_client):
        # The stop loss is sent, but its answer is lost and it is not found by its cloid (yet).
        self._setup(mock_client, "-0.01")
        mock_client.exchange.order.side_effect = [
            _order_response({"filled": {"totalSz": "0.01", "avgPx": "84990.0", "oid": 1}}),
            ServerError(502, "Bad Gateway")]
        mock_client.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"), pytest.raises(ProtectionOrderException) as caught:
            mock_client.open_short_position("BTC", 0.01, stop_losses=[(90000.0, 1)])
        sent = mock_client.exchange.order.call_args.kwargs["cloid"].to_raw()
        first = caught.value.missing[0]
        assert (first["leg"], first["outcome_unknown"], first["cloid"]) == ("stop_loss_1", True, sent)

    def test_a_level_too_small_for_a_tiny_fill_is_not_placed_and_logged(self, mock_client, caplog):
        # 0.00001 filled: the take profit's half rounds to 0; the stop covers all of it.
        self._setup(mock_client, "0.00001", fills="0.00001")
        with caplog.at_level("WARNING", logger="fractrade_hl_simple"):
            orders = mock_client.open_long_position("BTC", 0.001, stop_losses=[(80000.0, 1)],
                                                    take_profits=[(90000.0, 0.5)])
        assert set(orders) == {"entry", "stop_loss_1"} and "take_profit_1" in caplog.text

    def test_the_position_is_read_once_for_all_levels(self, mock_client):
        # Each level read the positions again, between the fill and its order: five levels
        # were five reads while the new position had no protection yet.
        self._setup(mock_client, "-0.01")
        mock_client.open_short_position("BTC", 0.01, stop_losses=[(90000.0, 0.5), (92000.0, 0.5)],
                                        take_profits=[(80000.0, 0.4), (78000.0, 0.4), (76000.0, 0.2)])
        assert mock_client.info.user_state.call_count == 1

    def test_no_level_is_placed_when_the_entry_only_reduced_an_opposite_position(self, mock_client):
        # Still long after the short entry: a stop above the price would add to the long.
        self._setup(mock_client, "0.02")
        with pytest.raises(ProtectionOrderException) as caught:
            mock_client.open_short_position("BTC", 0.01, stop_losses=[(90000.0, 1)])
        assert mock_client.exchange.order.call_count == 1  # the entry only
        assert [leg["leg"] for leg in caught.value.missing] == ["stop_loss_1"]

    def test_a_buy_limit_above_the_mid_is_checked_against_the_mid(self, mock_client):
        # A buy at 87000 with the mid at 85000 fills at once, near 85000. A stop at 86000 was
        # accepted (below the limit), then sat above the price and executed at once; a take
        # profit at 86000 was refused although it is above the fill.
        self._setup(mock_client, "0.01")
        with pytest.raises(InvalidArgumentException, match="below entry"):
            mock_client.open_long_position("BTC", 0.01, stop_loss_price=86000.0, limit_price=87000.0)
        mock_client.exchange.order.assert_not_called()
        orders = mock_client.open_long_position("BTC", 0.01, stop_loss_price=80000.0, take_profit_price=86000.0,
                                                limit_price=87000.0)
        assert set(orders) == {"entry", "stop_loss", "take_profit"}

    def test_a_sell_limit_below_the_mid_is_checked_against_the_mid(self, mock_client):
        self._setup(mock_client, "-0.01")
        with pytest.raises(InvalidArgumentException, match="above entry"):
            mock_client.open_short_position("BTC", 0.01, stop_loss_price=84000.0, limit_price=83000.0)
        mock_client.exchange.order.assert_not_called()

    def test_a_resting_limit_is_checked_against_its_own_price(self, mock_client):
        # A buy at 82000 with the mid at 85000 rests: it fills at 82000 or below.
        self._setup(mock_client, "0.01")
        with pytest.raises(InvalidArgumentException, match="below entry"):
            mock_client.open_long_position("BTC", 0.01, stop_loss_price=83000.0, limit_price=82000.0)
        mock_client.exchange.order.assert_not_called()

    def test_a_failed_level_names_every_level_still_missing(self, mock_client):
        self._setup(mock_client, "-0.01")
        mock_client.exchange.order.side_effect = [
            _order_response({"filled": {"totalSz": "0.01", "avgPx": "84990.0", "oid": 1}}),
            _order_response({"resting": {"oid": 2}}),
            _order_response({"error": "Order must have minimum value of $10."}),
        ]
        with pytest.raises(ProtectionOrderException) as caught:
            mock_client.open_short_position("BTC", 0.01, stop_losses=[(90000.0, 0.5), (92000.0, 0.5)],
                                            take_profits=[(80000.0, 1)])
        error = caught.value
        assert error.leg == "stop_loss_2" and set(error.orders) == {"entry", "stop_loss_1"}
        assert [(m["leg"], m["price"], m["size"]) for m in error.missing] == [
            ("stop_loss_2", 92000.0, Decimal("0.005")), ("take_profit_1", 80000.0, Decimal("0.01"))]

    def test_an_ioc_limit_entry_is_sent_at_its_price(self, mock_client):
        self._setup(mock_client, "0.01")
        mock_client.open_long_position("BTC", 0.01, limit_price=86000.0, time_in_force="Ioc",
                                       stop_losses=[(80000.0, 1)])
        entry = mock_client.exchange.order.call_args_list[0].kwargs
        assert (entry["limit_px"], entry["order_type"]) == (86000.0, {"limit": {"tif": "Ioc"}})

    def test_time_in_force_without_a_limit_price_is_refused(self, mock_client):
        self._setup(mock_client, "0.01")
        with pytest.raises(ValueError, match="only with limit_price"):
            mock_client.open_long_position("BTC", 0.01, time_in_force="Ioc", stop_losses=[(80000.0, 1)])


class TestAmbiguousWrites:
    def test_lost_order_response_is_settled_by_cloid_without_resending(self, mock_client):
        mock_client.max_retries = 3
        mock_client.exchange.order.side_effect = requests.ConnectionError("Connection reset by peer")
        mock_client.info.query_order_by_cloid.return_value = _order_status(77, "filled")
        mock_client.info.user_fills.return_value = [
            {"coin": "BTC", "side": "B", "px": "85010.0", "sz": "0.001", "oid": 77,
             "time": 1700000000001, "crossed": True, "dir": "Open Long", "fee": "0.03"},
        ]
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            order = mock_client.buy("BTC", 0.001, limit_price=86000.0)
        assert order.status == "filled"
        assert order.average_fill_price == Decimal("85010.0")
        assert mock_client.exchange.order.call_count == 1
        cloid = mock_client.exchange.order.call_args.kwargs["cloid"]
        mock_client.info.query_order_by_cloid.assert_called_with(mock_client.public_address, cloid)

    def test_order_response_without_a_status_is_settled_by_cloid(self, mock_client):
        # Valid JSON, but no status for the order: it may or may not have been placed, so it
        # is looked up instead of being reported as a failure (callers could resend it).
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"type": "order", "data": {"statuses": []}}
        }
        mock_client.info.query_order_by_cloid.return_value = _order_status(78, "open", sz="0.001")
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            order = mock_client.buy("BTC", 0.001, limit_price=80000.0)
        assert (order.status, order.order_id) == ("open", 78)
        assert mock_client.exchange.order.call_count == 1

    def test_partly_filled_ioc_waits_for_its_fills(self, mock_client):
        # Lost response; the IOC filled 0.0006 of 0.001 and the rest was canceled. The fill
        # is not in userFills at the first lookup: it must not be reported as "no fill".
        mock_client.exchange.order.side_effect = requests.ConnectionError("Connection reset by peer")
        mock_client.info.query_order_by_cloid.return_value = _order_status(79, "canceled", sz="0.0004")
        fill = {"coin": "BTC", "side": "B", "px": "85010.0", "sz": "0.0006", "oid": 79,
                "time": 1700000000001, "crossed": True, "dir": "Open Long", "fee": "0.02"}
        mock_client.info.user_fills.side_effect = [[], [fill]]
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"):
            order = mock_client.create_order("BTC", 0.001, True, limit_price=86000.0, time_in_force="Ioc")
        assert order.status == "filled"
        assert order.filled_size == Decimal("0.0006")

    def test_triggered_stop_is_not_reported_as_resting(self, mock_client):
        # Lost response for a stop loss that fired at once: "triggered" means it is executing.
        # Reporting it as resting would let update_stop_loss remove the old stop.
        mock_client.info.user_state.return_value = _btc_position_state("0.001")
        mock_client.exchange.order.side_effect = requests.ConnectionError("Connection aborted")
        mock_client.info.query_order_by_cloid.return_value = _order_status(80, "triggered", sz="0.001")
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"), \
                pytest.raises(OrderOutcomeUnknownException, match="triggered"):
            mock_client.stop_loss("BTC", 0.001, 84000.0)

    def test_order_that_cannot_be_found_raises_and_is_not_resent(self, mock_client):
        mock_client.max_retries = 3
        mock_client.exchange.order.side_effect = requests.ReadTimeout("Read timed out")
        mock_client.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"), \
                pytest.raises(OrderOutcomeUnknownException) as excinfo:
            mock_client.buy("BTC", 0.001, limit_price=80000.0)
        assert mock_client.exchange.order.call_count == 1
        assert excinfo.value.cloid == str(mock_client.exchange.order.call_args.kwargs["cloid"])

    def test_transfer_with_unreadable_response_is_not_retried(self, mock_client):
        # A transfer cannot be looked up by id: the caller has to check the balances.
        mock_client.max_retries = 3
        mock_client.exchange.usd_class_transfer.return_value = {
            "error": "Could not parse JSON: <html>502 Bad Gateway</html>"
        }
        with pytest.raises(ActionOutcomeUnknownException):
            mock_client.transfer_to_spot(10.0)
        mock_client.exchange.usd_class_transfer.assert_called_once()

    def test_second_action_in_the_same_millisecond_waits_for_the_next(self, mock_client):
        # The SDK uses the current millisecond as nonce; a second action from the same key
        # in the same millisecond would be rejected by the exchange.
        mock_client.exchange_account.address = "0x" + "c" * 40  # a signer no other test uses
        ms = 1_700_000_000_000 * 1_000_000  # nanoseconds
        clock = iter([ms, ms, ms, ms + 1_000_000])
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        with patch("fractrade_hl_simple.hyperliquid.time.time_ns",
                   side_effect=lambda: next(clock, ms + 1_000_000)), \
                patch("fractrade_hl_simple.hyperliquid.time.sleep") as sleep:
            mock_client.cancel_order(1, "BTC")  # sent in millisecond ms
            sleep.assert_not_called()
            mock_client.cancel_order(2, "BTC")  # still ms: waits for ms + 1
        sleep.assert_called()
        assert mock_client.exchange.cancel.call_count == 2


# ── update_stop_loss / update_take_profit ─────────────────

class TestProtectionReplacement:
    OLD_STOP = {
        "coin": "BTC", "side": "A", "limitPx": "80000.0", "sz": "0.001", "oid": 11,
        "timestamp": 1700000000000, "triggerCondition": "Price below 80000", "isTrigger": True,
        "triggerPx": "80000.0", "children": [], "isPositionTpsl": False, "reduceOnly": True,
        "orderType": "Stop Market", "origSz": "0.001", "tif": None, "cloid": None,
    }

    def _setup(self, mock_client):
        # The position grew to 0.002 since the old 0.001 stop was placed.
        mock_client.info.user_state.return_value = _btc_position_state("0.002")
        mock_client.info.frontend_open_orders.return_value = [self.OLD_STOP]

    def test_new_stop_is_placed_for_the_whole_position_before_the_old_one_is_canceled(
        self, mock_client
    ):
        self._setup(mock_client)
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 12}})
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        new_stop = mock_client.update_stop_loss("BTC", 82000.0)
        actions = [name for name, _, _ in mock_client.exchange.mock_calls if not name.startswith("info.")]
        assert actions == ["order", "cancel"]
        assert mock_client.exchange.order.call_args.kwargs["sz"] == 0.002
        mock_client.exchange.cancel.assert_called_once_with("BTC", 11)
        assert new_stop.order_id == 12

    def test_old_stop_stays_when_the_new_one_is_not_confirmed(self, mock_client):
        self._setup(mock_client)
        mock_client.exchange.order.side_effect = requests.ConnectionError("Connection aborted")
        mock_client.info.query_order_by_cloid.return_value = {"status": "unknownOid"}
        with patch("fractrade_hl_simple.hyperliquid.time.sleep"), \
                pytest.raises(OrderOutcomeUnknownException):
            mock_client.update_stop_loss("BTC", 82000.0)
        mock_client.exchange.cancel.assert_not_called()

    def test_old_stop_is_kept_when_the_position_grew_meanwhile(self, mock_client):
        # Another fill raised the position from 0.002 to 0.003 while the new 0.002 stop was
        # placed: canceling the old stop would leave part of the position without one.
        self._setup(mock_client)
        mock_client.info.user_state.side_effect = (
            [_btc_position_state("0.002")] * 2 + [_btc_position_state("0.003")]
        )
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 12}})
        with pytest.raises(ProtectionReplacementIncompleteException) as excinfo:
            mock_client.update_stop_loss("BTC", 82000.0)
        mock_client.exchange.cancel.assert_not_called()
        assert excinfo.value.old_order_ids == [11]
        assert excinfo.value.position.size == Decimal("0.003")  # what the protection should cover

    def test_old_stop_is_kept_when_the_position_cannot_be_reread(self, mock_client):
        self._setup(mock_client)
        mock_client.info.user_state.side_effect = (
            [_btc_position_state("0.002")] * 2 + [requests.ConnectionError("Connection aborted")]
        )
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 12}})
        with pytest.raises(ProtectionReplacementIncompleteException) as excinfo:
            mock_client.update_stop_loss("BTC", 82000.0)
        mock_client.exchange.cancel.assert_not_called()
        assert excinfo.value.new_order.order_id == 12

    def test_a_new_stop_without_an_old_one_that_does_not_cover_the_position_is_reported(self, mock_client):
        # No stop yet. The new one triggered at once and closed only part of the position: the
        # rest has no stop, which used to be returned as a success.
        mock_client.info.frontend_open_orders.return_value = []
        mock_client.info.user_state.side_effect = (
            [_btc_position_state("0.002")] * 2 + [_btc_position_state("0.0015")]
        )
        mock_client.exchange.order.return_value = _order_response(
            {"filled": {"oid": 12, "totalSz": "0.0005", "avgPx": "81990.0"}})
        with pytest.raises(ProtectionReplacementIncompleteException) as excinfo:
            mock_client.update_stop_loss("BTC", 82000.0)
        error = excinfo.value
        assert (error.new_order.order_id, error.old_order_ids, error.position.size) == (12, [], Decimal("0.0015"))
        assert str(error) == "New order 12 was placed, but the new order does not cover the whole position"

    def test_a_new_stop_for_a_position_that_flipped_meanwhile_is_reported(self, mock_client):
        # Long 0.002 when the sell stop was placed; another fill turned it into a short of
        # 0.001: the size suffices, but a reduce-only sell cannot protect a short.
        mock_client.info.frontend_open_orders.return_value = []
        mock_client.info.user_state.side_effect = (
            [_btc_position_state("0.002")] * 2 + [_btc_position_state("-0.001")]
        )
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 12}})
        with pytest.raises(ProtectionReplacementIncompleteException) as excinfo:
            mock_client.update_stop_loss("BTC", 82000.0)
        assert excinfo.value.position.size == Decimal("-0.001")

    def test_a_new_stop_without_an_old_one_is_returned_when_it_covers_the_position(self, mock_client):
        mock_client.info.frontend_open_orders.return_value = []
        mock_client.info.user_state.return_value = _btc_position_state("0.002")
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 12}})
        assert mock_client.update_stop_loss("BTC", 82000.0).order_id == 12
        mock_client.exchange.cancel.assert_not_called()

    def test_old_stop_that_cannot_be_canceled_is_reported(self, mock_client):
        self._setup(mock_client)
        mock_client.exchange.order.return_value = _order_response({"resting": {"oid": 12}})
        mock_client.exchange.cancel.side_effect = requests.ConnectionError("Connection aborted")
        with pytest.raises(ProtectionReplacementIncompleteException) as excinfo:
            mock_client.update_stop_loss("BTC", 82000.0)
        assert excinfo.value.new_order.order_id == 12
        assert excinfo.value.old_order_ids == [11]


# ── maker_order / maker_buy / maker_sell ─────────────────────────────

class TestMakerOrder:
    """Tests for maker_order, maker_buy, maker_sell."""

    BOOK = {
        "levels": [
            [{"n": 1, "px": "85000.0", "sz": "1.0"}],
            [{"n": 1, "px": "85001.0", "sz": "1.0"}],
        ]
    }
    # Status of a maker attempt after its cancel was confirmed, with nothing filled.
    ORDER_STATUS_CANCELED = {
        "status": "order", "order": {"order": {"sz": "0.001", "origSz": "0.001"}, "status": "canceled"}
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
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_CANCELED
        mock_client.exchange.cancel.return_value = _cancel_response("success")

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_order("BTC", True, 0.001, timeout=10, reprice_interval=1)
        assert order.status == "filled"

    def test_fallback_ioc_on_timeout(self, mock_client):
        """After timeout, falls back to IOC order."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_CANCELED
        mock_client.exchange.cancel.return_value = _cancel_response("success")
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
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_CANCELED
        mock_client.exchange.cancel.return_value = _cancel_response("success")
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
        mock_client.info.query_order_by_oid.return_value = self.ORDER_STATUS_CANCELED
        mock_client.exchange.cancel.return_value = _cancel_response("success")

        # Use explicit timeout to skip auto-detect, then mock time to expire after 1 order
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with patch('fractrade_hl_simple.hyperliquid.time.time') as mock_time:
                # 0=deadline calc, 0=loop check, 0..=inner loop, 100=loop check (expired)
                mock_time.side_effect = [0, 0, 0, 0, 100, 100, 100, 100, 100]
                order = mock_client.maker_buy("BTC", 0.001, timeout=1, reprice_interval=0.5, fallback="cancel")

        assert order.status == "canceled"  # its cancel was confirmed; it can no longer fill
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
            if call_count[0] == 1:  # the documented BadAloPx text
                return _order_response({"error": "Post only order would have immediately matched, bbo was 85000@85001."})
            return {"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"oid": 2, "avgPx": "85000", "totalSz": "0.001"}}
            ]}}}

        mock_client.exchange.order.side_effect = order_side_effect

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=1)
        assert order.status == "filled"
        assert call_count[0] == 2

    def test_any_other_rejection_is_raised_at_once(self, mock_client):
        # It used to be retried as a post-only crossing until the timeout, then RuntimeError.
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = _order_response({"error": "Insufficient margin to place order."})
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(InsufficientMarginException):
            mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=1)
        assert mock_client.exchange.order.call_count == 1

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
        """If the cancel finds the order gone, the status check detects the fill."""
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
        }
        # The order filled between placing and canceling it: the cancel finds it gone, the
        # status shows it filled.
        mock_client.exchange.cancel.return_value = _cancel_response(
            {"error": "Order was never placed, already canceled, or filled."})
        mock_client.info.query_order_by_oid.return_value = {
            "status": "order", "order": {"order": {"sz": "0.001", "origSz": "0.001"}, "status": "filled"}
        }

        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=0.5)
        assert order.status == "filled"
        assert order.is_maker is True
        # The ids of the attempt that filled.
        assert (order.order_id, order.cloid) == (1, mock_client.exchange.order.call_args.kwargs["cloid"].to_raw())

    @pytest.mark.parametrize("status_lookup", [
        pytest.param(requests.ConnectionError("network down"), id="status-unreadable"),
        pytest.param([{"status": "order", "order": {"order": {"sz": "0.001", "origSz": "0.001"},
                                                    "status": "open"}}], id="status-not-ended-yet"),
    ])
    def test_unknown_fill_state_stops_the_chase(self, mock_client, status_lookup):
        # The maker order is canceled, but its final state is not known. It may have filled
        # in the meantime, so placing the next order could buy the same size twice.
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
        }
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        mock_client.info.query_order_by_oid.side_effect = status_lookup
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'), \
                pytest.raises(ActionOutcomeUnknownException):
            mock_client.maker_buy("BTC", 0.001, timeout=10, reprice_interval=0.5)
        assert mock_client.exchange.order.call_count == 1

    def test_size_finer_than_the_lot_step_is_complete_once_the_tradable_part_fills(self, mock_client):
        # 0.001009 BTC can only be traded as 0.00100 (5 size decimals). Once that filled, the
        # order is done; the 0.000009 rest used to keep the chase going until the timeout.
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.exchange.order.return_value = {
            "status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}
        }
        mock_client.exchange.cancel.return_value = _cancel_response(
            {"error": "Order was never placed, already canceled, or filled."}
        )
        mock_client.info.query_order_by_oid.return_value = {
            "status": "order", "order": {"order": {"sz": "0.0", "origSz": "0.001"}, "status": "filled"}
        }
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            order = mock_client.maker_buy("BTC", 0.001009, timeout=10, reprice_interval=0.5)
        assert order.status == "filled"
        assert order.filled_size == Decimal("0.001")
        assert mock_client.exchange.order.call_count == 1

    def test_fallback_result_includes_the_maker_fill(self, mock_client):
        # 0.0004 of 0.001 fills as maker at 85000 before the chase times out; the IOC
        # fallback fills the other 0.0006 at 85010.
        mock_client.info.l2_snapshot.return_value = self.BOOK
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.cancel.return_value = _cancel_response("success")
        mock_client.info.query_order_by_oid.return_value = {
            "status": "order",
            "order": {"order": {"sz": "0.0006", "origSz": "0.001", "limitPx": "85000.0"},
                      "status": "canceled"},
        }

        def order_side_effect(*args, **kwargs):
            if kwargs["order_type"]["limit"]["tif"] == "Ioc":
                return {"status": "ok", "response": {"data": {"statuses": [
                    {"filled": {"oid": 2, "avgPx": "85010", "totalSz": "0.0006"}}]}}}
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}

        mock_client.exchange.order.side_effect = order_side_effect
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with patch('fractrade_hl_simple.hyperliquid.time.time') as mock_time:
                mock_time.side_effect = [0, 0, 0, 100, 100, 100, 100, 100, 100, 100]
                order = mock_client.maker_buy("BTC", 0.001, timeout=1, reprice_interval=0.5)

        assert mock_client.exchange.order.call_args.kwargs["sz"] == 0.0006
        assert order.filled_size == Decimal("0.001")
        assert order.average_fill_price == Decimal("85006")  # (0.0004*85000 + 0.0006*85010) / 0.001

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
        # DYM as in mainnet metadata (2026-11-13); the fixture only knows BTC and ETH.
        mock_client.market_specs["DYM"] = {"size_decimals": 1, "max_leverage": 3, "margin_mode": "cross"}
        mock_client.info.name_to_coin["DYM"] = "DYM"
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
