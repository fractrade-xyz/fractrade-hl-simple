"""Tool errors: the JSON body of a failed tool call.

The body carries the fields of the exception that occurred, unchanged: the library's
exceptions bring their own ``code`` and ``retryable``, and the server's own errors
(`ToolError`) are library exceptions with the same fields. There is no translation table.
Anything else is a bug: INTERNAL, with the traceback in the log only.

``details`` of a library exception are its documented attributes (DETAILS), converted like
results: orders and positions as the tools return them, an exception per symbol as its own
error fields, so an agent can act on a partial outcome without reading the message.

Independent of the MCP SDK: the server turns the body into an error result. Anything that
looks like a private key is redacted, and texts are cut to 500 characters, in the message,
the hint and every string, key and number in ``details``.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Optional

from ..exceptions import (
    ActionOutcomeUnknownException, ActionRejectedException, ClientErrorException, ClosePositionsIncompleteException,
    HyperliquidException, MakerOrderIncompleteException, OrderOutcomeUnknownException, OrdersNotCanceledException,
    PositionNotClosedException, ProtectionOrderException, ProtectionReplacementIncompleteException,
    ProtectionSyncIncompleteException,
    ServerErrorException, UnsupportedAccountModeException,
)
from ..models import Order, Position
from . import convert, keys

logger = logging.getLogger(__name__)

# The codes of the server's own errors; none can succeed by calling again unchanged.
CODES = frozenset({
    "INVALID_ARGUMENT",       # same code as the library's InvalidArgumentException
    "ACCOUNT_NOT_FOUND",
    "AMBIGUOUS_ACCOUNT",
    "TRADING_DISABLED",
    "KILL_SWITCH_ENGAGED",
    "GUARD_BLOCKED",
    "SIGNER_NOT_AUTHORIZED",
    "INTERNAL",
    "OUTCOME_UNKNOWN",        # the library's code, for a batch: with the cloids of its orders in details
    "BUSY",                   # the same call is still running in this server: retry once it answered
})
RETRYABLE = frozenset({"BUSY"})

MAX_TEXT = 500
MAX_DEPTH = 8  # nesting of details: an error per symbol holds an order holds its fields

# The documented attributes of the library's exceptions (their docstrings, the README error
# table), by class; a subclass has those of every class it is. Left out: the HTTP headers of
# ClientErrorException, which carry nothing to act on.
DETAILS: tuple[tuple[type[HyperliquidException], tuple[str, ...]], ...] = (
    (ServerErrorException, ("status_code",)),
    (ClientErrorException, ("status_code", "error_code", "error_message", "error_data")),
    (ActionRejectedException, ("action", "reason")),           # OrderRejected, InsufficientMargin
    (ActionOutcomeUnknownException, ("action",)),
    (OrderOutcomeUnknownException, ("symbol", "cloid")),
    (ProtectionOrderException, ("leg", "orders", "missing")),
    (ProtectionReplacementIncompleteException, ("new_order", "old_order_ids", "position")),
    (ProtectionSyncIncompleteException, ("placed", "kept", "canceled", "not_canceled", "retained", "missing",
                                         "position")),
    (PositionNotClosedException, ("symbol", "order", "remaining", "reason")),
    (MakerOrderIncompleteException, ("symbol", "filled_size", "average_price", "remaining")),
    (ClosePositionsIncompleteException, ("closed", "failed")),
    (OrdersNotCanceledException, ("failed", "canceled", "not_open")),
    (UnsupportedAccountModeException, ("mode",)),
)

# What to do after an error whose outcome is unknown, most specific first; for a
# library exception, which has no hint of its own.
HINTS: tuple[tuple[type[HyperliquidException], str], ...] = (
    (OrderOutcomeUnknownException,
     "do not place the order again: look it up with get_order_status_by_cloid (details.cloid); an order "
     "not found yet can still arrive"),
    (ActionOutcomeUnknownException,
     "do not repeat the call: it may have been executed; check get_order_status, get_open_orders and "
     "get_positions first"),
    (ProtectionSyncIncompleteException,
     "the position is not protected as asked: details say what was placed, kept, canceled and not_canceled (may "
     "still be open), and the levels missing; one with outcome_unknown may still arrive: look it up with "
     "get_order_status_by_cloid (its cloid) before placing it again; then call sync_protection again, or place "
     "what is missing"),
    (ProtectionOrderException,
     "the entry was placed without the stop losses and take profits in details.missing (price and size of "
     "each). Check get_positions and the entry's filled_size: what filled is an open position without them "
     "(place them with stop_loss / take_profit, or close it). An entry still open (a resting limit, waiting or "
     "partly filled) can be canceled: that cancels its unfilled rest and the orders attached to it. One with outcome_unknown may still "
     "arrive: look it up with get_order_status_by_cloid (its cloid) before placing it again"),
)


def scrub(text: str) -> str:
    """`text` with private-key-like runs redacted, then cut to MAX_TEXT characters.
    Redacted first, so a cut can never leave part of a key."""
    text = keys.redact(text)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _scrub_value(value: Any, depth: int = 0, within: frozenset[int] = frozenset()) -> Any:
    """`value` as a JSON value that cannot fail to serialize: texts scrubbed, decimals as
    decimal strings (never 1E-7), NaN and infinity as null, orders and positions as the tools
    return them, an exception as its own error fields. A value that cannot be converted, a
    cycle, or nesting deeper than MAX_DEPTH becomes a marker text; its siblings stay, as each
    value is converted on its own."""
    try:
        return _convert(value, depth, within)
    except Exception:  # never let a detail replace the error it belongs to, or its siblings
        return "<unavailable>"


def _convert(value: Any, depth: int, within: frozenset[int]) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):  # an all-digit key is a number too; a subclass's str() may hide it
        number = int(value)
        return "<redacted>" if keys.contains_private_key(str(number)) else number
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return scrub(format(value, "f")) if value.is_finite() else None
    if depth >= MAX_DEPTH or id(value) in within:
        return "<omitted>"
    within = within | {id(value)}  # the objects on the way here: a cycle ends at the first repeat
    if isinstance(value, Order):
        value = convert.placed_order(value)
    elif isinstance(value, Position):
        value = convert.position(value)
    elif isinstance(value, BaseException):  # e.g. the failure of one symbol in close_all_positions
        return _fields(value, depth + 1, within)
    if isinstance(value, Mapping):
        return {_key(key): _scrub_value(item, depth + 1, within) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(item, depth + 1, within) for item in value]
    return scrub(str(value))


def _key(key: Any) -> str:
    try:
        return scrub(str(key))
    except Exception:
        return "<unavailable>"


class ToolError(HyperliquidException):
    """An error of the MCP server itself (account resolution, trading switched off, ...),
    with the same fields as the library's exceptions plus a hint and details."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None,
                 details: Optional[Mapping[str, Any]] = None) -> None:
        if code not in CODES:
            raise ValueError(f"unknown tool error code {code!r}")
        self.code = code
        self.retryable = code in RETRYABLE
        self.hint = None if hint is None else scrub(hint)
        self.details: dict[str, Any] = _scrub_value(details or {})
        super().__init__(scrub(message))


def error_body(error: BaseException) -> dict[str, Any]:
    """The JSON body for `error`: the fields of the exception itself."""
    return {"error": _fields(error, 0, frozenset())}


def _fields(error: BaseException, depth: int, within: frozenset[int]) -> dict[str, Any]:
    if not isinstance(error, HyperliquidException):
        # Log records, tracebacks included, are scrubbed by the filter the server installs on
        # its stderr handler.
        logger.error("unexpected error in a tool call", exc_info=error)
        error = ToolError("INTERNAL", f"unexpected error ({type(error).__name__}); the details are in the server log")
    hint = getattr(error, "hint", None)
    if hint is None:
        hint = next((text for kind, text in HINTS if isinstance(error, kind)), None)
    details = error.details if isinstance(error, ToolError) else library_details(error)
    return {
        "code": error.code,
        "message": scrub(str(error)),
        "retryable": error.retryable,
        "hint": None if hint is None else scrub(str(hint)),
        "details": _scrub_value(details, depth, within | {id(error)}),
    }


def library_details(error: HyperliquidException) -> dict[str, Any]:
    """The documented attributes of a library exception (DETAILS), as they are."""
    fields = [name for kind, names in DETAILS if isinstance(error, kind) for name in names]
    return {name: getattr(error, name, None) for name in dict.fromkeys(fields)}


def error_json(error: BaseException) -> str:
    """`error_body(error)` as compact JSON (valid JSON: no NaN or infinity)."""
    return json.dumps(error_body(error), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
