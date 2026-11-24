"""The JSON body of failed tool calls and its scrubbing."""

import json
import logging

import pytest
import requests

from decimal import Decimal

from fractrade_hl_simple.exceptions import (
    AddressRequiredException, ClientErrorException, ClosePositionsIncompleteException, NetworkException, OrderOutcomeUnknownException, OrderRejectedException, PositionNotClosedException,
    ProtectionReplacementIncompleteException, RateLimitException, ServerErrorException, UnknownSymbolException,
)
from fractrade_hl_simple.mcp_server.errors import MAX_DEPTH, MAX_TEXT, ToolError, error_body, error_json
from fractrade_hl_simple.models import Leverage, Order, OrderType, Position

KEY = "ab" * 32  # 64 hex characters, the shape of a private key


def test_the_body_has_every_field_as_compact_json():
    error = ToolError("ACCOUNT_NOT_FOUND", "no account named 'mian'", hint="configured: main")
    assert error_body(error) == {"error": {
        "code": "ACCOUNT_NOT_FOUND", "message": "no account named 'mian'", "retryable": False,
        "hint": "configured: main", "details": {}}}
    assert error_json(error) == json.dumps(error_body(error), separators=(",", ":"))


@pytest.mark.parametrize("error", [
    RateLimitException("Rate limited after 3 retries"),
    NetworkException("Hyperliquid could not be reached: connection refused"),
    AddressRequiredException(),
], ids=lambda e: type(e).__name__)
def test_a_library_exception_is_returned_with_its_own_fields(error):
    body = error_body(error)["error"]
    assert (body["code"], body["message"], body["retryable"]) == (error.code, str(error), error.retryable)
    assert body["hint"] is None and body["details"] == {}


def test_an_unknown_symbol_names_the_mcp_recovery_tools():
    body = error_body(UnknownSymbolException("Unknown symbol 'NEW'"))["error"]
    assert (body["code"], body["retryable"]) == ("UNKNOWN_SYMBOL", False)
    assert "refresh_market_specs" in body["hint"] and "same base_url" in body["hint"]
    assert "does not enable a dex; use enable_perp_dexs" in body["hint"]


class TestDetailsOfLibraryExceptions:
    """An agent acts on a partial outcome from details, without reading the message."""

    def test_an_order_whose_outcome_is_unknown_names_its_cloid(self):
        cloid = "0x" + "1" * 32
        details = error_body(OrderOutcomeUnknownException("BTC", cloid, "read timed out"))["error"]["details"]
        assert details == {"action": f"BTC order {cloid}", "symbol": "BTC", "cloid": cloid}

    def test_a_partial_close_has_the_order_as_results_have_it_and_the_size_still_open(self):
        closing = order(7, status="filled", filled="0.004", average_fill_price=Decimal("85000.5"))
        details = error_body(PositionNotClosedException("BTC", closing, Decimal("0.006")))["error"]["details"]
        assert details["remaining"] == "0.006" and details["symbol"] == "BTC" and details["reason"] is None
        assert details["order"] == {
            "order_id": 7, "cloid": None, "symbol": "BTC", "side": "sell", "type": "limit", "order_type": "Market",
            "is_market": True, "size": "0.01", "filled_size": "0.004", "reduced_size": None,
            "remaining_size": "0.006", "limit_price": "83300", "trigger_price": None, "slippage_cap_pct": None,
            "trigger_condition": None, "reduce_only": True, "is_position_tpsl": None, "time_in_force": "Ioc",
            "created_at": "2023-11-14T22:13:20.000Z", "children": [], "status": "filled", "average_fill_price": "85000.5"}

    def test_close_all_positions_has_the_closed_orders_and_each_failure_as_its_own_error(self):
        not_closed = PositionNotClosedException("ETH", None, Decimal("1E-7"), reason="no liquidity")
        error = ClosePositionsIncompleteException({"BTC": order(7, status="filled")}, {"ETH": not_closed})
        details = error_body(error)["error"]["details"]
        assert details["closed"]["BTC"]["order_id"] == 7 and details["closed"]["BTC"]["status"] == "filled"
        failed = details["failed"]["ETH"]
        assert (failed["code"], failed["retryable"], failed["details"]["remaining"]) == (
            "POSITION_NOT_CLOSED", False, "0.0000001")  # a decimal string, never 1E-7

    def test_an_incomplete_protection_replacement_has_the_unprotected_position(self):
        error = ProtectionReplacementIncompleteException(order(9, type="stop_loss"), [], "the new order does not "
                                                         "cover the whole position", position=position("BTC", "0.02"))
        details = error_body(error)["error"]["details"]
        assert (details["new_order"]["order_id"], details["old_order_ids"]) == (9, [])
        assert details["position"]["size"] == "0.02" and details["position"]["side"] == "long"

    def test_an_exchange_text_in_details_is_scrubbed(self):
        details = error_body(OrderRejectedException("BTC order", f"bad {KEY}"))["error"]["details"]
        assert details["reason"] == "bad <redacted>"
        nested = ClosePositionsIncompleteException({}, {"ETH": OrderRejectedException("ETH order", f"x {KEY}")})
        assert KEY not in error_json(nested)

    def test_http_errors_have_their_status_but_not_the_headers(self):
        assert error_body(ServerErrorException("unavailable", 503))["error"]["details"] == {"status_code": 503}
        error = ClientErrorException("rejected", status_code=422, error_code=None, error_message="bad request",
                                     header={"set-cookie": "x"}, error_data=None)
        assert error_body(error)["error"]["details"] == {
            "status_code": 422, "error_code": None, "error_message": "bad request", "error_data": None}

    def test_transport_objects_of_a_network_error_are_left_out(self):
        error = NetworkException("connection reset")
        error.request, error.response = object(), object()  # as requests' errors carry them
        assert error_body(error)["error"]["details"] == {}

class TestDetailsNeverBreakTheError:
    """Whatever an exception holds, the body is valid JSON with the error's own code."""

    def strict(self, error):
        def refuse(constant):
            raise AssertionError(f"{constant} is not JSON")
        return json.loads(error_json(error), parse_constant=refuse)["error"]

    def test_a_value_that_cannot_be_converted_becomes_a_marker_and_the_rest_stays(self):
        broken = order(7, status="filled")
        broken.created_at = 10 ** 18  # beyond any date
        body = self.strict(PositionNotClosedException("BTC", broken, Decimal("0.006")))
        assert body["code"] == "POSITION_NOT_CLOSED" and body["details"]["order"] == "<unavailable>"
        assert body["details"]["remaining"] == "0.006"
        nan_position = position("BTC", "NaN")
        body = self.strict(ProtectionReplacementIncompleteException(order(9), [], "x", position=nan_position))
        assert body["details"]["position"] == "<unavailable>" and body["details"]["new_order"]["order_id"] == 9

    def test_nan_and_infinity_are_null(self):
        assert self.strict(PositionNotClosedException("BTC", None, float("nan")))["details"]["remaining"] is None
        infinite = order(7, average_fill_price=Decimal("Infinity"))
        body = self.strict(PositionNotClosedException("BTC", infinite, Decimal("Infinity")))
        assert body["details"]["remaining"] is None and body["details"]["order"]["average_fill_price"] is None

    def test_a_cycle_ends_with_a_marker(self):
        error = ClosePositionsIncompleteException({}, {})
        error.failed["BTC"] = error
        assert self.strict(error)["details"]["failed"]["BTC"] == "<omitted>"
        loop = []
        loop.append(loop)
        assert self.strict(ToolError("INVALID_ARGUMENT", "x", details={"loop": loop}))["details"]["loop"] == ["<omitted>"]

    def test_the_same_order_twice_is_no_cycle(self):
        placed = order(7, status="filled")
        body = self.strict(ClosePositionsIncompleteException({"BTC": placed, "ETH": placed}, {}))
        assert body["details"]["closed"]["BTC"] == body["details"]["closed"]["ETH"] != "<omitted>"

    def test_nesting_deeper_than_the_limit_is_cut(self):
        deep = current = {}
        for _ in range(MAX_DEPTH + 5):
            current["next"] = current = {}
        text = error_json(ToolError("INVALID_ARGUMENT", "x", details={"deep": deep}))
        assert '"<omitted>"' in text


def order(order_id, status="open", filled="0", **fields):
    return Order(order_id=order_id, symbol="BTC", is_buy=False, size=Decimal("0.01"), order_type=OrderType(limit={}),
                 reduce_only=True, status=status, time_in_force="Ioc", created_at=1_700_000_000_000,
                 filled_size=Decimal(filled), limit_price=Decimal("83300"), **{"type": "limit", **fields})


def position(symbol, size):
    return Position(symbol=symbol, entry_price=Decimal("85000"), leverage=Leverage(type="cross", value=Decimal("5")),
                    liquidation_price=None, margin_used=Decimal("340"), position_value=Decimal("1700"),
                    return_on_equity=Decimal("0"), size=Decimal(size), unrealized_pnl=Decimal("0"))


def test_keys_are_redacted_in_message_hint_and_details():
    error = ToolError("INVALID_ARGUMENT", f"bad key 0x{KEY}", hint="f" * 63 + KEY, details={
        "nested": {"f" * 10 + KEY: [f"a{KEY}", int("9" * 64)]}, "fine": 42})
    body = error_body(error)["error"]
    assert (body["message"], body["hint"]) == ("bad key 0x<redacted>", "<redacted>")
    assert body["details"] == {"nested": {"<redacted>": ["<redacted>", "<redacted>"]}, "fine": 42}


def test_a_library_message_is_scrubbed_too():
    body = error_body(NetworkException(f"Hyperliquid could not be reached: {KEY}"))["error"]
    assert KEY not in body["message"]


def test_a_number_cannot_hide_a_key_behind_its_str():
    class Sneaky(int):
        def __str__(self):
            return "safe"

    error = ToolError("INVALID_ARGUMENT", "x", details={"n": Sneaky(int("9" * 64))})
    assert "9" * 64 not in error_json(error)


def test_texts_are_cut_after_redaction_so_no_part_of_a_key_remains():
    message = error_body(ToolError("INVALID_ARGUMENT", "y" * 470 + KEY + "y" * 100))["error"]["message"]
    assert len(message) == MAX_TEXT and KEY[:10] not in message
    assert error_body(ToolError("INVALID_ARGUMENT", "y" * MAX_TEXT))["error"]["message"] == "y" * MAX_TEXT
    assert error_body(ToolError("INVALID_ARGUMENT", "y" * (MAX_TEXT + 1)))["error"]["message"] == (
        "y" * (MAX_TEXT - 1) + "…")


def test_an_unknown_code_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown tool error code"):
        ToolError("NOT_A_CODE", "x")


@pytest.mark.parametrize("error", [KeyError("missing"), requests.exceptions.InvalidURL("x"), ValueError("x")],
                         ids=lambda e: type(e).__name__)
def test_anything_that_is_not_a_library_exception_is_internal(error):
    body = error_body(error)["error"]
    assert (body["code"], body["retryable"]) == ("INTERNAL", False)
    assert body["message"] == f"unexpected error ({type(error).__name__}: {error}); the traceback is in the server log"


def test_an_unexpected_error_names_its_text_and_logs_its_traceback(caplog):
    try:
        {}["missing"]
    except KeyError as raised:
        error = raised
    with caplog.at_level(logging.ERROR):
        text = error_json(error)
    assert "KeyError: 'missing'" in text and "Traceback" not in text
    (record,) = caplog.records
    assert record.exc_info[0] is KeyError and record.exc_info[2] is not None  # with its traceback


