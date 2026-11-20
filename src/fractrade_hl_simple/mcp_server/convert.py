"""The JSON form of the library's objects, shared by tool results and error details.

Pure: no client, no request. Decimals stay Decimal here; the server writes them as decimal
strings.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from .._answers import MARKET_TIFS, TPSL_MARKET_SLIPPAGE_PCT, parse_open_order
from ..models import Order, Position


def decimal(value: Any) -> Decimal:
    """`value` exactly as written (a float's shortest text, not its binary expansion); NaN for
    anything that is not a number, bool included, so that a check fails on it."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal("NaN")
    if isinstance(value, int):
        return Decimal(int(value))
    if isinstance(value, float):
        return Decimal(repr(float(value)))  # float(): a subclass's repr may be no number (numpy)
    return Decimal("NaN")


def iso(ms: Optional[int]) -> Optional[str]:
    """Milliseconds since the epoch as ISO 8601 UTC."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def side(is_buy: bool) -> str:
    return "buy" if is_buy else "sell"


def dex(symbol: str) -> Optional[str]:
    """The HIP-3 dex of a perp symbol ("xyz" for xyz:NVDA), None for the default dex."""
    return symbol.split(":", 1)[0] if ":" in symbol else None


def order_kind(order: Order) -> tuple[Optional[str], bool]:
    """Hyperliquid's name of the order's type ("Limit", "Market", "Stop Market", "Stop Limit",
    "Take Profit Market", "Take Profit Limit") and whether it executes as a market order. A
    trigger that is neither a stop loss nor a take profit has no name (None)."""
    trigger = order.order_type.trigger if order.order_type is not None else None
    if trigger is not None:
        is_market = trigger.get("isMarket") is True
        name = {"sl": "Stop", "tp": "Take Profit"}.get(str(trigger.get("tpsl")))
        return (None if name is None else f"{name} {'Market' if is_market else 'Limit'}"), is_market
    is_market = order.time_in_force in MARKET_TIFS
    return ("Market" if is_market else "Limit"), is_market


def slippage_cap_pct(order: Order, kind: Optional[str]) -> Optional[float]:
    """How far, in %, a triggered order may execute beyond its trigger price against its own
    side: Hyperliquid's 10 for a TP/SL market order; for a TP/SL limit order the distance to
    its limit price (a buy's above the trigger, a sell's below; 0 for a limit on the better
    side). None for limit and market orders, whose limit price is their worst price, and when
    the prices are missing."""
    if kind in ("Stop Market", "Take Profit Market"):
        return TPSL_MARKET_SLIPPAGE_PCT
    if kind not in ("Stop Limit", "Take Profit Limit"):
        return None
    trigger, limit = order.trigger_price, order.limit_price
    if not isinstance(trigger, Decimal) or not isinstance(limit, Decimal) or not trigger.is_finite() \
            or not limit.is_finite() or trigger <= 0 or limit <= 0:
        return None
    try:  # a derived figure: never in the way of the order's own fields, also in error details
        against = limit - trigger if order.is_buy else trigger - limit
        pct = round(float(max(against, Decimal(0)) * 100 / trigger), 2)
    except (ArithmeticError, OverflowError, ValueError):
        return None
    return pct if math.isfinite(pct) else None


def order_type_name(order: Order) -> Optional[str]:
    """The order's type as the tools show it: the library's (limit, stop_loss, take_profit), but
    trigger_entry for a trigger order without reduce_only (as place_trigger_entry places it).
    Only a label: one on the side that closes a position can still close or reverse it."""
    if order.trigger_price is not None and not order.reduce_only and order.type in ("stop_loss", "take_profit"):
        return "trigger_entry"
    return order.type


def order(order: Order) -> dict[str, Any]:
    """An order as get_open_orders lists it. For a reduce-only order whose size no longer
    open may not be fills (Order.filled_size_known: Hyperliquid also shrinks such an order to
    the position), filled_size is null and reduced_size says how much is no longer open."""
    kind, is_market = order_kind(order)
    known = order.filled_size_known
    return {
        "order_id": order.order_id,
        "cloid": order.cloid,
        "symbol": order.symbol,
        "side": side(order.is_buy),
        "type": order_type_name(order),
        "order_type": kind,
        "is_market": is_market,
        "size": order.size,
        "filled_size": order.filled_size if known else None,
        "reduced_size": None if known else order.filled_size,
        "remaining_size": order.remaining_size,
        "limit_price": order.limit_price,
        "trigger_price": order.trigger_price,
        "slippage_cap_pct": slippage_cap_pct(order, kind),
        "trigger_condition": order.trigger_condition,
        "reduce_only": order.reduce_only,
        "is_position_tpsl": order.is_position_tpsl,
        "time_in_force": order.time_in_force,
        "created_at": iso(order.created_at),
        "children": _children(order),
    }


def _children(parent: Order) -> list[dict[str, Any]]:
    """The orders attached to `parent` (the stop loss and take profit of a resting limit entry,
    placed once it fills), each with the fields of an order. They are not open orders of their own."""
    return [order(parse_open_order(child, parent.symbol)) for child in parent.children or []]


def placed_order(placed: Order) -> dict[str, Any]:
    """An order a trade placed: the get_open_orders fields, its status ("open",
    "filled") and the average price of its fills."""
    return {**order(placed), "status": placed.status, "average_fill_price": placed.average_fill_price}


def position(position: Position) -> dict[str, Any]:
    """A position as get_positions lists it."""
    funding = position.cum_funding
    return {
        "symbol": position.symbol,
        "dex": dex(position.symbol),
        "side": "long" if position.size > 0 else "short",
        "size": abs(position.size),
        "entry_price": position.entry_price,
        "notional": position.position_value,
        "unrealized_pnl": position.unrealized_pnl,
        "return_on_equity": position.return_on_equity,
        "leverage": position.leverage.value,
        "margin_mode": position.leverage.type,
        "liquidation_price": position.liquidation_price,
        "margin_used": position.margin_used,
        "funding_since_open": None if funding is None else funding.since_open,
    }
