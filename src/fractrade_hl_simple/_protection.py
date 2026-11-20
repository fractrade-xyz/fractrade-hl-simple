"""Stop losses and take profits as workflows: an entry with its levels (open_long_position,
open_short_position; a resting limit entry with them attached), a stop entry (trigger_order),
moving a position's stop loss or take profit (update_stop_loss, update_take_profit) and
replacing all its levels (resize_protection).

The client's methods of those names document what callers get; the functions here take the
client first and do the work. A level, or leg, is (name, kind, price, fraction or size).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from . import _answers, _inputs, _signing
from ._inputs import TimeInForce
from .exceptions import (
    ActionOutcomeUnknownException,
    AuthenticationRequiredException,
    HyperliquidException,
    InvalidArgumentException,
    OrderOutcomeUnknownException,
    OrderRejectedException,
    PositionNotFoundException,
    ProtectionOrderException,
    ProtectionReplacementIncompleteException,
    ProtectionSyncIncompleteException,
)
from .models import Order, Position, ProtectionSync

if TYPE_CHECKING:
    from .hyperliquid import HyperliquidClient

logger = logging.getLogger("fractrade_hl_simple")

Level = Tuple[str, str, float, Decimal]  # (name, kind, price, fraction of the position or size)


def protection_legs(kind: str, price: Optional[float],
                    levels: Optional[Sequence[Tuple[float, float]]]) -> List[Level]:
    """(name, kind, price, fraction) per stop loss or take profit of an open_*_position call:
    the single price (the whole position, named `kind`) or the list (named kind_1, kind_2,
    ...). Raises InvalidArgumentException for both, a price that is not a positive number, a
    fraction outside (0, 1], or fractions adding up to more than 1."""
    if price is not None and levels is not None:
        raise InvalidArgumentException(f"pass {kind}_price or {kind}s, not both")
    if price is not None:
        if not _inputs.positive_number(price):
            raise InvalidArgumentException(f"{kind}_price must be a positive number, got {price!r}")
        return [(kind, kind, price, Decimal(1))]
    if levels is None:
        return []
    legs: List[Level] = []
    for number, level in enumerate(levels, start=1):
        try:
            level_price, fraction_value = level
        except (TypeError, ValueError):
            raise InvalidArgumentException(f"{kind}s: each is (price, fraction), got {level!r}") from None
        if not _inputs.positive_number(level_price):
            raise InvalidArgumentException(f"{kind}s: price must be a positive number, got {level_price!r}")
        fraction = Decimal(str(fraction_value)) if isinstance(fraction_value, (int, float)) and \
            not isinstance(fraction_value, bool) else Decimal("NaN")
        if not fraction.is_finite() or not 0 < fraction <= 1:
            raise InvalidArgumentException(f"{kind}s: fraction must be above 0 and at most 1, got {fraction_value!r}")
        legs.append((f"{kind}_{number}", kind, level_price, fraction))
    if not legs:
        raise InvalidArgumentException(f"{kind}s is empty: pass at least one, or none")
    if sum(fraction for _, _, _, fraction in legs) > 1:
        raise InvalidArgumentException(f"{kind}s: the fractions add up to more than 1")
    return legs


def split_size(size: Decimal, legs: Sequence[Tuple[str, str, Any, Decimal]], size_decimals: int,
               is_long: bool) -> List[Decimal]:
    """The size of each leg (name, kind, price, fraction) of a long or short position: its
    fraction of `size`, rounded down to the lot size; per kind, the leg nearest the entry (the
    first the price reaches: a long's highest stop loss and lowest take profit) takes what
    rounding left, so the legs of a kind cover their share (their fractions' sum of `size`,
    rounded down) exactly. Given to the nearest stop, the cheapest per unit, the loss if
    every stop fills (moves and fees, and slippage estimated for all the stops together)
    only grows with `size`: a partial fill never loses more at its stops than the full size."""
    step = Decimal(1).scaleb(-size_decimals)
    sizes = [(size * fraction).quantize(step, ROUND_DOWN) for _, _, _, fraction in legs]
    for kind in ("stop_loss", "take_profit"):
        indexes = [i for i, leg in enumerate(legs) if leg[1] == kind]
        if indexes:
            share = (size * sum(legs[i][3] for i in indexes)).quantize(step, ROUND_DOWN)
            highest = (kind == "stop_loss") == is_long  # a long's stops, a short's take profits: below the price
            nearest = max(indexes, key=lambda i: Decimal(str(legs[i][2]))) if highest else \
                min(indexes, key=lambda i: Decimal(str(legs[i][2])))
            sizes[nearest] += share - sum(sizes[i] for i in indexes)
    return sizes


def _missing_levels(levels: Sequence[Level]) -> List[Dict[str, Any]]:
    """The levels (name, kind, price, size) not in place, for the exceptions' missing."""
    return [{"leg": name, "kind": kind, "price": price, "size": leg_size.normalize(), "outcome_unknown": False,
             "cloid": None} for name, kind, price, leg_size in levels if leg_size > 0]


def _missing(legs: Sequence[Level], sizes: Sequence[Decimal]) -> List[Dict[str, Any]]:
    """The legs (name, kind, price, fraction) of an open_position not placed, at these sizes."""
    return _missing_levels([(name, kind, price, leg_size) for (name, kind, price, _), leg_size in zip(legs, sizes)])


def open_position(
    client: HyperliquidClient,
    symbol: str,
    size: float,
    is_long: bool,
    stop_loss_price: Optional[float],
    take_profit_price: Optional[float],
    limit_price: Optional[float],
    slippage: Optional[float],
    stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
    take_profits: Optional[Sequence[Tuple[float, float]]] = None,
    time_in_force: Optional[TimeInForce] = None,
) -> Dict[str, Order]:
    """Shared implementation of open_long_position and open_short_position."""
    if not client.is_authenticated():
        raise AuthenticationRequiredException()
    if time_in_force is not None and (limit_price is None or time_in_force not in _inputs.TIMES_IN_FORCE):
        raise InvalidArgumentException("time_in_force is Gtc, Ioc or Alo, and only with limit_price")
    legs = (protection_legs("stop_loss", stop_loss_price, stop_losses)
            + protection_legs("take_profit", take_profit_price, take_profits))
    client._check_order_size(symbol, size)
    return _open_with_levels(client, symbol, size, is_long, legs, limit_price, slippage, time_in_force)


def _open_with_levels(
    client: HyperliquidClient,
    symbol: str,
    size: float,
    is_long: bool,
    legs: List[Level],
    limit_price: Optional[float],
    slippage: Optional[float],
    time_in_force: Optional[TimeInForce] = None,
) -> Dict[str, Order]:
    """Body of open_position. `legs`: (name, kind, price, fraction) per stop loss and
    take profit."""
    # Everything is checked before anything is placed, at the prices that will be sent:
    # the stop loss and take profit against the mid price when the entry executes at once
    # (market, or a limit with Ioc; its own limit includes the slippage allowance, which is
    # no price it fills at), against its limit price when it rests. A resting entry takes one
    # stop loss and one take profit, attached to it for its whole size: Hyperliquid places
    # them once it fills (its normalTpsl grouping, as the SDK's example sends it).
    resting = limit_price is not None and time_in_force != "Ioc"
    if resting and legs:
        kinds = [kind for _, kind, _, _ in legs]
        if len(set(kinds)) != len(kinds) or any(fraction != 1 for _, _, _, fraction in legs):
            raise InvalidArgumentException(
                "a resting limit entry takes one stop loss and one take profit, each for its whole size "
                "(stop_loss_price, take_profit_price); several levels need an entry that executes at once")
    entry_price: float
    if limit_price is not None and resting:
        entry_price = limit_price
        time_in_force = time_in_force or "Gtc"
        reference = limit_price
    else:
        reference = client._mid_price(symbol)
        entry_price = limit_price if limit_price is not None else client._with_slippage(reference, is_long, slippage)
        time_in_force = "Ioc"
    entry_px = client._format_price(symbol, reference)
    decimals = client.market_specs[symbol].get("size_decimals", 0)
    # The levels at their prices as sent (rounding can make two equal: the remainder then
    # goes to the same one as the MCP server's plan), and the size as the entry is sent.
    sent = [(name, kind, client._format_price(symbol, price), fraction) for name, kind, price, fraction in legs]
    planned = split_size(Decimal(str(_inputs.round_down(size, decimals))), sent, decimals, is_long)
    for (name, kind, price, _), leg_size in zip(legs, planned):
        label = name.replace("_", " ").capitalize()
        px = client._format_price(symbol, price)
        if not _inputs.positive_number(px):  # rounds to 0
            raise InvalidArgumentException(f"{label} price rounds to {px} at {symbol}'s price precision")
        below = (kind == "stop_loss") == is_long  # a long's stop and a short's take profit: below
        if (px >= entry_px) if below else (px <= entry_px):
            raise InvalidArgumentException(
                f"{label} price must be {'below' if below else 'above'} entry price for "
                f"{'longs' if is_long else 'shorts'}")
        # Hyperliquid refuses an order worth less than 10 USD (reduce-only ones closing a
        # whole position may be exempt; nothing here relies on it): refused now, not after
        # the entry filled.
        if leg_size * Decimal(str(px)) < _inputs.MIN_ORDER_USD:
            raise InvalidArgumentException(
                f"{label} would be {_inputs.plain(leg_size)} at {_inputs.plain(px)}, worth less than Hyperliquid's "
                f"minimum order of "
                f"{_inputs.MIN_ORDER_USD} USD")

    if resting and legs:
        return _with_attached(client, symbol, is_long, size, entry_price, time_in_force, sent)
    entry = client.create_order(
        symbol, size, is_long, limit_price=entry_price, time_in_force=time_in_force
    )
    orders = {"entry": entry}
    if not legs:
        return orders
    if entry.status != "filled":  # an Ioc entry is filled (partly) or rejected; kept as a safeguard
        raise ProtectionOrderException(legs[0][0], orders, _missing(legs, planned)) from ValueError(
            "the entry order is resting, not filled, so there is no position to protect "
            "yet; place the stop loss and take profit after it fills"
        )

    # A market entry can fill only partly: protect the filled size.
    filled = Decimal(str(entry.filled_size)) if entry.filled_size else Decimal(str(size))
    sizes = split_size(filled, sent, decimals, is_long)
    try:
        # One read for all levels: the position must be on the entry's side (an opposite
        # position the entry only reduced has nothing these orders protect).
        closing_is_buy = client._closing_side(symbol, not is_long)
    except Exception as exc:
        raise ProtectionOrderException(legs[0][0], orders, _missing(legs, sizes)) from exc
    for index, ((name, kind, price, _), leg_size) in enumerate(zip(legs, sizes)):
        if leg_size <= 0:  # a fraction too small for the filled size: its part went to the nearest of its kind
            logger.warning("%s: %s at %s rounds to 0 for the filled size %s and is not placed",
                           symbol, name, price, filled)
            continue
        try:
            placed = client._place_trigger("sl" if kind == "stop_loss" else "tp", symbol, float(leg_size),
                                           price, closing_is_buy, _inputs.order_cloid(None))
        except Exception as exc:
            missing = _missing(legs[index:], sizes[index:])
            if isinstance(exc, OrderOutcomeUnknownException) and missing:  # it may still arrive
                missing[0].update(outcome_unknown=True, cloid=exc.cloid)
            raise ProtectionOrderException(name, orders, missing) from exc
        orders[name] = placed
        if placed.status != "open":  # it executed at once: the price had already crossed it
            left = leg_size - Decimal(str(placed.filled_size or 0))
            rest = _missing(legs[index:index + 1], [left]) + _missing(legs[index + 1:], sizes[index + 1:])
            raise ProtectionOrderException(name, orders, rest) from ValueError(
                f"the {name.replace('_', ' ')} at {price} executed at once ({placed.status}, "
                f"{placed.filled_size} of {leg_size}): the price had already crossed it; the rest of the "
                "position has not all its protection")
    return orders


def trigger_order(client: HyperliquidClient, symbol: str, is_buy: bool, size: float, trigger_price: float,
                  limit_price: Optional[float], cloid: Optional[str]) -> Order:
    """HyperliquidClient.trigger_order()."""
    if not client.is_authenticated():
        raise AuthenticationRequiredException()
    client._check_order_size(symbol, size)
    for name, price in (("trigger_price", trigger_price), ("limit_price", limit_price)):
        if price is not None and not _inputs.positive_number(price):
            raise InvalidArgumentException(f"{name} must be a positive number, got {price!r}")
    size, trigger = client._validate_and_format_order(symbol, size, trigger_price)
    side, above = ("buy", "above") if is_buy else ("sell", "below")
    # Hyperliquid's rule for a stop order: a buy's trigger above the mid, a sell's below. The
    # trigger itself is the mark price: one already past it would execute at once.
    mid = client._mid_price(symbol)
    if (trigger <= mid) if is_buy else (trigger >= mid):
        raise InvalidArgumentException(
            f"a {side} trigger_price must be {above} the mid price {mid}: a stop order buys on a rise and "
            f"sells on a fall (to {side} at a better price than now, place a limit order)")
    mark = _mark_price(client, symbol)
    if (trigger <= mark) if is_buy else (trigger >= mark):
        raise InvalidArgumentException(f"the mark price {mark} is already past trigger_price {trigger}: "
                                       "the order would trigger at once")
    if limit_price is None:
        worst, is_market = client._market_trigger_limit(symbol, trigger, is_buy), True
    else:
        worst, is_market = client._format_price(symbol, limit_price), False
        if (worst < trigger) if is_buy else (worst > trigger):
            raise InvalidArgumentException(f"a {side}'s limit_price must be at or {above} trigger_price {trigger}, "
                                           f"got {worst}")
    if Decimal(str(size)) * Decimal(str(trigger)) < _inputs.MIN_ORDER_USD:
        raise InvalidArgumentException(
            f"the entry would be {_inputs.plain(size)} at {_inputs.plain(trigger)}, worth less than Hyperliquid's "
            f"minimum order of {_inputs.MIN_ORDER_USD} USD")
    # A stop entry is sent on its own: Hyperliquid refuses a trigger order as the main order
    # of a group with attached TP/SL ("Main order cannot be trigger order.", seen live).
    order_cloid = _inputs.order_cloid(cloid)
    order_type = {"trigger": {"triggerPx": trigger, "isMarket": is_market, "tpsl": "sl"}}
    response = client._place_order(symbol, order_cloid, is_buy=is_buy, sz=size, limit_px=worst, reduce_only=False,
                                   order_type=order_type)
    return _answers.order_from_response(response, symbol, is_buy, size, worst, order_type, False,
                                        f"{symbol} trigger order", cloid=order_cloid)


def _mark_price(client: HyperliquidClient, symbol: str) -> float:
    """The mark price of a perp market, which triggers stop and take-profit orders."""
    dex = symbol.split(":")[0] if ":" in symbol else ""
    _, markets = client._markets_with_contexts(client.info, dex)
    context = next((ctx for market, ctx in markets if market.get("name") == symbol), None)
    mark = None if context is None else context.get("markPx")
    if mark is None or not _inputs.positive_number(float(mark)):
        raise HyperliquidException(f"no mark price for {symbol}")
    return float(mark)


# The answers to an attached order of a normalTpsl group: Hyperliquid's documented ones, and the
# official Rust SDK's (ExchangeDataStatus) for one waiting for its parent to fill.
_ATTACHED = frozenset({"success", "waitingForFill", "waitingForTrigger"})


def _with_attached(client: HyperliquidClient, symbol: str, is_long: bool, size: float, price: float,
                   time_in_force: Optional[TimeInForce], levels: List[Level]) -> Dict[str, Order]:
    """A resting limit entry with its stop loss and take profit attached, in one action. The
    attached ones are reduce-only market triggers for the entry's size on the closing side.
    Returns {"entry": Order}: the attached orders are placed by Hyperliquid once it fills."""
    size, price = client._validate_and_format_order(symbol, size, price)
    tif = time_in_force or "Gtc"
    entry_cloid = _inputs.order_cloid(None)
    requests: List[Dict[str, Any]] = [
        {"coin": symbol, "is_buy": is_long, "sz": size, "limit_px": price,
         "order_type": {"limit": {"tif": tif}}, "reduce_only": False, "cloid": entry_cloid}]
    # In the order of the SDK's example (examples/basic_tpsl.py): the parent, then the take
    # profit, then the stop loss; each with its limit beyond its trigger (_market_trigger_limit).
    levels = sorted(levels, key=lambda level: level[1] != "take_profit")
    for _, kind, level_price, _ in levels:
        requests.append({"coin": symbol, "is_buy": not is_long, "sz": size,
                         "limit_px": client._market_trigger_limit(symbol, level_price, not is_long),
                         "order_type": {"trigger": {"triggerPx": level_price, "isMarket": True,
                                                    "tpsl": "sl" if kind == "stop_loss" else "tp"}},
                         "reduce_only": True, "cloid": _inputs.order_cloid(None)})
    action = f"{symbol} order"
    attached: List[Any]
    try:
        response = client._signed(action, client.exchange.bulk_orders, requests, grouping="normalTpsl")
        if isinstance(response, dict) and response.get("status") == "err":  # refused as a whole
            raise OrderRejectedException(action, str(response.get("response")))
        statuses = _answers.status_list(response)
        refused = _answers.batch_error(statuses, len(requests))
        if refused is not None:
            raise OrderRejectedException(action, refused)
        if statuses is None or len(statuses) != len(requests) or not _answers.is_documented_status(statuses[0]):
            raise ActionOutcomeUnknownException(action, f"answered in no documented format: {response}")
        entry_status, attached = statuses[0], statuses[1:]
    except ActionOutcomeUnknownException as exc:  # the entry is looked up by its cloid, never sent again
        entry_status, attached = client._reconcile_order(symbol, entry_cloid, exc), [None] * len(levels)
    answer = {"status": "ok", "response": {"type": "order", "data": {"statuses": [entry_status]}}}
    entry = _answers.order_from_response(answer, symbol, is_long, size, price, requests[0]["order_type"], False,
                                         action, cloid=entry_cloid)
    missing: List[Dict[str, Any]] = []
    for (name, kind, level_price, _), request, status in zip(levels, requests[1:], attached):
        documented = _answers.is_documented_status(status)
        if (isinstance(status, str) and status in _ATTACHED) or (documented and "error" not in status):
            continue
        unknown = not documented  # a documented {"error": text} is a refusal; anything else is not known
        missing.append({"leg": name, "kind": kind, "price": level_price, "size": Decimal(str(size)).normalize(),
                        "outcome_unknown": unknown, "cloid": request["cloid"].to_raw() if unknown else None})
    if missing:
        raise ProtectionOrderException(missing[0]["leg"], {"entry": entry}, missing) from ValueError(
            f"the {missing[0]['leg'].replace('_', ' ')} attached to entry {entry.order_id} was not confirmed: "
            f"{attached}")
    return {"entry": entry}


def replace(client: HyperliquidClient, symbol: str, new_price: float, kind: str) -> Order:
    """Shared implementation of update_stop_loss and update_take_profit.

    Modify is not used: it cannot change trigger orders under the exchange's
    always-place rule, and a modify whose response is lost cannot be told apart from
    one that failed.
    """
    # One replacement at a time per signing key: two concurrent ones would both cancel the
    # same old order and leave two new ones.
    with _signing.signer_state(client.exchange_account.address).operation_lock:
        position = next((p for p in client.get_positions() if p.symbol == symbol and p.size != 0), None)
        if position is None:
            raise PositionNotFoundException(f"No position found for {symbol}")
        # The position's own stops (take profits) are on the side that closes it; one on the
        # other side opens or adds to a position, an entry, and is left alone.
        old_orders = [order for order in client.get_open_orders(symbol)
                      if order.type == kind and order.is_buy == position.is_short]

        place = client.stop_loss if kind == "stop_loss" else client.take_profit
        # The side of the position read above: if it flipped since, the order is refused.
        new_order = place(symbol, abs(float(position.size)), new_price, is_buy=position.is_short)

        # The new order must protect the whole position now: it may have executed at once
        # (partly), triggered since, or the position may have grown meanwhile. Only then are
        # the old orders removed; without old orders, a gap is reported all the same. The
        # order is read after the position: one that triggers in between is then not open.
        old_ids = [o.order_id for o in old_orders]
        kept = "were kept: " if old_ids else ""
        try:
            position = next(
                (p for p in client.get_positions() if p.symbol == symbol and p.size != 0), None
            )
            live = next((o for o in client.get_open_orders(symbol) if o.order_id == new_order.order_id), None)
        except Exception as exc:
            raise ProtectionReplacementIncompleteException(
                new_order, old_ids,
                f"{kept}the position and the new order could not be read to check it covers the position",
            ) from exc
        if position is not None and not (
            live is not None and live.remaining_size >= abs(position.size)
            and live.is_buy == position.is_short  # still the closing side: it may have flipped
        ):
            raise ProtectionReplacementIncompleteException(
                new_order, old_ids, f"{kept}the new order does not cover the whole position",
                position=position,
            )

        not_canceled = []
        for order in old_orders:
            try:
                client._cancel_one(symbol, order.order_id)
            except Exception as exc:
                logger.warning(f"Failed to cancel old {kind} order {order.order_id} for {symbol}: {exc}")
                not_canceled.append(order.order_id)
        if not_canceled:
            raise ProtectionReplacementIncompleteException(new_order, not_canceled)
        return new_order


def resize(client: HyperliquidClient, symbol: str, stop_losses: Optional[Sequence[Tuple[float, float]]],
           take_profits: Optional[Sequence[Tuple[float, float]]], take_profit_type: str) -> ProtectionSync:
    """HyperliquidClient.resize_protection()."""
    if not client.is_authenticated():
        raise AuthenticationRequiredException()
    if stop_losses is None and take_profits is None:
        raise InvalidArgumentException("pass stop_losses, take_profits or both")
    if take_profit_type not in ("trigger", "limit"):
        raise InvalidArgumentException(f"take_profit_type is 'trigger' or 'limit', got {take_profit_type!r}")
    legs = protection_legs("stop_loss", None, stop_losses) + protection_legs("take_profit", None, take_profits)
    limit = take_profit_type == "limit"
    with _signing.signer_state(client.exchange_account.address).operation_lock:
        position = client._position(symbol)
        if position is None:
            raise PositionNotFoundException(f"No position found for {symbol}")
        is_long = position.size > 0
        size = abs(position.size)
        decimals = client.market_specs[symbol].get("size_decimals", 0)
        sent = [(name, kind, client._format_price(symbol, price), fraction) for name, kind, price, fraction in legs]
        sizes = split_size(size, sent, decimals, is_long)
        _check_levels(client, symbol, sent, sizes, is_long)
        plan = [(name, kind, price, leg_size) for (name, kind, price, _), leg_size in zip(sent, sizes)]
        closing_is_buy = not is_long
        ours = [o for o in client.get_open_orders(symbol) if o.reduce_only and o.is_buy == closing_is_buy]
        untouched = [o for o in ours if o.is_position_tpsl or o.children]
        movable = [o for o in ours if not (o.is_position_tpsl or o.children)]
        if take_profits is not None and limit:
            # Hyperliquid keeps the resting reduce-only limits within the position: the new
            # ones and those left untouched must fit, or it would cancel one of them itself.
            resting = sum((o.remaining_size for o in untouched if o.type == "limit"), Decimal(0))
            asked = sum((leg_size for _, kind, _, leg_size in plan if kind == "take_profit"), Decimal(0))
            if resting and asked + resting > size:
                raise InvalidArgumentException(
                    f"the take profit limits ({asked}) and the resting reduce-only limits left untouched "
                    f"({resting}) would exceed the position ({size}): Hyperliquid would cancel one of them")
        done = _Sync(client=client, symbol=symbol, closing_is_buy=closing_is_buy, start_size=position.size,
                     limit=limit)
        # Each kind's steps are worked out before anything is sent, so a failure can name
        # every level still missing and every old order still open.
        steps = []
        if stop_losses is not None:
            steps.append(done.step("stop_loss", plan, [o for o in movable if o.type == "stop_loss"],
                                   place_first=True))
        if take_profits is not None:
            old = [o for o in movable if o.type in ("take_profit", "limit")]
            steps.append(done.step("take_profit", plan, old,
                                   place_first=not limit and not any(o.type == "limit" for o in old)))
        for kind, to_place, left, place_first in steps:
            if place_first:
                done.place_levels(to_place)
                if left:  # the old ones go only once every level of the kind is open with its size
                    done.check_targets({kind})
                done.cancel_old(left, stop=False)
            else:
                done.cancel_old(left, stop=True)
                done.place_levels(to_place)
        done.check_targets({kind for kind, *_ in steps})
        if done.not_canceled:
            raise done.incomplete(f"old orders {', '.join(map(str, done.not_canceled))} could not be cancelled")
        return ProtectionSync(symbol=symbol, position_size=position.size, placed=done.placed, kept=done.kept,
                              canceled=done.canceled, untouched=untouched)


def _check_levels(client: HyperliquidClient, symbol: str, sent: Sequence[Level], sizes: Sequence[Decimal],
                  is_long: bool) -> None:
    """resize_protection's checks before anything is sent (InvalidArgumentException)."""
    mid = client._mid_price(symbol)
    seen: Set[Tuple[str, float]] = set()
    for (name, kind, price, _), leg_size in zip(sent, sizes):
        label = name.replace("_", " ")
        if not _inputs.positive_number(price):
            raise InvalidArgumentException(f"{label}: the price rounds to {price} at {symbol}'s price precision")
        if (kind, price) in seen:
            raise InvalidArgumentException(f"{label}: another {kind.replace('_', ' ')} has the price {price}")
        seen.add((kind, price))
        below = (kind == "stop_loss") == is_long  # a long's stop loss and a short's take profit: below the price
        if (price >= mid) if below else (price <= mid):
            raise InvalidArgumentException(
                f"{label} at {price} must be {'below' if below else 'above'} the mid price {mid} for a "
                f"{'long' if is_long else 'short'}")
        if leg_size <= 0:
            raise InvalidArgumentException(f"{label}: its fraction of the position rounds to 0 lots")
        if leg_size * Decimal(str(price)) < _inputs.MIN_ORDER_USD:
            raise InvalidArgumentException(
                f"{label} would be {_inputs.plain(leg_size)} at {_inputs.plain(price)}, worth less than "
                f"Hyperliquid's minimum order of "
                f"{_inputs.MIN_ORDER_USD} USD")


def _protects(order: Order, closing_is_buy: bool, kind: str, price: float, leg_size: Decimal,
              limit: bool) -> bool:
    """Whether an open order is this level: reduce-only on the closing side, the order kind
    and execution, the price (a trigger's trigger price, a limit's limit price) and the
    size still open."""
    if not order.reduce_only or order.is_buy != closing_is_buy or order.remaining_size != leg_size:
        return False
    if kind == "stop_loss" or not limit:
        wanted = "stop_loss" if kind == "stop_loss" else "take_profit"
        trigger = order.order_type.trigger if order.order_type else None
        return (order.type == wanted and bool(trigger and trigger.get("isMarket"))
                and order.trigger_price == Decimal(str(price)))
    return order.type == "limit" and order.limit_price == Decimal(str(price))


@dataclass
class _Sync:
    """One resize_protection call: its steps, and what they have done so far, for its result
    or its exception. A level is (name, kind, price, size)."""
    client: HyperliquidClient
    symbol: str
    closing_is_buy: bool
    start_size: Decimal
    limit: bool                                                   # take profits as resting limits
    placed: List[Order] = field(default_factory=list)
    kept: List[Order] = field(default_factory=list)
    canceled: List[int] = field(default_factory=list)
    not_canceled: List[int] = field(default_factory=list)
    to_cancel: List[int] = field(default_factory=list)           # the old orders that match no level
    pending: List[Level] = field(default_factory=list)            # levels not placed (yet)
    targets: List[Tuple[Level, int]] = field(default_factory=list)  # (level, its order id)

    @property
    def retained(self) -> List[int]:
        """The old orders not cancelled yet: still open."""
        return [i for i in self.to_cancel if i not in self.canceled and i not in self.not_canceled]

    def incomplete(self, reason: str, *, missing: Optional[Sequence[Level]] = None,
                   rows: Optional[List[Dict[str, Any]]] = None,
                   position: Optional[Position] = None) -> ProtectionSyncIncompleteException:
        """The exception for what was done so far; missing: the levels not in place (default:
        those not placed yet), or rows already made (with an unknown outcome's cloid)."""
        if rows is None:
            rows = _missing_levels(self.pending if missing is None else missing)
        return ProtectionSyncIncompleteException(
            reason, placed=self.placed, kept=self.kept, canceled=self.canceled, not_canceled=self.not_canceled,
            retained=self.retained, missing=rows, position=position)

    def step(self, kind: str, plan: List[Level], old: List[Order], *,
             place_first: bool) -> Tuple[str, List[Level], List[Order], bool]:
        """One kind's steps: the old orders that already are a level (kept), the levels to
        place and the old orders to cancel; recorded here as pending and to cancel."""
        left = list(old)
        to_place = []
        for level in (p for p in plan if p[1] == kind):
            match = next((o for o in left
                          if _protects(o, self.closing_is_buy, kind, level[2], level[3], self.limit)), None)
            if match is not None:
                self.kept.append(match)
                self.targets.append((level, match.order_id))
                left.remove(match)
            else:
                to_place.append(level)
        self.pending.extend(to_place)
        self.to_cancel.extend(o.order_id for o in left)
        return kind, to_place, left, place_first

    def place_levels(self, levels: List[Level]) -> None:
        for level in levels:
            name, kind, price, leg_size = level
            try:
                if kind == "take_profit" and self.limit:
                    placed = self.client.create_order(self.symbol, float(leg_size), self.closing_is_buy,
                                                      limit_price=price, reduce_only=True, time_in_force="Alo")
                else:
                    placed = self.client._place_trigger("sl" if kind == "stop_loss" else "tp", self.symbol,
                                                        float(leg_size), price, self.closing_is_buy,
                                                        _inputs.order_cloid(None))
            except Exception as exc:
                rows = _missing_levels(self.pending)  # this level first, then every one not placed yet
                if isinstance(exc, OrderOutcomeUnknownException) and rows:  # it may still arrive
                    rows[0].update(outcome_unknown=True, cloid=exc.cloid)
                raise self.incomplete(f"the {name.replace('_', ' ')} at {price} was not placed: {exc}",
                                      rows=rows) from exc
            self.pending.remove(level)
            self.placed.append(placed)
            if placed.status != "open":  # executed at once (partly): the price had already crossed it
                rest = leg_size - Decimal(str(placed.filled_size or 0))
                missing = ([(name, kind, price, rest)] if rest > 0 else []) + self.pending
                raise self.incomplete(f"the {name.replace('_', ' ')} at {price} executed at once ({placed.status}, "
                                      f"{placed.filled_size} of {leg_size})", missing=missing)
            self.targets.append((level, placed.order_id))

    def cancel_old(self, orders: List[Order], *, stop: bool) -> None:
        """Cancel the old orders; with `stop`, the first that cannot be cancelled ends it (the
        new take-profit limits are then not placed: with it they could exceed the position)."""
        for order in orders:
            try:
                self.client._cancel_one(self.symbol, order.order_id)
            except Exception as exc:
                logger.warning(f"Failed to cancel old order {order.order_id} for {self.symbol}: {exc}")
                self.not_canceled.append(order.order_id)
                if stop:
                    raise self.incomplete(f"old order {order.order_id} could not be cancelled, so the new take "
                                          "profits were not placed") from exc
            else:
                self.canceled.append(order.order_id)

    def position_after(self) -> Optional[Position]:
        """The position read again (None: flat now); raises the incomplete exception if it
        cannot be read."""
        try:
            return self.client._position(self.symbol)
        except Exception as exc:
            raise self.incomplete("the position could not be read to check the orders") from exc

    def check_targets(self, kinds: Set[str]) -> None:
        """Every level of `kinds` placed or kept so far open, by its order id, with its size,
        on the position as it was."""
        try:
            live = {o.order_id: o for o in self.client.get_open_orders(self.symbol)}
        except Exception as exc:
            raise self.incomplete("the open orders could not be read to check the levels") from exc
        position = self.position_after()
        gone = [level for level, order_id in self.targets if level[1] in kinds and not (
            order_id in live
            and _protects(live[order_id], self.closing_is_buy, level[1], level[2], level[3], self.limit))]
        if position is None or position.size != self.start_size:  # e.g. a new stop triggered and filled
            raise self.incomplete("the position changed while the orders were replaced", position=position,
                                  missing=gone + self.pending)
        if gone:
            raise self.incomplete(f"{', '.join(name.replace('_', ' ') for name, *_ in gone)} not open with its size",
                                  missing=gone + self.pending)
