"""The JSON form of the library's objects, shared by tool results and error details.

Pure: no client, no request. Decimals stay Decimal here; the server writes them as decimal
strings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..models import Order, Position


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


def order(order: Order) -> dict[str, Any]:
    """An order as get_open_orders lists it."""
    return {
        "order_id": order.order_id,
        "cloid": order.cloid,
        "symbol": order.symbol,
        "side": side(order.is_buy),
        "type": order.type,
        "size": order.size,
        "filled_size": order.filled_size,
        "remaining_size": order.size - order.filled_size,
        "limit_price": order.limit_price,
        "trigger_price": order.trigger_price,
        "trigger_condition": order.trigger_condition,
        "reduce_only": order.reduce_only,
        "is_position_tpsl": order.is_position_tpsl,
        "time_in_force": order.time_in_force,
        "created_at": iso(order.created_at),
    }


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
