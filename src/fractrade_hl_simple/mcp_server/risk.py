"""Trade outcomes from stop losses and take profits: what a position, or a planned trade,
makes or loses at each level, after fees and an estimate of slippage; what it makes if every
take profit fills and loses if every stop does; its risk/reward and the share of the account
at stake. Pure: no client, no request (tools/risk.py reads the data).

Estimates, as the result says:
- A stop loss or take profit that executes as a market order (a triggered stop, a market
  take profit) pays the taker fee and slippage. Slippage is the average price impact on
  today's order book (the visible levels) of all the stop losses together (and of all the
  take profits together), applied to each trigger price. With one rate for every unit of a
  kind, the loss at the stops only grows with the size, also when the rounding remainder
  moves between levels. An estimate, not a bound: stops firing one after another into a
  book that does not refill fill the later ones deeper than the average, and a book
  thinner when they trigger makes it worse.
- A trigger order with a limit (stop limit) is counted at its limit, the worst it fills at;
  once triggered it may not fill at all, which the warnings say where it matters.
- An order that rests (a limit take profit) fills at its price as a maker; it may not fill.
- Funding is not included.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional, Sequence

from ..hyperliquid import split_size


@dataclass(frozen=True)
class Book:
    """The visible order book: (price, size) per level, best first."""
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]

    @property
    def mid(self) -> Optional[Decimal]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


@dataclass(frozen=True)
class Exit:
    """A stop loss or take profit that closes (part of) the position."""
    kind: str                     # "stop_loss" or "take_profit"
    price: Decimal                # its trigger price, or the limit price of a resting order
    size: Decimal
    market: bool                  # executes as a market order: taker fee and slippage
    resting: bool = False         # a limit order already in the book: maker fee, no slippage
    order_id: Optional[int] = None
    limit: Optional[Decimal] = None  # a trigger order's limit (stop limit): it fills no worse


@dataclass(frozen=True)
class Fees:
    taker: Decimal
    maker: Decimal


def impact(book: Book, is_buy: bool, size: Decimal) -> Optional[Decimal]:
    """The price impact of a market order of `size`, as a fraction of the mid: the average
    price it would get walking the visible book (a buy walks the asks) against the mid. None
    if the visible book holds less than `size`, or has no mid."""
    mid = book.mid
    if mid is None or mid <= 0 or size <= 0:
        return None if mid is None or mid <= 0 else Decimal(0)
    left, cost = size, Decimal(0)
    for price, available in (book.asks if is_buy else book.bids):
        take = min(left, available)
        cost += take * price
        left -= take
        if left <= 0:
            average = cost / size
            return (average - mid) / mid if is_buy else (mid - average) / mid
    return None


_OWN = object()  # exit_outcome: estimate the exit's impact on its own


def exit_outcome(is_long: bool, entry: Decimal, exit: Exit, fees: Fees, book: Book,
                 slip: Any = _OWN) -> dict[str, Any]:
    """One exit level: its PnL at its price, the estimated slippage (what the execution costs
    against that price; negative if it is better), the fee, the net PnL from the executed
    price (rounded to the cent for showing; `net_exact` unrounded, for totals). `slip`: the
    price impact of a market exit, if outcomes estimated it for exits sharing a trigger; None
    there means not estimable."""
    closing_buy = not is_long
    if exit.market:
        slip = impact(book, closing_buy, exit.size) if slip is _OWN else slip
        executed = None if slip is None else (exit.price * (1 + slip) if closing_buy else exit.price * (1 - slip))
    elif exit.resting:
        executed = exit.price
    else:  # a triggered limit: at its limit (at worst, or better if the limit is on the far side)
        executed = exit.price if exit.limit is None else exit.limit
    gross = (exit.price - entry) * exit.size if is_long else (entry - exit.price) * exit.size
    at = exit.price if executed is None else executed  # not estimable: at its price, flagged
    slippage = None if executed is None else ((executed - exit.price) if closing_buy else (exit.price - executed)) * exit.size
    rate = fees.maker if exit.resting else fees.taker
    fee = rate * at * exit.size
    net = ((at - entry) if is_long else (entry - at)) * exit.size - fee
    return {"kind": exit.kind, "order_id": exit.order_id, "price": exit.price, "size": exit.size,
            "executes_as": ("limit (maker)" if exit.resting else "market (taker)" if exit.market
                            else f"limit {executed} once triggered (taker, at worst)"),
            "gross_pnl": _usd(gross), "fee": _usd(fee), "slippage": None if slippage is None else _usd(slippage),
            "net_pnl": _usd(net), "net_exact": net}


def outcomes(*, is_long: bool, entry: Decimal, size: Decimal, exits: list[Exit], fees: Fees, book: Book,
             account_value: Optional[Decimal], entry_fee: Decimal = Decimal(0),
             liquidation_price: Optional[Decimal] = None, max_daily_loss_pct: Optional[Decimal] = None,
             ) -> dict[str, Any]:
    """The outcomes of a position of `size` at `entry` with its `exits`: per level, if every
    stop fills (the stops nearest the price first, up to the position's size: they are
    reduce-only) and if every take profit does, risk/reward, break-even win rate, warnings.
    `entry_fee` is counted in both totals (a planned trade's entry; 0 for an open position,
    whose entry fee is paid)."""
    warnings: list[str] = []
    mid = book.mid
    # In the order the price reaches them: a long's stops from the highest down, its take
    # profits from the lowest up; a short's the other way round.
    stops = _covering([e for e in exits if e.kind == "stop_loss"], size, ascending=not is_long, warnings=warnings)
    profits = _covering([e for e in exits if e.kind == "take_profit"], size, ascending=is_long, warnings=warnings)
    levels, slips = _levels(is_long, entry, stops + profits, fees, book)
    stop_levels, profit_levels = levels[:len(stops)], levels[len(stops):]
    if any(e.market and slips[e.kind] is None for e in stops + profits):
        warnings.append("an exit is larger than the visible order book: its slippage is not estimated")
    stopped = sum((e.size for e in stops), Decimal(0))
    taken = sum((e.size for e in profits), Decimal(0))
    # Unrounded: rounding each level first would let the totals drift by cents.
    worst = sum((level.pop("net_exact") for level in stop_levels), Decimal(0)) - entry_fee * _share(stopped, size)
    best = sum((level.pop("net_exact") for level in profit_levels), Decimal(0)) - entry_fee * _share(taken, size)

    if not stops:
        warnings.append("no stop loss: the loss is not limited until liquidation")
    elif stopped < size:
        bare = size - stopped
        at_liquidation = ""
        if liquidation_price is not None:
            loss = abs(entry - liquidation_price) * bare
            at_liquidation = f" (at the liquidation price {liquidation_price} it alone loses {_usd(loss)} USD)"
        warnings.append(f"the stop losses cover {stopped} of {size}: {bare} has no stop{at_liquidation}")
    if profits and taken < size:
        warnings.append(f"the take profits cover {taken} of {size}: the rest stays open after them")
    for e in stops:
        if liquidation_price is not None and (e.price <= liquidation_price if is_long else e.price >= liquidation_price):
            warnings.append(f"the stop at {e.price} is beyond the liquidation price {liquidation_price}: "
                            "the position is liquidated first")
        if mid is not None and (e.price >= mid if is_long else e.price <= mid):
            warnings.append(f"the stop at {e.price} is already crossed by the market ({mid})")
        if not e.market and not e.resting and e.limit is not None and (e.limit > e.price if is_long
                                                                       else e.limit < e.price):
            warnings.append(f"the stop at {e.price} has a limit of {e.limit} on the far side of its trigger: once "
                            "triggered it may not fill, and then does not protect")
    for e in profits:
        if mid is not None and e.market and (e.price <= mid if is_long else e.price >= mid):
            warnings.append(f"the take profit at {e.price} is already crossed by the market ({mid})")

    risk_reward = None
    break_even = None
    if stops and worst < 0:
        if profits:
            risk_reward = _ratio(best / -worst)
            break_even = _percent(-worst / (-worst + best)) if best > 0 else None
    elif stops:
        warnings.append("the stops close in profit: the position cannot lose at them (before slippage)")
    loss_pct = None if account_value is None or account_value <= 0 or not stops else _percent(-worst / account_value)
    if (max_daily_loss_pct is not None and loss_pct is not None and Decimal(loss_pct) >= max_daily_loss_pct):
        warnings.append(f"if every stop fills, the loss ({loss_pct} % of the account) reaches max_daily_loss_pct "
                        f"({max_daily_loss_pct} %)")
    return {
        "side": "long" if is_long else "short", "size": size, "entry_price": entry, "mid_price": mid,
        "stop_losses": stop_levels, "take_profits": profit_levels,
        "if_all_stops": None if not stops else {"size": stopped, "net_pnl": _usd(worst), "percent_of_account": loss_pct},
        "if_all_take_profits": None if not profits else {
            "size": taken, "net_pnl": _usd(best),
            "percent_of_account": None if account_value is None or account_value <= 0 else _percent(best / account_value)},
        "risk_reward": risk_reward, "break_even_win_rate_percent": break_even,
        "unprotected_size": max(Decimal(0), size - stopped),
        "fees": {"taker": fees.taker, "maker": fees.maker, "entry_fee": _usd(entry_fee) if entry_fee else None},
        "warnings": warnings,
        "notes": "PnL after fees and estimated slippage (the price impact on today's order book, applied to "
                 "each trigger price; a thinner book in a fast market makes it worse). Funding is not included.",
    }


def planned_entry(size: Decimal, *, is_long: bool, limit: Optional[Decimal], book: Book) -> Optional[Decimal]:
    """A planned entry's price: a limit entry's limit; a market entry the mid moved by the
    price impact of `size` (None if the visible book holds less)."""
    if limit is not None:
        return limit
    mid, slip = book.mid, impact(book, is_long, size)
    if mid is None or slip is None:
        return None
    return mid * (1 + slip) if is_long else mid * (1 - slip)


def stop_legs(size: Decimal, stops: Sequence[tuple[Decimal, Decimal]], size_decimals: int,
              is_long: bool) -> list[Exit]:
    """The market stops of a planned position of `size`, (price, fraction) each, sized as
    open_long_position / open_short_position place them (the library's split_size). The
    prices as sent: rounded as Hyperliquid requires (the caller's job)."""
    legs = [(f"stop_loss_{i}", "stop_loss", price, fraction) for i, (price, fraction) in enumerate(stops, start=1)]
    return [Exit("stop_loss", price, leg_size, market=True)
            for (_, _, price, _), leg_size in zip(legs, split_size(size, legs, size_decimals, is_long)) if leg_size > 0]


def planned_loss(size: Decimal, *, is_long: bool, limit: Optional[Decimal], resting: bool,
                 stops: Sequence[tuple[Decimal, Decimal]], fees: Fees, book: Book, size_decimals: int,
                 ) -> Optional[Decimal]:
    """What a planned position of `size` loses if every stop (market stops, (price, fraction)
    each) fills: as outcomes() reports it, entry fee included for the share they close. None
    if it cannot be estimated (an entry or stops larger than the visible book)."""
    entry = planned_entry(size, is_long=is_long, limit=limit, book=book)
    if entry is None or size <= 0:
        return None
    exits = stop_legs(size, stops, size_decimals, is_long)
    levels, slips = _levels(is_long, entry, exits, fees, book)
    if any(slips[e.kind] is None for e in exits):
        return None
    stopped = sum((e.size for e in exits), Decimal(0))
    entry_fee = (fees.maker if resting else fees.taker) * entry * size
    loss: Decimal = -sum((level["net_exact"] for level in levels), Decimal(0)) + entry_fee * _share(stopped, size)
    return loss


def size_for_risk(*, is_long: bool, limit: Optional[Decimal], resting: bool,
                  stops: Sequence[tuple[Decimal, Decimal]], risk: Decimal, fees: Fees, book: Book,
                  size_decimals: int) -> tuple[Decimal, bool]:
    """A size at the market's lot size whose planned_loss is known and at most `risk`, as
    large as found, and whether the visible book limited it (the next lot not estimable).
    The loss grows with the size (each unit adds the move, the fees and one impact for all
    the stops, which grows with their total; rounding leaves its remainder with the nearest
    stop, the cheapest per unit), so a binary search over the lots finds it; the next few
    lots are checked too. Only sizes that were checked are returned."""
    step = Decimal(1).scaleb(-size_decimals)
    if (limit if limit is not None else book.mid) is None or risk <= 0 or not stops:
        return Decimal(0), False
    # No more than the visible book on the stops' side can be estimated (nor, for a market
    # entry, on the entry's side): an upper bound whatever the fees, rebates included.
    depth = sum((level[1] for level in (book.asks if not is_long else book.bids)), Decimal(0))
    if limit is None:
        depth = min(depth, sum((level[1] for level in (book.asks if is_long else book.bids)), Decimal(0)))

    def loss_at(lots: int) -> Optional[Decimal]:
        return planned_loss(lots * step, is_long=is_long, limit=limit, resting=resting, stops=stops, fees=fees,
                            book=book, size_decimals=size_decimals)

    top = int(depth / step)
    low, high = 0, top
    while low < high:
        lots = (low + high + 1) // 2
        loss = loss_at(lots)
        if loss is not None and loss <= risk:
            low = lots
        else:
            high = lots - 1
    # Rounding several stops' sizes moves lots between them: a few more lots can fit.
    for lots in range(low + 1, min(top, low + 2 * len(stops)) + 1):
        loss = loss_at(lots)
        if loss is not None and loss <= risk:
            low = lots
    # The visible book limited it if the next lot is not estimable (rather than too risky).
    return low * step, loss_at(low + 1) is None


def _levels(is_long: bool, entry: Decimal, exits: list[Exit], fees: Fees,
            book: Book) -> tuple[list[dict[str, Any]], dict[str, Optional[Decimal]]]:
    """exit_outcome of each exit, and the price impact per kind: the market stop losses
    execute together into the book (as in a fast move), and so do the market take profits."""
    together: dict[str, Decimal] = {}
    for e in exits:
        if e.market:
            together[e.kind] = together.get(e.kind, Decimal(0)) + e.size
    slips = {kind: impact(book, not is_long, size) for kind, size in together.items()}
    return [exit_outcome(is_long, entry, e, fees, book, slips[e.kind] if e.market else _OWN) for e in exits], slips


def _covering(exits: list[Exit], size: Decimal, *, ascending: bool, warnings: list[str]) -> list[Exit]:
    """The exits in the order the price reaches them (by price, `ascending` or not), cut to
    the position's size: Hyperliquid fills a reduce-only order only up to the position."""
    ordered = sorted(exits, key=lambda e: e.price, reverse=not ascending)
    covered, left = [], size
    for e in ordered:
        if left <= 0:
            warnings.append(f"the {e.kind.replace('_', ' ')} at {e.price} is beyond the position's size: "
                            "it would not fill")
            continue
        part = min(e.size, left)
        covered.append(replace(e, size=part))
        left -= part
    return covered


def _share(part: Decimal, whole: Decimal) -> Decimal:
    return part / whole if whole > 0 else Decimal(0)


def _usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _percent(value: Decimal) -> Decimal:
    return (value * 100).quantize(Decimal("0.01"))
