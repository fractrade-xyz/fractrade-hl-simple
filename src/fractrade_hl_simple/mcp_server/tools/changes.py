"""get_changes: what changed in a wallet between two looks, from Hyperliquid's history.

A pull feed, read on each call (MCP is request/response; nothing runs between calls). One
call covers one interval [from, to], both included, with `to` a few seconds before the call:
Hyperliquid's answers can lag. The next call starts after `to`, so an event is reported once,
as long as Hyperliquid records it before the interval is read; one it records later, behind an
interval already read, cannot be ruled out (as for get_fill_history). Where Hyperliquid's
limits cut the history, `gaps` says so."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Optional

from ..._answers import REMOVED_BY_HYPERLIQUID
from ...hyperliquid import HISTORICAL_ORDERS_LIMIT
from ...models import PERP_MARKETS, Fill, Order, coin_market
from ..accounts import AccountRegistry, Target
from ..convert import dex as _dex, iso as _iso, order as _order, side as _side
from ..errors import ToolError
from ..server import ToolSpec
from .read import OBSERVATION_TEXT, SYMBOL, TIME, _check_unmatched_symbol, _ms, _observation, _wallet_tool
from .risk import _exits

MARGIN_MS = 5_000             # the interval ends this long before the call
FIRST_LOOK_MS = 3_600_000     # without since and without an earlier call: the last hour
MAX_EVENTS = 500              # fills and order entries per answer; beyond it the interval is cut (more)
LIFETIME_MS = 7 * 86_400_000  # how far back the fills of an older open reduce-only order are read
CLOCK_MS = 30 * 86_400_000    # fills are read this far past this computer's clock, which may be behind Hyperliquid's

SINCE = {**TIME, "description": "Where the interval starts: the cursor of an earlier call, or an ISO 8601 time "
                                "(UTC if no offset). Without it: where this server's last call for the same "
                                "wallet, network and symbol ended; on the first call, the last hour."}


def changes_tools(registry: AccountRegistry) -> list[ToolSpec]:
    return [_get_changes(registry)]


def _get_changes(registry: AccountRegistry) -> ToolSpec:
    cursors: dict[tuple[str, str, str], int] = {}  # this server's last interval end + 1, per wallet and filter
    busy: set[tuple[str, str, str]] = set()         # the wallets and filters a call is reading now
    guard = threading.Lock()

    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        key = (target.base_url, target.address.lower(), arguments.get("symbol") or "")
        # One call per wallet and filter at a time: two would read the same interval. A second
        # one is refused at once rather than waiting, which would hold one of the server's
        # workers that other calls (trades too) need.
        with guard:
            if key in busy:
                raise ToolError("BUSY", "get_changes is already running for this wallet and symbol",
                                hint="wait for that call's answer, then call again: it covers the same interval")
            busy.add(key)
        try:
            return look(client, target, arguments, key)
        finally:
            with guard:
                busy.discard(key)

    def look(client: Any, target: Target, arguments: dict[str, Any], key: tuple[str, str, str]) -> dict[str, Any]:
        symbol = arguments.get("symbol")
        now = _now()
        if "since" in arguments:
            start = _ms(arguments["since"], "since")
            if start > now:
                raise ToolError("INVALID_ARGUMENT", "since: in the future", hint="pass an earlier cursor or time")
        else:
            start = cursors.get(key, now - FIRST_LOOK_MS)
        end = now - MARGIN_MS
        if start > end:  # called again within seconds: nothing to read yet
            return {"interval": {"from": _iso(start), "to": _iso(end)}, "observed_at": None, "scope": None,
                    "cursor": _iso(start), **_nothing(),
                    "note": f"the interval is empty: it ends {MARGIN_MS // 1000} seconds before the call; call "
                            "again later; no fresh positions or protection snapshot was read"}
        changes = _read_changes(client, target.address, symbol, start, end, now)
        # Never moved back: an older interval read again would report its events twice.
        cursors[key] = max(cursors.get(key, 0), changes["next"])
        observation = _observation(client, symbol=symbol)
        observation["scope"]["history_all_perp_dexs"] = True
        return {**observation, "interval": {"from": _iso(start), "to": _iso(changes["next"] - 1)},
                "cursor": _iso(changes["next"]), **{k: v for k, v in changes.items() if k != "next"}}

    return _wallet_tool(
        registry, "get_changes",
        "What changed in a wallet's perp trading since the last look: fills grouped per order (with the position "
        "before and after), order status changes (placed, filled, canceled, triggered, rejected, and orders "
        "Hyperliquid removed, e.g. reduceOnlyCanceled), the positions that changed (before, after, now), and "
        "protection: stops and take profits Hyperliquid removed or shrank, and each open position's size under "
        "stops and take profits (positions now and the protection snapshot are as of the call, not the interval's "
        "end). Each protection.coverage row has warnings: STOP_COVERAGE_GAP for missing stop coverage; "
        "STOP_SIZE_EXCEEDS_POSITION / TAKE_PROFIT_SIZE_EXCEEDS_POSITION are informational review notices "
        "when remaining fixed-size exits exceed the position, with quantities and order ids. Overlap can be "
        "intentional; whole-position TP/SL are excluded from fixed-size excess checks, and absent or partial "
        "take profits are valid. Review intended levels before using sync_protection; this tool changes no "
        "orders. Pass the returned cursor as since next time; without since, it continues where "
        "this server's last call for the wallet ended (the first call: the last hour). The interval ends 5 seconds "
        "before the call; events after it come next time. gaps lists where Hyperliquid's limits may have cut the "
        f"history (it keeps 10000 fills and lists {HISTORICAL_ORDERS_LIMIT} recent orders); more: true when the answer "
        f"stopped at about {MAX_EVENTS} events, call again with cursor. Each call reads fills (with Hyperliquid's "
        "completeness checks), historical orders, positions and open orders: about 60 to 80 + 22 per enabled dex of "
        "Hyperliquid's 1200 per minute, plus 1 per 20 items returned. Call it when needed (after a trade, at the "
        "start of a turn), not in a loop." + OBSERVATION_TEXT +
        " scope.perp_dexs covers the current position/protection context; history_all_perp_dexs is true because "
        "fills and order history cover every perp dex (still filtered by symbol when given). An empty interval "
        "reads no fresh snapshot: observed_at and scope are null; keep the cursor and look again later.",
        {"since": SINCE, "symbol": SYMBOL}, read)


def _now() -> int:
    return int(time.time() * 1000)


def _nothing() -> dict[str, Any]:
    """The changes of an interval without any."""
    return {"more": False, "fills": [], "orders": [], "positions": [],
            "protection": {"removed": [], "reduced": [], "coverage": []}, "gaps": []}


def _read_changes(client: Any, address: str, symbol: Optional[str], start: int, end: int,
                  now: int) -> dict[str, Any]:
    """The changes of [start, end], the protection snapshot and the gaps. Reads, in this order:
    positions, open orders, the fills from start on (past this computer's clock: Hyperliquid's
    may be ahead), older fills of open reduce-only orders placed before start (only if any has
    less open than placed), historical orders, and the open orders again (only then): a shrink
    is only told apart from fills when the open orders read before and after the fills agree.
    The positions come first so that a stop that fills meanwhile is not counted as covering
    them. A symbol that matched nothing is checked
    last, as the other read tools do: one no longer listed can still have history."""
    def wanted(name: str, market: str) -> bool:
        return market in PERP_MARKETS and (symbol is None or name == symbol)

    positions = {p.symbol: p for p in client.get_positions(address=address)
                 if p.size != 0 and (symbol is None or p.symbol == symbol)}
    # With a symbol, the open orders of its dex only.
    first = [o for o in client.get_open_orders(symbol, address=address) if wanted(o.symbol, o.market)]
    history = client.get_fill_history(start, now + CLOCK_MS, address=address)  # every fill so far: for the shrinks too
    fills = [f for f in history.fills if wanted(f.symbol, f.market)]
    reduced = [o for o in first if not o.filled_size_known]  # reduce-only, with less open than placed
    earlier = _earlier_fills(client, address, reduced, start, now)
    listed = client.get_historical_orders(address=address)
    second = ({o.order_id: o for o in client.get_open_orders(symbol, address=address)} if reduced else {})
    # Coverage from the latest read: a stop that triggered after the first one is gone from it.
    latest = [o for o in second.values() if wanted(o.symbol, o.market)] if reduced else first

    orders = [o for o in listed if wanted(o.symbol, o.market) and o.status_time is not None
              and start <= o.status_time <= end]
    in_interval = [f for f in fills if f.time <= end]
    end, more = _cut(in_interval, orders, end)
    in_interval = [f for f in in_interval if f.time <= end]
    # Hyperliquid lists the newest first; reversed, a millisecond's entries keep their order
    # oldest first (an order placed and filled at once: open, then filled).
    orders = sorted((o for o in reversed(orders) if o.status_time is not None and o.status_time <= end),
                    key=lambda o: o.status_time or 0)
    cloids = {o.order_id: o.cloid for o in [*first, *listed] if o.cloid}
    if symbol is not None and not (fills or positions or first or any(wanted(o.symbol, o.market) for o in listed)):
        _check_unmatched_symbol(client, symbol, every_dex=True)
    return {
        "next": end + 1,
        "more": more,
        "fills": _fill_groups(in_interval, cloids),
        "orders": [{**_order(o), "status": o.status, "status_time": _iso(o.status_time),
                    "removed_by_hyperliquid": o.status in REMOVED_BY_HYPERLIQUID} for o in orders],
        "positions": _positions(client, in_interval, positions),
        "protection": {
            "removed": [_removed(o) for o in orders if o.status in REMOVED_BY_HYPERLIQUID and o.reduce_only],
            "reduced": [row for row in (_reduced(o, second, _filled(fills, earlier, history.truncated, start, o))
                                        for o in reduced) if row is not None],
            "coverage": [_coverage(p, latest) for p in positions.values()],
        },
        "gaps": _gaps(history, listed, start, end),
    }


def _earlier_fills(client: Any, address: str, reduced: list[Order], start: int, now: int) -> Optional[dict[str, Any]]:
    """The fills from the oldest reduced reduce-only order placed before start (at most
    LIFETIME_MS back) up to start, for telling its shrink from its fills; None if none needs them."""
    placed = [o.created_at for o in reduced if o.created_at is not None and o.created_at < start]
    if not placed:
        return None
    since = max(min(placed), now - LIFETIME_MS)
    if since > start - 1:  # every such order is older than the lookback: its fills are not read
        return None
    history = client.get_fill_history(since, start - 1, address=address)
    return {"from": since, "fills": history.fills, "truncated": history.truncated}


def _cut(fills: list[Fill], orders: list[Order], end: int) -> tuple[int, bool]:
    """The end of the interval with MAX_EVENTS events (fills and order entries), every event of
    its last millisecond included, so it can hold more; whether events are left after it."""
    times = sorted([f.time for f in fills] + [o.status_time for o in orders if o.status_time is not None])
    if len(times) <= MAX_EVENTS:
        return end, False
    cut = times[MAX_EVENTS - 1]
    return (cut, True) if times[-1] > cut else (end, False)


def _after(fill: Fill) -> Optional[Decimal]:
    """The position (signed) after `fill`."""
    if fill.start_position is None:
        return None
    return fill.start_position + (fill.size if fill.side == "B" else -fill.size)


def _fill_groups(fills: list[Fill], cloids: dict[int, str]) -> list[dict[str, Any]]:
    """The fills of each order, in the order of their first fill."""
    groups: dict[int, list[Fill]] = defaultdict(list)
    for fill in sorted(fills, key=lambda f: f.time):  # stable: a millisecond's fills as listed (tid is a hash)
        groups[fill.order_id].append(fill)
    rows = []
    for order_id, group in groups.items():
        size = sum((f.size for f in group), Decimal(0))
        fees = [f.fee for f in group]
        tokens = {f.fee_token for f in group}
        last = group[-1]
        rows.append({
            "order_id": order_id,
            "cloid": cloids.get(order_id),
            "symbol": group[0].symbol,
            "side": _side(group[0].side == "B"),
            "directions": sorted({f.direction for f in group}),
            "size": size,
            "average_price": sum((f.price * f.size for f in group), Decimal(0)) / size if size else None,
            "closed_pnl": sum((f.closed_pnl for f in group), Decimal(0)),
            "fee": None if any(fee is None for fee in fees) else sum((fee for fee in fees if fee is not None), Decimal(0)),
            "fee_token": next(iter(tokens)) if len(tokens) == 1 else None,
            "taker_size": sum((f.size for f in group if f.crossed), Decimal(0)),
            "maker_size": sum((f.size for f in group if not f.crossed), Decimal(0)),
            "fills": len(group),
            "first_time": _iso(group[0].time),
            "last_time": _iso(last.time),
            "position_before": group[0].start_position,
            "position_after": _after(last),
        })
    return rows


def _dex_read(client: Any, symbol: str) -> bool:
    return coin_market(symbol) != "hip3_perp" or _dex(symbol) in client.perp_dexs


def _positions(client: Any, fills: list[Fill], positions: dict[str, Any]) -> list[dict[str, Any]]:
    """Each symbol with fills in the interval: the position (signed, negative is short) before
    its first fill, after its last, and now; now is null where its dex is not read."""
    by_symbol: dict[str, list[Fill]] = defaultdict(list)
    for fill in sorted(fills, key=lambda f: f.time):  # stable, as in _fill_groups
        by_symbol[fill.symbol].append(fill)
    rows = []
    for name, group in by_symbol.items():
        last = group[-1]
        read = _dex_read(client, name)
        now = positions[name].size if name in positions else (Decimal(0) if read else None)
        rows.append({"symbol": name, "before": group[0].start_position,
                     "after": _after(last),
                     "now": now, "now_read": read})
    return rows


def _removed(order: Order) -> dict[str, Any]:
    return {"order_id": order.order_id, "cloid": order.cloid, "symbol": order.symbol, "type": order.type,
            "status": order.status, "status_time": _iso(order.status_time), "size": order.size,
            "trigger_price": order.trigger_price, "limit_price": order.limit_price}


def _reduced(order: Order, second: dict[int, Order], filled: Optional[Decimal]) -> Optional[dict[str, Any]]:
    """An open reduce-only order with less open than placed: how much of that is fills and
    how much Hyperliquid shrank it, when the reads establish it: its fills over its whole life
    are read, and the open orders read before and after them agree on it; else both null (as
    get_open_orders shows it: reduced, cause unknown). None for an order whose gap is all fills."""
    reduced = order.size - order.remaining_size
    again = second.get(order.order_id)
    if filled is None or again is None or again.remaining_size != order.remaining_size or filled > reduced:
        filled = None
    shrunk = None if filled is None else reduced - filled
    if shrunk == 0:
        return None
    return {"order_id": order.order_id, "cloid": order.cloid, "symbol": order.symbol, "type": order.type,
            "price": order.trigger_price if order.trigger_price is not None else order.limit_price,
            "placed_size": order.size, "open_size": order.remaining_size, "reduced_size": reduced,
            "filled_size": filled, "shrunk_size": shrunk}


def _filled(fills: list[Fill], earlier: Optional[dict[str, Any]], truncated: bool, start: int,
            order: Order) -> Optional[Decimal]:
    """Every fill of `order` up to the fills read, or None if the history read does not cover
    its whole life."""
    if truncated or order.created_at is None:
        return None
    own = sum((f.size for f in fills if f.order_id == order.order_id), Decimal(0))
    if order.created_at >= start:
        return own
    if earlier is None or earlier["truncated"] or order.created_at < earlier["from"]:
        return None
    return own + sum((f.size for f in earlier["fills"] if f.order_id == order.order_id), Decimal(0))


def _coverage(position: Any, orders: list[Order]) -> dict[str, Any]:
    """The size of an open position under its stops and under its take profits (the risk
    tools' reading: a TP/SL for the whole position covers all of it), with size warnings.

    Keep the coverage totals unchanged. Only fixed-size exits count toward an excess:
    a zero-size position TP/SL follows the live position, so its overlap is not a sizing
    mismatch. A position TP/SL with its own positive size stays fixed, as in _exits.
    Warnings describe this observation, not its cause or whether an overlap was intended.
    """
    size = abs(position.size)
    related = [o for o in orders if o.symbol == position.symbol]
    exits, _ = _exits(related, position.is_long, size)
    stops = sum((e.size for e in exits if e.kind == "stop_loss"), Decimal(0))
    profits = sum((e.size for e in exits if e.kind == "take_profit"), Decimal(0))
    unprotected = max(Decimal(0), size - stops)
    warnings: list[dict[str, Any]] = []
    hint = ("Review the intended levels against the live position before using sync_protection to resize "
            "supported exits. Native position TP/SL and orders with attached children are left untouched.")
    if unprotected > 0:
        warnings.append({
            "code": "STOP_COVERAGE_GAP", "severity": "warning", "symbol": position.symbol,
            "position_size": size, "order_size": stops, "unprotected_size": unprotected,
            "order_ids": [e.order_id for e in exits if e.kind == "stop_loss" and e.size > 0],
            "message": f"Counted stop losses cover {stops} of the position's {size}; "
                       f"{unprotected} has no counted stop coverage.",
            "hint": hint,
        })
    whole_position = {o.order_id for o in related if o.is_position_tpsl and o.remaining_size == 0}
    for kind, code in (("stop_loss", "STOP_SIZE_EXCEEDS_POSITION"),
                       ("take_profit", "TAKE_PROFIT_SIZE_EXCEEDS_POSITION")):
        fixed = [e for e in exits if e.kind == kind and e.order_id not in whole_position and e.size > 0]
        total = sum((e.size for e in fixed), Decimal(0))
        if total > size:
            warnings.append({
                "code": code, "severity": "info", "symbol": position.symbol,
                "position_size": size, "order_size": total, "excess_size": total - size,
                "order_ids": [e.order_id for e in fixed],
                "message": f"Remaining fixed-size {kind.replace('_', ' ')} orders total {total} "
                           f"for a position of {size}. Overlap may be intentional; review quantities "
                           "and the allocation across levels.",
                "hint": hint,
            })
    return {"symbol": position.symbol, "side": "long" if position.is_long else "short", "size": size,
            "stop_size": stops, "take_profit_size": profits, "unprotected_size": unprotected, "warnings": warnings}


def _gaps(history: Any, listed: list[Order], start: int, end: int) -> list[dict[str, Any]]:
    """Where Hyperliquid's limits may have cut the history (read up to now, so the cut can also
    lie after the interval)."""
    gaps = []
    if history.truncated:
        gaps.append({"what": "fills", "from": _iso(start), "to": _iso(end), "reason": history.truncated_reason})
    if len(listed) >= HISTORICAL_ORDERS_LIMIT:
        oldest = min((o.status_time for o in listed if o.status_time is not None), default=None)
        if oldest is None or oldest >= start:
            gaps.append({"what": "orders", "from": _iso(start), "to": _iso(oldest if oldest is not None else end),
                         "reason": "history_limit"})
    return gaps
