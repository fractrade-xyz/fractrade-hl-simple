"""The maker chase behind HyperliquidClient.maker_order(): post-only orders at the best price,
re-priced until the size is filled or the time is up.

The order of the steps is what keeps a fill from being bought or sold twice: nothing is sent
before the previous attempt was canceled and its fill counted, and an attempt whose outcome
is not known ends the chase with ActionOutcomeUnknownException.
"""
from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from . import _answers
from .exceptions import (
    ActionOutcomeUnknownException,
    AuthenticationRequiredException,
    HyperliquidException,
    InvalidArgumentException,
    MakerOrderIncompleteException,
    OrderRejectedException,
)
from .models import Order

if TYPE_CHECKING:
    from .hyperliquid import HyperliquidClient

logger = logging.getLogger("fractrade_hl_simple")

BboCache = Dict[str, Dict[str, float]]  # the caller's best bid and ask per symbol


def auto_timing(client: HyperliquidClient, symbol: str, bbo_cache: Optional[BboCache] = None) -> Tuple[float, float]:
    """(timeout, reprice_interval) for a maker chase, from the order book's spread."""
    try:
        cached = bbo_cache.get(symbol) if bbo_cache else None
        mid: Optional[float]
        spread: Optional[float]
        if cached and "bid" in cached and "ask" in cached:
            mid = (cached["bid"] + cached["ask"]) / 2
            spread = cached["ask"] - cached["bid"]
        else:
            book = client.get_order_book(symbol)
            mid = book.get("mid_price")
            spread = book.get("spread")
        if mid and spread and mid > 0:
            spread_bps = (spread / mid) * 10000
            if spread_bps < 2:       # Tight spread: BTC, ETH, SOL
                return (30.0, 3.0)
            elif spread_bps < 20:     # Medium: kPEPE, DOGE, LINK
                return (45.0, 5.0)
            else:                     # Wide spread: DYM, low-cap
                return (60.0, 8.0)
    except Exception as e:
        logger.debug(f"Auto-detect spread failed for {symbol}: {e}")
    return (60.0, 5.0)  # Safe defaults


def average_price(parts: Sequence[Tuple[Decimal, Optional[Decimal]]]) -> Optional[Decimal]:
    """Average price of (size, price) parts; None if a price is unknown or nothing filled."""
    qty = sum((q for q, _ in parts), Decimal(0))
    if not qty or any(px is None for _, px in parts):
        return None
    return sum((q * px for q, px in parts if px is not None), Decimal(0)) / qty


class MakerChase:
    """One maker_order() call. `run()` does it; the attributes are what it has done so far."""

    def __init__(self, client: HyperliquidClient, symbol: str, is_buy: bool, size: float,
                 timeout: Optional[float], reprice_interval: Optional[float], fallback: str, reduce_only: bool,
                 bbo_cache: Optional[BboCache], fill_event: Optional[threading.Event]) -> None:
        self.client = client
        self.symbol = symbol
        self.is_buy = is_buy
        self.requested = size  # as the caller passed it; `size` is what of it can be traded
        self.timeout = timeout
        self.reprice_interval = reprice_interval
        self.fallback = fallback
        self.reduce_only = reduce_only
        self.bbo_cache = bbo_cache
        self.fill_event = fill_event

        self.size = 0.0      # set when the chase starts, with the three below
        self.interval = 0.0  # seconds between re-pricings
        self.started = 0.0
        self.deadline = 0.0
        self.attempts = 0
        self.spread_bps: Optional[float] = None
        self.fills: List[Tuple[Decimal, Optional[Decimal]]] = []  # (size, price) filled by each attempt so far
        self.sent = False                    # whether any order of the chase was sent
        self.live: Optional[Order] = None    # the attempt in the book, until it is canceled and its fill counted
        self.last_order: Optional[Order] = None  # the last attempt that was placed

    def run(self) -> Order:
        """The final order; what maker_order() documents."""
        try:
            return self._chase()
        except ActionOutcomeUnknownException:
            raise  # its contract already: check the account before trying again
        except Exception as exc:
            # The library's own errors come from known steps, so `live` and `fills` describe
            # them. Any other one (a signal handler's, a bug) can come from any step, e.g. while
            # an order is being sent or its fill counted: once an order was sent, nothing is certain.
            live, foreign = self.live, not isinstance(exc, HyperliquidException)
            if live is None and not self.fills and not (foreign and self.sent):
                raise
            requested = Decimal(str(self.client._format_size(self.symbol, self.requested)))
            filled = self._filled()
            if live is not None:
                raise ActionOutcomeUnknownException(
                    f"{self.symbol} maker order {live.order_id} (cloid {live.cloid})",
                    f"the chase stopped ({exc!r}) while it was in the book: it may still be there or have "
                    f"filled; {filled} of {requested} filled by earlier attempts",
                ) from exc
            if foreign:
                raise ActionOutcomeUnknownException(
                    f"{self.symbol} maker order",
                    f"the chase was interrupted ({exc!r}) after sending orders: one may be in the book or "
                    f"have filled; {filled} of {requested} counted as filled",
                ) from exc
            raise MakerOrderIncompleteException(
                self.symbol, filled, average_price(self.fills), requested - filled) from exc

    def _chase(self) -> Order:
        client, symbol = self.client, self.symbol
        if not client.is_authenticated():
            raise AuthenticationRequiredException()
        if self.fallback not in ("ioc", "market", "cancel"):
            raise InvalidArgumentException("fallback must be 'ioc', 'market', or 'cancel'")
        # Invalid input (unknown symbol, size below the minimum) raises here, before anything is sent.
        client._check_order_size(symbol, self.requested)
        self.size = client._format_size(symbol, self.requested)  # e.g. 0.001009 -> 0.001

        timeout, reprice_interval = self.timeout, self.reprice_interval
        if timeout is None or reprice_interval is None:
            auto_timeout, auto_reprice = auto_timing(client, symbol, self.bbo_cache)
            if timeout is None:
                timeout = auto_timeout
            if reprice_interval is None:
                reprice_interval = auto_reprice
            logger.info(f"Maker order {symbol}: auto-detected timeout={timeout}s, reprice={reprice_interval}s")

        remaining = self.size
        self.interval = reprice_interval
        self.started = time.time()
        self.deadline = self.started + timeout

        while time.time() < self.deadline and remaining > 0:
            best_price = self._best_price()
            if best_price is None:
                logger.warning(f"No {'bid' if self.is_buy else 'ask'} in order book for {symbol}, retrying...")
                time.sleep(self._pause())
                continue

            limit_price = client._format_price(symbol, best_price)
            formatted_size = client._format_size(symbol, remaining)
            self.attempts += 1

            # Post-only: "Alo" (add liquidity only) on Hyperliquid
            self.sent = True
            try:
                order = client.create_order(
                    symbol=symbol,
                    is_buy=self.is_buy,
                    size=formatted_size,
                    limit_price=limit_price,
                    reduce_only=self.reduce_only,
                    post_only=False,
                    time_in_force="Alo",
                )
                self.last_order = order
            except OrderRejectedException as e:
                if not e.reason.startswith(_answers.POST_ONLY_CROSSED):
                    raise  # e.g. insufficient margin: no later attempt can succeed
                logger.debug(f"Post-only rejected for {symbol}: {e}")  # it would cross the spread: priced again
                time.sleep(self._pause())
                continue

            if order.status == "filled":
                logger.info(f"Maker order filled immediately: {symbol} {'buy' if self.is_buy else 'sell'} "
                            f"{order.filled_size} @ {order.average_fill_price}")
                return self._annotate(self._with_fills(order), is_maker=True)

            self.live = order
            order_id = order.order_id
            self._wait()
            attempt_filled = self._cancel_and_count(order_id, formatted_size)
            if attempt_filled > 0:
                remaining = max(0.0, float(Decimal(str(self.size)) - self._filled()))
                logger.info(f"Maker fill: {attempt_filled} filled, {remaining} remaining")
                if remaining <= 0:
                    return self._annotate(self._filled_order(order_id, order), is_maker=True)

        return self._after_timeout(remaining)

    def _best_price(self) -> Optional[float]:
        """The best bid (buy) or ask (sell), from the caller's cache or the order book; keeps
        the spread seen for the result."""
        book: Dict[str, Any]
        if self.bbo_cache is not None and self.symbol in self.bbo_cache:
            cached = self.bbo_cache[self.symbol]
            best_bid = cached.get("bid")
            best_ask = cached.get("ask")
            mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
            spread = best_ask - best_bid if best_bid and best_ask else None
            book = {"best_bid": best_bid, "best_ask": best_ask, "mid_price": mid, "spread": spread}
        else:
            book = self.client.get_order_book(self.symbol)
        mid = book.get("mid_price")
        spread = book.get("spread")
        if mid and spread and mid > 0:
            self.spread_bps = round((spread / mid) * 10000, 2)
        return book.get("best_bid") if self.is_buy else book.get("best_ask")

    def _pause(self) -> float:
        """Seconds until the next re-pricing: the interval, or what is left until the deadline
        (at least 0.1)."""
        return min(self.interval, max(0.1, self.deadline - time.time()))

    def _wait(self) -> None:
        """Leave the attempt in the book until the next re-pricing, or until the caller's
        fill_event says it may have filled."""
        pause = self._pause()
        if self.fill_event is not None:
            if self.fill_event.wait(timeout=pause):
                self.fill_event.clear()
        else:
            time.sleep(pause)

    def _cancel_and_count(self, order_id: int, formatted_size: float) -> Decimal:
        """Cancel the attempt and count how much of it filled. Never reprice or fall back
        without knowing: an unnoticed fill would be bought or sold a second time."""
        self.client.cancel_order(order_id, self.symbol)
        try:
            status = self.client.get_order_status(order_id)
            order_info = status["order"]
            order_detail = order_info["order"]
            state = order_info["status"]
            # The attempt must have ended (filled or canceled), and its sizes must say how
            # much of it filled; "sz" is what was left when it ended.
            if state == "filled":
                attempt_filled = Decimal(str(formatted_size))
            elif state in _answers.CANCELED_STATUSES:
                attempt_filled = Decimal(str(order_detail["origSz"])) - Decimal(str(order_detail["sz"]))
            else:
                raise ValueError(f"order status is {state!r}, not ended")
        except Exception as e:
            raise ActionOutcomeUnknownException(
                f"{self.symbol} maker order {order_id}",
                f"canceled, but how much it filled could not be read ({e}); "
                f"{self._filled()} of {self.size} filled by earlier attempts",
            ) from e

        # A maker (Alo) order fills at its limit price
        avg_px_str = order_detail.get("avgPx") or order_detail.get("limitPx")
        avg_px = Decimal(str(avg_px_str)) if avg_px_str else None
        if attempt_filled > 0:
            self.fills.append((attempt_filled, avg_px))
        self.live = None  # canceled, and its fill is counted
        return attempt_filled

    def _after_timeout(self, remaining: float) -> Order:
        """The time is up: the fallback for the remaining size, or the last attempt as it ended."""
        client, symbol = self.client, self.symbol
        if remaining > 0 and self.fallback != "cancel":
            formatted_remaining = client._format_size(symbol, remaining)
            if formatted_remaining <= 0:
                if self.last_order is not None:
                    self.last_order.status = "canceled"  # every attempt was canceled after its fills
                    return self._annotate(self._with_fills(self.last_order, counted=True), is_maker=True)
            logger.info(f"Maker chase timeout for {symbol}, falling back to {self.fallback} "
                        f"for remaining {formatted_remaining}")
            self.sent = True
            if self.fallback == "ioc":
                fallback_order = client.create_order(
                    symbol=symbol,
                    is_buy=self.is_buy,
                    size=formatted_remaining,
                    limit_price=None,
                    reduce_only=self.reduce_only,
                    time_in_force="Ioc",
                )
                return self._annotate(self._with_fills(fallback_order), is_maker=False)
            elif self.fallback == "market":
                if self.is_buy:
                    fallback_order = client.buy(symbol, formatted_remaining, reduce_only=self.reduce_only)
                else:
                    fallback_order = client.sell(symbol, formatted_remaining, reduce_only=self.reduce_only)
                return self._annotate(self._with_fills(fallback_order), is_maker=False)

        if self.last_order is not None:
            self.last_order.status = "canceled"  # the last attempt was canceled when the chase ended
            return self._annotate(self._with_fills(self.last_order, counted=True), is_maker=False)

        raise RuntimeError(f"maker_order failed: no order placed for {symbol}")

    def _filled(self) -> Decimal:
        return sum((qty for qty, _ in self.fills), Decimal(0))

    def _annotate(self, order: Order, is_maker: bool) -> Order:
        """Attach how the chase went to the order that is returned."""
        order.is_maker = is_maker
        order.attempts = self.attempts
        order.elapsed = round(time.time() - self.started, 2)
        order.spread_bps = self.spread_bps
        if self.reduce_only and self.fills:
            # Counted from each attempt's status (placed minus open), which for a reduce-only
            # order also includes a shrink by Hyperliquid to the position.
            order.filled_size_known = False
        return order

    def _with_fills(self, order: Order, counted: bool = False) -> Order:
        """Report the fills of every maker attempt on the order that is returned. `counted`:
        the order is a canceled attempt, its fills are in `fills` already."""
        if not self.fills:
            return order
        parts = list(self.fills)
        if order.filled_size and not counted:
            parts.append((Decimal(str(order.filled_size)), order.average_fill_price))
        order.size = Decimal(str(self.size))
        order.filled_size = sum((q for q, _ in parts), Decimal(0))
        order.average_fill_price = average_price(parts)
        return order

    def _filled_order(self, order_id: int, order: Order) -> Order:
        """The whole size as one filled order, once the last attempt's fill completed it."""
        avg_fill_price = average_price(self.fills)
        logger.info(f"Maker order filled: {self.symbol} {'buy' if self.is_buy else 'sell'} {self.size}"
                    f"{f' @ {avg_fill_price}' if avg_fill_price else ''}")
        return Order(
            order_id=order_id,
            cloid=order.cloid,
            symbol=self.symbol,
            is_buy=self.is_buy,
            size=Decimal(str(self.size)),
            filled_size=Decimal(str(self.size)),
            order_type=order.order_type,
            reduce_only=order.reduce_only,
            status="filled",
            created_at=order.created_at,
            limit_price=order.limit_price,
            average_fill_price=avg_fill_price,
        )
