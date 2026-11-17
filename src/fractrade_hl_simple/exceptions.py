"""The exceptions of this library: one documented contract for every failure.

Every failure a library method reports is a ``HyperliquidException`` with a fixed ``code``
and ``retryable``. ``retryable`` means the same call can succeed later; the library has
already retried what it can. Where an exception replaces a type raised before (``ValueError``,
``RuntimeError``, requests' and the SDK's errors), it also inherits that type, so existing
handlers keep catching it. The MCP server returns these fields unchanged.

Two exceptions to that, on purpose: ``ActionOutcomeUnknownException`` and
``OrderOutcomeUnknownException`` are neither requests' errors nor ``ValueError``, although an
order, modify or transfer raised those for a lost answer before; a handler that sends again on
them would execute the action twice. And ``get_portfolio()`` and ``get_funding_rates()`` no
longer wrap every failure in ``ValueError``: network failures are ``NetworkException`` or
``ServerErrorException``, as in every other read.

Kept as they were, for existing handlers: ``maker_order``'s ``RuntimeError`` when it placed no
order, and the ``RuntimeError`` for a market list without any market.
"""

from typing import Optional

import requests
from hyperliquid.utils.error import ClientError, ServerError


def _rebuild(cls, args):
    """An exception of `cls` with `args`, made without calling its __init__."""
    error = cls.__new__(cls, *args)
    error.args = args  # OSError subclasses (requests' errors) take their args in __init__
    return error


class HyperliquidException(Exception):
    """Base class for exceptions in this module; also raised for a failure Hyperliquid
    reports that has no exception of its own.

    Copying and pickling (multiprocessing, task queues) keep the type, message and every
    attribute and note as they are. Python's default would call the class again with the
    message, which fails for subclasses whose constructor takes other arguments. As with
    Python's own exceptions, only instance attributes are kept, not ``__slots__`` values.
    """

    code = "HYPERLIQUID_ERROR"
    retryable = False

    def __reduce__(self):
        return _rebuild, (type(self), self.args), self.__dict__

class ConfigurationException(HyperliquidException):
    """Exception raised for configuration errors."""
    code = "CONFIGURATION_ERROR"

class UnauthorizedException(HyperliquidException):
    """Exception raised for 401 Unauthorized status code."""
    code = "UNAUTHORIZED"

    def __init__(self, message="UNAUTHORIZED"):
        super().__init__(message)

class RateLimitException(HyperliquidException):
    """Hyperliquid's rate limit was still reached after the library's retries."""
    code = "RATE_LIMITED"
    retryable = True

    def __init__(self, message="RATE_LIMIT_EXCEEDED"):
        super().__init__(message)

class ServerErrorException(HyperliquidException, ServerError):
    """Hyperliquid answered with a server error (HTTP 5xx), or with a body that is not JSON,
    after the library's retries. Also the SDK's ``ServerError`` it replaces.

    Attributes:
        status_code: The HTTP status (200 for a body that is not JSON), or None.
    """
    code = "SERVER_ERROR"
    retryable = True

    def __init__(self, message: str = "INTERNAL_ERROR", status_code: Optional[int] = None):
        self.status_code = status_code
        self.message = message
        HyperliquidException.__init__(self, message)

class ClientErrorException(HyperliquidException, ClientError):
    """Hyperliquid rejected the request (an HTTP 4xx other than the rate limit). Also the
    SDK's ``ClientError`` it replaces, with its fields.

    Attributes:
        status_code, error_code, error_message, header, error_data: As in the SDK's ClientError.
    """
    code = "REQUEST_REJECTED"

    def __init__(self, message: str = "Hyperliquid rejected the request", status_code: Optional[int] = None,
                 error_code=None, error_message=None, header=None, error_data=None):
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message
        self.header = header
        self.error_data = error_data
        HyperliquidException.__init__(self, message)

class NetworkException(HyperliquidException, requests.ConnectionError):
    """Hyperliquid could not be reached after the library's retries (connection failed or
    another transport error). Also the requests ``ConnectionError`` it replaces; the original
    error is ``__cause__``."""
    code = "NETWORK_ERROR"
    retryable = True

    def __reduce__(self):
        # An OSError: rebuilt through its constructor, which keeps errno, strerror, filename.
        return OSError.__reduce__(self)

class TimeoutException(NetworkException, requests.Timeout):
    """Hyperliquid did not answer in time (``request_timeout``) after the library's retries.
    Also the requests ``Timeout`` it replaces."""
    code = "TIMEOUT"

class InvalidArgumentException(HyperliquidException, ValueError):
    """An argument of the call is invalid. Also the ``ValueError`` raised for it before."""
    code = "INVALID_ARGUMENT"

class UnknownSymbolException(InvalidArgumentException):
    """No market has this symbol. Perp symbols as Hyperliquid names them (``BTC``,
    ``xyz:NVDA`` with ``extended_universe=True``), spot tokens by name (``HYPE``)."""
    code = "UNKNOWN_SYMBOL"

class OrderException(HyperliquidException):
    """Base for order-related errors."""
    code = "ORDER_ERROR"

class ActionRejectedException(HyperliquidException, ValueError):
    """The exchange refused the action: it was not executed. Also the ``ValueError`` raised
    for it before.

    Attributes:
        action: What was sent, e.g. "BTC leverage update", "transfer to spot".
        reason: Hyperliquid's text, e.g. "Order must have minimum value of $10.".
    """
    code = "ACTION_REJECTED"
    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        super().__init__(f"{action} rejected: {reason}")

class OrderRejectedException(ActionRejectedException, OrderException):
    """The exchange refused the order (or the modify of one): it was not placed. ``reason``
    is Hyperliquid's text; the documented ones are listed in the API docs, "Error responses"."""
    code = "ORDER_REJECTED"

class InsufficientMarginException(OrderRejectedException):
    """The exchange refused the order for lack of margin ("Insufficient margin to place
    order.", PerpMargin in the API docs)."""
    code = "INSUFFICIENT_MARGIN"

class OrderNotFoundException(OrderException):
    """Raised when an order is not found."""
    code = "ORDER_NOT_FOUND"
    def __init__(self, message="Order not found"):
        super().__init__(message)

class ActionOutcomeUnknownException(HyperliquidException):
    """A signed action was sent, but whether the exchange executed it is unknown.

    Raised instead of retrying: the request may have been executed, and a retry would sign a
    new action that executes again. Check the account (orders, positions, balances) before
    trying again. The original error is ``__cause__``.

    Attributes:
        action: What was sent, e.g. "BTC order" or "transfer to spot".
    """
    code = "OUTCOME_UNKNOWN"  # never retry blindly: the action may have been executed
    def __init__(self, action: str, detail: str = ""):
        self.action = action
        super().__init__(
            f"Outcome of {action} is unknown{f' ({detail})' if detail else ''}; it may have "
            "been executed. Check the account before retrying."
        )

class OrderOutcomeUnknownException(ActionOutcomeUnknownException, OrderException):
    """An order was sent, but whether it was placed is unknown.

    The library looked the order up by its client order id and could not settle it. Look it
    up again with ``get_order_status_by_cloid(cloid)`` before placing it again.

    Attributes:
        symbol: The order's symbol.
        cloid: The client order id the order was sent with.
    """
    def __init__(self, symbol: str, cloid: str, detail: str = ""):
        self.symbol = symbol
        self.cloid = cloid
        super().__init__(f"{symbol} order {cloid}", detail)

class ProtectionOrderException(OrderException):
    """The entry order was placed, but a stop loss or take profit was not.

    Raised when placing it failed, or when the entry is a resting limit order that has not
    filled yet (there is no position to protect).

    Attributes:
        leg: The order that was not placed: "stop_loss", "take_profit", or with several,
            "stop_loss_2", "take_profit_1", ...
        orders: Every order that was confirmed placed, always including "entry".
        missing: Every stop loss and take profit not placed (or not left resting), this one
            first: dicts of leg, kind ("stop_loss" or "take_profit"), price, size, and
            outcome_unknown with its cloid when the order may still have arrived.

    The reason is ``__cause__``. If it is ``OrderOutcomeUnknownException``, the order may
    still have reached the exchange: check the open orders before placing it again.
    """
    code = "PROTECTION_NOT_PLACED"
    def __init__(self, leg: str, orders: dict, missing: Optional[list] = None):
        self.leg = leg
        self.orders = orders
        self.missing = list(missing or [])
        entry = orders["entry"]
        super().__init__(
            f"Entry order {entry.order_id} ({entry.status}) was placed, but the "
            f"{leg.replace('_', ' ')} was not confirmed. Orders placed: {', '.join(orders)}"
        )

class ProtectionReplacementIncompleteException(OrderException):
    """The new stop loss or take profit was placed, but the position is not protected as asked.

    An old one is still open: it could not be canceled, or it was kept because the new order
    does not protect the whole position (the position grew, or the new order executed at
    once, partly) or that could not be checked. Without an old one, the new order does not
    protect the whole position, or that could not be checked. All are reduce-only, so
    together they cannot close more than the position, but an old order can still trigger at
    its old price. Cancel it with ``cancel_order`` once that is what you want.

    Attributes:
        new_order: The order that was placed.
        old_order_ids: Ids of the old orders that may still be open; empty if there was none.
        position: The position read after the new order was placed, when the new order does
            not cover it (so the unprotected size is known); else None.
    """
    code = "PROTECTION_REPLACEMENT_INCOMPLETE"
    def __init__(self, new_order, old_order_ids, reason: str = "could not be canceled", position=None):
        self.new_order = new_order
        self.old_order_ids = list(old_order_ids)
        self.position = position
        subject = f"old orders {', '.join(map(str, self.old_order_ids))} " if self.old_order_ids else ""
        super().__init__(f"New order {new_order.order_id} was placed, but {subject}{reason}")

class OrdersNotCanceledException(OrderException):
    """Canceling several orders (``cancel_all_orders``, ``spot_cancel_all_orders``): the
    exchange did not cancel some of them. The others were canceled, or were already gone.

    Attributes:
        failed: One entry per order still open: {"symbol", "order_id", "reason"}, the reason
            being Hyperliquid's text.
    """
    code = "ORDERS_NOT_CANCELED"
    def __init__(self, failed: list):
        self.failed = [dict(entry) for entry in failed]
        super().__init__("Orders not canceled: " + "; ".join(
            f"{entry['symbol']} {entry['order_id']}: {entry['reason']}" for entry in self.failed))

class PositionNotFoundException(HyperliquidException):
    """Raised when a position is not found."""
    code = "POSITION_NOT_FOUND"
    def __init__(self, message="Position not found"):
        super().__init__(message)

class PositionNotClosedException(OrderException):
    """The closing order executed, but only for part of the position.

    The closing order is immediate-or-cancel: what the order book could not fill within the
    slippage limit was canceled, and the rest of the position is still open. Close it again,
    perhaps with more slippage, or keep it: that decision is the caller's.

    Also raised when the exchange rejected the closing order (e.g. no liquidity within the
    slippage): then nothing was closed, ``order`` is None and the rejection is ``__cause__``.

    Attributes:
        symbol: The position's symbol.
        order: The closing order: what it filled, at which average price; None if rejected.
        remaining: The size still open, in base units (Decimal).
        reason: Hyperliquid's text if the order was rejected, else None.
    """
    code = "POSITION_NOT_CLOSED"
    def __init__(self, symbol: str, order, remaining, reason: Optional[str] = None):
        self.symbol = symbol
        self.order = order
        self.remaining = remaining
        self.reason = reason
        if order is None:
            message = f"Closed nothing of {remaining} {symbol}; the closing order was rejected: {reason}"
        else:
            message = (f"Closed {order.filled_size} of {order.filled_size + remaining} {symbol}; "
                       f"{remaining} still open (order {order.order_id})")
        super().__init__(message)

class MakerOrderIncompleteException(OrderException):
    """``maker_order()`` filled part of the size, then stopped on an error.

    Its earlier attempts filled ``filled_size`` at ``average_price``; ``remaining`` is not
    filled, and no order of the chase is left in the book. The error that stopped it is
    ``__cause__``, e.g. Hyperliquid refusing the rest (below its minimum order value,
    insufficient margin) or a failed read of the order book. Send at most the rest again,
    never the whole size. When an order's outcome is not known, ``maker_order()`` raises
    ``ActionOutcomeUnknownException`` instead. Its own (the chase stopped while an attempt was
    in the book, e.g. its cancel failed, its fills could not be read, or an error that is not
    the library's interrupted it after an order was sent) name the earlier fills; an order's
    ``OrderOutcomeUnknownException`` comes as it is, with that order's cloid, so check the
    account for the earlier fills.

    Attributes:
        symbol: The order's symbol.
        filled_size: The size filled so far (Decimal).
        average_price: Its average price (Decimal), or None if a price is not known.
        remaining: The size not filled by the earlier attempts (Decimal).
    """
    code = "MAKER_ORDER_INCOMPLETE"
    def __init__(self, symbol: str, filled_size, average_price, remaining):
        self.symbol = symbol
        self.filled_size = filled_size
        self.average_price = average_price
        self.remaining = remaining
        at = f" at {average_price}" if average_price is not None else ""
        super().__init__(f"maker_order filled {filled_size} {symbol}{at}, then stopped with {remaining} "
                         "not filled; the error is the cause")

class ClosePositionsIncompleteException(OrderException):
    """``close_all_positions()`` closed some positions, but not all.

    Every position was tried: a failure does not stop the others.

    Attributes:
        closed: The closing orders that went through, by symbol.
        failed: By symbol, the exception for each position that is not (fully) closed, e.g.
            ``PositionNotClosedException`` with the size still open.
    """
    code = "CLOSE_POSITIONS_INCOMPLETE"
    def __init__(self, closed: dict, failed: dict):
        self.closed = dict(closed)
        self.failed = dict(failed)

        def reason(error) -> str:
            remaining = getattr(error, "remaining", None)
            return f"{remaining} still open" if remaining is not None else str(error)

        not_closed = ", ".join(f"{symbol} ({reason(error)})" for symbol, error in self.failed.items())
        super().__init__(f"Closed {', '.join(self.closed) or 'none'}; not closed: {not_closed}")

class UnsupportedAccountModeException(HyperliquidException):
    """The account's mode has no correct single balance.

    Raised by ``get_perp_balance`` and ``get_all_balances`` for portfolio-margin accounts,
    where borrowing makes one balance misleading, and for account modes this library does
    not know. ``get_spot_balance(simple=False)`` and ``get_user_state`` still return the
    figures per token and per dex.

    Attributes:
        mode: "portfolio_margin" or "unknown", as returned by ``get_account_mode``.
    """
    code = "UNSUPPORTED_ACCOUNT_MODE"
    def __init__(self, mode: str):
        self.mode = mode
        name = "portfolio margin accounts" if mode == "portfolio_margin" else "accounts in an unknown mode"
        super().__init__(f"No single balance is supported for {name}; use get_spot_balance(simple=False) "
                         "and get_user_state for the figures per token and per dex")


class AuthenticationRequiredException(HyperliquidException, RuntimeError):
    """The method signs an action, but the client has no account (no private key). Create
    the client with an account. Also the ``RuntimeError`` raised before."""
    code = "AUTHENTICATION_REQUIRED"
    def __init__(self, message: str = "This method requires authentication: create the client with an account "
                                      "(its private key)"):
        super().__init__(message)

class AddressRequiredException(InvalidArgumentException, RuntimeError):
    """A read about one wallet got no address, and the client has no account to read.

    Pass ``address=`` or create the client with an account. It is also a ``ValueError`` and a
    ``RuntimeError``, the two types these reads raised before, so existing handlers still
    catch it.
    """
    code = "ADDRESS_REQUIRED"

    def __init__(self, message: str = "address is required: pass address= or create the client with an account"):
        super().__init__(message)
