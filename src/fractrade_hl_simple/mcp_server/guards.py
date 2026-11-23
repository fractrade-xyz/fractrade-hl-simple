"""Guards: the account's configured limits, checked on an order exactly as it will
be signed.

Pure: no requests and no client. A trade tool prepares the order with the library
(`prepare_order`: the size and price `create_order` signs), checks it here and sends it only
if every rule passed; otherwise `GUARD_BLOCKED` with the report in its details. Input the
library validates itself (a known symbol, a finite positive size and price, the minimum
size) and rules the exchange enforces (minimum order value, margin, open-interest caps) are
not checked here.

Risk-reducing actions (closes, stop losses and take profits, reduce-only orders, cancels)
are exempt from the allowlist and the notional cap, so neither can stop a position from
being reduced.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Any, Optional, TypeGuard

from ..models import PERP_MARKETS, coin_market
from .config import AccountConfig
from .convert import decimal as _decimal
from .errors import ToolError


@dataclass(frozen=True)
class Check:
    """The outcome of one rule. mode "warn": one that did not pass is reported, not enforced."""
    rule: str
    passed: bool
    detail: str
    mode: str = "block"

    def as_dict(self) -> dict[str, Any]:
        shown = {"rule": self.rule, "passed": self.passed, "detail": self.detail}
        return shown if self.mode == "block" else {**shown, "mode": self.mode}


# The warnings of the trade call running in this thread (collect_warnings).
_WARNINGS: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar("guard_warnings", default=None)


@contextmanager
def collect_warnings() -> Iterator[list[dict[str, Any]]]:
    """The warnings of the rules in warn mode that did not pass, while the block runs:
    GuardReport.enforce() adds them."""
    collected: list[dict[str, Any]] = []
    token = _WARNINGS.set(collected)
    try:
        yield collected
    finally:
        _WARNINGS.reset(token)


@dataclass(frozen=True)
class GuardReport:
    """The rules checked for one action, in order."""
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def enforce(self) -> None:
        """Raise GUARD_BLOCKED, with every check in its details, unless every rule in block
        mode passed; the rules in warn mode that did not pass are warnings (collect_warnings)."""
        collected = _WARNINGS.get()
        if collected is not None:
            collected.extend({"rule": check.rule, "detail": check.detail} for check in self.checks
                             if not check.passed and check.mode == "warn")
        failed = [check for check in self.checks if not check.passed and check.mode == "block"]
        if failed:
            raise ToolError("GUARD_BLOCKED", "; ".join(check.detail for check in failed),
                            hint="change the call to fit the account's limits; the limits are in list_accounts",
                            details={"checks": [check.as_dict() for check in self.checks]})


# ── rules ───────────────────────────────────────────────────────────


def perp_only(symbol: str) -> Check:
    """A perp of the default dex or of a HIP-3 dex: the trade tools trade nothing else. The
    library's methods also act on spot orders by pair name (cancel_order), so cancels are
    checked too."""
    market = coin_market(symbol)
    if market in PERP_MARKETS:
        return Check("perp_only", True, f"{symbol} is a perp")
    return Check("perp_only", False, f"{symbol} is not a perp ({market}); the trade tools trade perps only")


def symbol_allowed(symbol: str, allowlist: Optional[frozenset[str]]) -> Check:
    """`symbol` is in the account's `symbols`, if it has one (names exactly as Hyperliquid
    names them)."""
    if allowlist is None:
        return Check("symbol_allowed", True, "the account has no symbols allowlist")
    if symbol in allowlist:
        return Check("symbol_allowed", True, f"{symbol} is in the account's symbols")
    return Check("symbol_allowed", False, f"{symbol} is not in the account's symbols ({', '.join(sorted(allowlist))})")


def order_notional(size: float, price: Any, max_order_usd: Decimal) -> Check:
    """Size times the order's value price (`valued_price`), at most `max_order_usd`. The price
    is in the market's collateral token, counted as USD: USDC on most dexes, a USD stablecoin
    such as USDH, USDE or USDT0 on some HIP-3 dexes (2026-11)."""
    notional = _decimal(size) * _decimal(price)
    if notional.is_finite() and notional <= max_order_usd:
        return Check("order_notional", True, f"order value {_text(notional)} is within max_order_usd {_text(max_order_usd)}")
    return Check("order_notional", False,
                 f"order value {_text(notional)} ({_text(size)} at {_text(price)}) is above max_order_usd {_text(max_order_usd)}")


def slippage(value: Optional[float], max_slippage: Decimal) -> Check:
    """The slippage of a market order, at most `max_slippage`. None is the client's default,
    which the server sets to `max_slippage`. It is the tolerance before the SDK's price
    rounding, which can add a few thousandths of a percent."""
    if value is None:
        return Check("slippage", True, f"slippage is the account's max_slippage, {_text(max_slippage)}")
    requested = _decimal(value)
    if requested.is_finite() and requested <= max_slippage:
        return Check("slippage", True, f"slippage {_text(requested)} is within max_slippage {_text(max_slippage)}")
    return Check("slippage", False, f"slippage {_text(requested)} is above max_slippage {_text(max_slippage)}")


def leverage(value: int, max_leverage: int) -> Check:
    """Requested leverage, at most `max_leverage` (the market's own maximum is the library's
    check)."""
    if value <= max_leverage:
        return Check("leverage", True, f"leverage {value} is within max_leverage {max_leverage}")
    return Check("leverage", False, f"leverage {value} is above max_leverage {max_leverage}")


@dataclass(frozen=True)
class RiskState:
    """What the risk guardrails need to know about the account, read just before an order
    that adds risk (trade tools), only as far as a configured guardrail needs it."""
    leverage: Optional[Any] = None              # models.Leverage of the market: type and value
    account_value: Optional[Decimal] = None     # None: not established for the account mode
    margin_used: Optional[Decimal] = None       # margin in use now, every position
    pending_margin: Optional[Decimal] = None    # remaining non-reduce-only perp entries, at current leverage
    day_loss: Optional[Decimal] = None          # loss over the last 24 h (0 for a gain)
    day_capital: Optional[Decimal] = None       # account value without that PnL: 24 h ago plus transfers
    orders_last_hour: int = 0                   # orders that added risk, by this server
    mid: Optional[Decimal] = None               # the market's mid price, read for a sell
    loss_at_stops: Optional[Decimal] = None     # open_position: the loss if every stop fills
    # max_day_loss_pct: the loss since Hyperliquid's last portfolio record at or before the
    # trading day's start (`since`, ms), and the account value then; `start` the day's start.
    today_loss: Optional[Decimal] = None
    today_capital: Optional[Decimal] = None
    today_since: Optional[int] = None
    today_start: Optional[int] = None
    # max_total_notional_x: the open positions' notional plus the open orders that add risk.
    open_notional: Optional[Decimal] = None
    unread: tuple[tuple[str, str], ...] = ()    # (rule, why) of a read that failed for a rule in warn mode


def valued_price(price: Any, is_buy: bool, mid: Optional[Decimal]) -> Decimal:
    """The price an order is valued at: a buy's limit price, the most it fills at; a sell's
    limit price or the current mid, whichever is higher, as a sell priced below the market
    fills at the market. NaN (so every rule fails) for a sell without a finite mid."""
    signed = _decimal(price)
    if is_buy:
        return signed
    if mid is None or not _finite(mid) or not signed.is_finite():
        return Decimal("NaN")
    return max(signed, mid)


def market_leverage(value: Decimal, max_leverage: int) -> Check:
    """The leverage the account has on the market (wherever it was set), at most
    `max_leverage`: the margin of an order is its value divided by it."""
    if value <= max_leverage:
        return Check("leverage", True, f"the market's leverage {_text(value)}x is within max_leverage {max_leverage}x")
    return Check("leverage", False, f"the market's leverage {_text(value)}x is above max_leverage {max_leverage}x; "
                                    "lower it with set_leverage")


def isolated(kind: str) -> Check:
    """isolated_only: new risk only with isolated margin, so a position loses at most its margin."""
    if kind == "isolated":
        return Check("isolated_only", True, "the market uses isolated margin")
    return Check("isolated_only", False, "the market uses cross margin, and the account trades isolated only; "
                                         "set it with set_leverage and is_cross false")


def margin_per_trade(margin: Decimal, account_value: Optional[Decimal], max_pct: Decimal) -> Check:
    """The new order's margin, at most `max_pct` % of the account value."""
    if not _finite(account_value) or account_value <= 0 or not _finite(margin) or margin < 0:
        return Check("max_margin_pct", False, "the account value or the order's margin is not established, so "
                                              "max_margin_pct cannot be checked")
    limit = account_value * max_pct / 100
    if margin <= limit:
        return Check("max_margin_pct", True, f"order margin {_money(margin)} is within {_text(max_pct)} % of the "
                                             f"account value ({_money(limit)})")
    return Check("max_margin_pct", False, f"order margin {_money(margin)} is above {_text(max_pct)} % of the account "
                                          f"value ({_money(limit)})")


def total_margin(margin_used: Optional[Decimal], pending: Optional[Decimal], margin: Decimal,
                 account_value: Optional[Decimal], max_pct: Decimal, what: str = "this order") -> Check:
    """Used position margin plus pending entries and the proposed order (or batch),
    at most `max_pct` % of the account value. Opposing entries are not netted."""
    if (not _finite(account_value) or account_value <= 0 or not _finite(margin_used) or margin_used < 0
            or not _finite(pending) or pending < 0 or not _finite(margin) or margin < 0):
        return Check("max_total_margin_pct", False, "the account value, used margin, pending entry margin or "
                                                    "proposed margin is not established, so "
                                                    "max_total_margin_pct cannot be checked")
    limit, total = account_value * max_pct / 100, margin_used + pending + margin
    passed = total <= limit
    return Check("max_total_margin_pct", passed, f"projected margin with {what} {_money(total)} is "
                 f"{'within' if passed else 'above'} {_text(max_pct)} % of the account value ({_money(limit)}); "
                 f"used {_money(margin_used)}, pending entries {_money(pending)}, proposed {_money(margin)}")


def daily_loss(loss: Optional[Decimal], capital: Optional[Decimal], max_pct: Decimal) -> Check:
    """The loss over the last 24 hours, below `max_pct` % of the account value without it
    (the value 24 hours ago plus deposits, minus withdrawals: an account funded today is
    covered, and a deposit does not count as a gain); at or above it, no new risk until it
    recovers."""
    if not _finite(loss) or loss < 0 or not _finite(capital):
        return Check("max_daily_loss_pct", False, "the last 24 hours' PnL is not established, so max_daily_loss_pct "
                                                  "cannot be checked")
    if loss <= 0:
        return Check("max_daily_loss_pct", True, "no loss over the last 24 hours")
    if capital <= 0:
        return Check("max_daily_loss_pct", False, "the account value without the last 24 hours' loss is not "
                                                  "positive, so max_daily_loss_pct cannot be checked")
    pct = loss * 100 / capital
    if pct < max_pct:
        return Check("max_daily_loss_pct", True, f"loss over the last 24 hours {_text(round(pct, 2))} % is below "
                                                 f"{_text(max_pct)} %")
    return Check("max_daily_loss_pct", False, f"loss over the last 24 hours {_text(round(pct, 2))} % reached "
                                              f"{_text(max_pct)} %: no new risk until it recovers; closing still works")


def portfolio_day_change(portfolio: Any) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """The PnL over the last 24 hours and the account value without it, from Hyperliquid's
    portfolio ("day": account value and cumulative PnL histories, sampled at the same times,
    the last one current): the last value minus the PnL, which is the value 24 hours ago
    plus deposits, minus withdrawals. (None, None) if either history is missing or empty,
    they end at different times, or a figure is no number."""
    try:
        histories = _histories(portfolio, "day")
        if histories is None:
            return None, None
        values, pnl = histories
        last, first, value = (Decimal(str(pnl[-1][1])), Decimal(str(pnl[0][1])), Decimal(str(values[-1][1])))
        if not (last.is_finite() and first.is_finite() and value.is_finite()):
            return None, None
        change = last - first
        return change, value - change
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, ArithmeticError):
        return None, None


def _histories(portfolio: Any, window: str) -> Optional[tuple[list[Any], list[Any]]]:
    """The account value and cumulative PnL histories of one portfolio window ("day",
    "week", ...), sampled at the same times and ending with the current one; None if either is
    missing or empty, or they end at different times."""
    history = dict(portfolio).get(window)
    if not isinstance(history, dict):
        return None
    values, pnl = history.get("accountValueHistory"), history.get("pnlHistory")
    if not isinstance(values, list) or not isinstance(pnl, list) or not values or not pnl \
            or values[-1][0] != pnl[-1][0]:
        return None
    return values, pnl


def portfolio_day_loss(portfolio: Any) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """The loss over the last 24 hours (0 after a gain) and the account value without it
    (portfolio_day_change); (None, None), which the guardrail refuses, if not established."""
    change, capital = portfolio_day_change(portfolio)
    return (None, None) if change is None else (max(Decimal(0), -change), capital)


def day_start_ms(now_ms: int, day_start: str, tz: str) -> int:
    """When the trading day running at `now_ms` started: the last day_start ("HH:MM") in the
    time zone tz, at or before now. Built from the local calendar date, so a day across a
    clock change has 23 or 25 hours; a time the change skips is read with the offset before
    it (03:30 in a skipped hour is 04:30), one it repeats is its first occurrence."""
    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now_ms / 1000, zone)
    hour, minute = (int(part) for part in day_start.split(":"))
    start = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if start.timestamp() * 1000 > now_ms:
        start = (local - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    return int(start.timestamp() * 1000)


def portfolio_since(portfolio: Any, start_ms: int) -> tuple[Optional[Decimal], Optional[Decimal], Optional[int]]:
    """(loss, capital, since) for max_day_loss_pct from Hyperliquid's portfolio: its last
    record at or before `start_ms` (since), in its "day" history or, without one there, its
    "week" history (Hyperliquid samples them about every 2 h 20 min); the loss is how much the
    cumulative PnL fell from that record to the last one (now), 0 after a gain; capital the
    account value at that record. Deposits and withdrawals are no PnL, so they neither count
    as a loss nor give room back. (None, None, None) if no record establishes it."""
    try:
        for window in ("day", "week"):
            histories = _histories(portfolio, window)
            if histories is None:
                continue
            values, pnl = histories
            before = [point for point in values if point[0] <= start_ms]
            if not before:
                continue
            since, value = before[-1]
            at = {point[0]: point[1] for point in pnl}
            if since not in at:
                return None, None, None
            change = Decimal(str(pnl[-1][1])) - Decimal(str(at[since]))
            capital = Decimal(str(value))
            if not (change.is_finite() and capital.is_finite()):
                return None, None, None
            return max(Decimal(0), -change), capital, int(since)
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, ArithmeticError):
        pass
    return None, None, None


def today(portfolio: Any, day_start: str, tz: str, now_ms: Optional[int] = None) -> dict[str, Any]:
    """The RiskState fields of max_day_loss_pct from the portfolio: the loss since the trading
    day (day_start in tz) started, by portfolio_since."""
    start = day_start_ms(int(time.time() * 1000) if now_ms is None else now_ms, day_start, tz)
    loss, capital, since = portfolio_since(portfolio, start)
    return {"today_loss": loss, "today_capital": capital, "today_since": since, "today_start": start}


def open_notional(summary: Any, orders: Iterable[Any], mids: Mapping[str, Decimal]) -> Decimal:
    """max_total_notional_x's open notional: the open positions of the account summary, plus
    its open perp orders that add risk (not reduce-only), at entry_value with the current
    `mids` (each symbol's; needed for every pending sell)."""
    held = sum((abs(p.position_value) for dex in summary.dexes for p in dex.positions if p.size != 0), Decimal(0))
    return held + sum((entry_value(o, mids.get(o.symbol)) for o in orders
                       if not o.reduce_only and o.market in PERP_MARKETS), Decimal(0))


def pending_margin(orders: Iterable[Any], leverages: Mapping[str, Any], mids: Mapping[str, Decimal]) -> Decimal:
    """Margin for every remaining perp entry, including ones placed outside this server.
    Reduce-only protection reserves none. Unknown leverage or value makes the total unknown.
    """
    total = Decimal(0)
    for order in orders:
        if order.reduce_only or order.market not in PERP_MARKETS:
            continue
        leverage = leverages.get(order.symbol)
        if leverage is None or not _finite(leverage.value) or leverage.value <= 0:
            return Decimal("NaN")
        value = entry_value(order, mids.get(order.symbol))
        if not value.is_finite():
            return Decimal("NaN")
        total += value / leverage.value
    return total


def _utc(ms: int) -> str:
    """A time for a message: UTC, to the minute."""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def today_loss(risk: "RiskState", max_pct: Decimal, mode: str) -> Check:
    """max_day_loss_pct: the loss since the trading day started (portfolio_since), below
    max_pct % of the account value then; at or above it, no new risk (block) or a warning."""
    rule = "max_day_loss_pct"
    unread = dict(risk.unread).get(rule)
    loss, capital, since = risk.today_loss, risk.today_capital, risk.today_since
    if unread is not None or not _finite(loss) or not _finite(capital) or since is None or capital <= 0:
        why = unread or "Hyperliquid's portfolio has no record at or before the day's start"
        return Check(rule, False, f"today's loss cannot be checked ({why})", mode)
    pct = loss * 100 / capital
    span = f"since {_utc(since)} (Hyperliquid's last record before the day's start"
    span += f", {_utc(risk.today_start)})" if risk.today_start is not None else ")"
    if pct < max_pct:
        return Check(rule, True, f"loss {span} {_text(round(pct, 2))} % is below {_text(max_pct)} %", mode)
    return Check(rule, False, f"loss {span} {_text(round(pct, 2))} % reached {_text(max_pct)} % of the account "
                              f"value then ({_money(capital)})", mode)


def entry_value(order: Any, mid: Optional[Decimal]) -> Decimal:
    """The notional of an open order that adds risk, valued as a new one is (valued_price):
    its open size at its limit price, a trigger order's at the higher of its trigger and
    limit price (a buy stop's limit is the most it fills at), and a sell's at the current
    `mid` when that is higher. NaN for an unknown price/size or a sell without a finite mid:
    the total cannot be checked then."""
    prices: list[Decimal] = [price for price in (order.trigger_price, order.limit_price) if price is not None]
    size = order.remaining_size
    if (not prices or any(not _finite(price) or price <= 0 for price in prices)
            or not _finite(size) or size < 0):
        return Decimal("NaN")
    if not order.is_buy:
        if not _finite(mid) or mid <= 0:
            return Decimal("NaN")
        prices.append(mid)
    return size * max(prices)


def total_notional(risk: "RiskState", adding: Decimal, max_x: Decimal, mode: str,
                   what: Optional[str] = "this order") -> Check:
    """max_total_notional_x: the open positions and the open orders that add risk, with the
    new ones (`what`; None for none), at most max_x times the account value."""
    rule = "max_total_notional_x"
    unread = dict(risk.unread).get(rule)
    held, value = risk.open_notional, risk.account_value
    if unread is not None or not _finite(held) or not _finite(value) or not _finite(adding) or value <= 0:
        why = unread or "the account value or the open positions and orders are not established"
        return Check(rule, False, f"the total notional cannot be checked ({why})", mode)
    total = held + adding
    ratio = total / value
    text = (f"positions and open orders that add risk{f' with {what}' if what else ''}: {_money(total)}, "
            f"{_text(round(ratio, 2))}x the account value ({_money(value)})")
    if ratio <= max_x:
        return Check(rule, True, f"{text}, within {_text(max_x)}x", mode)
    return Check(rule, False, f"{text}, above {_text(max_x)}x", mode)


def order_rate(count: int, max_per_hour: int, adding: int = 1) -> Check:
    """Orders that add risk in the last hour, before this one, below `max_per_hour`; for a
    batch of `adding` such orders, all of them fit: count + adding at most `max_per_hour`."""
    if adding == 1:
        if count < max_per_hour:
            return Check("max_orders_per_hour", True,
                         f"{count} orders that add risk in the last hour, below {max_per_hour}")
        return Check("max_orders_per_hour", False, f"{count} orders that add risk in the last hour reached "
                                                   f"max_orders_per_hour {max_per_hour}")
    total = count + adding
    if total <= max_per_hour:
        return Check("max_orders_per_hour", True, f"{count} orders that add risk in the last hour and {adding} in this "
                                                  f"batch, {total}, within max_orders_per_hour {max_per_hour}")
    return Check("max_orders_per_hour", False, f"{count} orders that add risk in the last hour and {adding} in this "
                                               f"batch, {total}, are above max_orders_per_hour {max_per_hour}")


def trade_risk(loss: Optional[Decimal], account_value: Optional[Decimal], max_pct: Decimal) -> Check:
    """max_risk_pct: what the trade loses if every stop loss fills (after fees and estimated
    slippage), at most `max_pct` % of the account value. None: no stop losses with the order
    (buy, sell, modify_order, the batches), or not estimable: refused."""
    if loss is None or not _finite(loss):
        return Check("max_risk_pct", False, "the order's loss is not limited by stop losses, or not estimable, so "
                                            "max_risk_pct cannot be checked: open the trade with open_position")
    if not _finite(account_value) or account_value <= 0:
        return Check("max_risk_pct", False, "the account value is not established, so max_risk_pct cannot be checked")
    limit = account_value * max_pct / 100
    if loss <= limit:
        return Check("max_risk_pct", True, f"loss at the stop losses {_money(loss)} is within {_text(max_pct)} % of "
                                           f"the account value ({_money(limit)})")
    return Check("max_risk_pct", False, f"loss at the stop losses {_money(loss)} is above {_text(max_pct)} % of the "
                                        f"account value ({_money(limit)}); plan_trade sizes it for a risk")


def _reads(account: AccountConfig) -> dict[str, list[tuple[str, str]]]:
    """Each read an order that adds risk needs for `account`'s rules, with those rules and
    their modes: "leverage" (the market's, for check_order_leverage, isolated_only and the
    margins), "account" (value and margin in use), "day" (the last 24 hours), "today" (the
    portfolio, for max_day_loss_pct), "orders" (open orders and their sell mids, for total
    margin and total notional). A proposed sell's mid is read besides (valued_price)."""
    reads: dict[str, list[tuple[str, str]]] = {}

    def need(read: str, rule: str, mode: str = "block") -> None:
        reads.setdefault(read, []).append((rule, mode))
    for rule in ("check_order_leverage", "isolated_only"):
        if getattr(account, rule):
            need("leverage", rule)
    for rule in ("max_margin_pct", "max_total_margin_pct"):
        if getattr(account, rule) is not None:
            need("leverage", rule)
            need("account", rule)
    if account.max_total_margin_pct is not None:
        need("orders", "max_total_margin_pct")
    if account.max_risk_pct is not None:
        need("account", "max_risk_pct")
    if account.max_daily_loss_pct is not None:
        need("day", "max_daily_loss_pct")
    if _active(account.max_day_loss_pct, account.max_day_loss_mode):
        need("today", "max_day_loss_pct", account.max_day_loss_mode)
    if _active(account.max_total_notional_x, account.max_total_notional_mode):
        need("orders", "max_total_notional_x", account.max_total_notional_mode)
        need("account", "max_total_notional_x", account.max_total_notional_mode)
    return reads


def needs(account: AccountConfig) -> set[str]:
    """What an order that adds risk must read for `account`'s rules (_reads)."""
    return set(_reads(account))


def soft(account: AccountConfig) -> dict[str, list[str]]:
    """The reads only rules in warn mode need, with those rules: a failed one makes them
    "cannot be checked" (a warning) instead of refusing the order. Any read a rule in block
    mode needs still refuses it. "day" and "today" are one read, the portfolio."""
    reads = _reads(account)
    portfolio = reads.pop("day", []) + reads.pop("today", [])
    if portfolio:
        reads["portfolio"] = portfolio
    return {read: [rule for rule, _ in rules] for read, rules in reads.items()
            if all(mode == "warn" for _, mode in rules)}


def _active(limit: Optional[Decimal], mode: str) -> bool:
    return limit is not None and mode != "off"


# ── the checks of each kind of action ───────────────────────────────


def check_order(account: AccountConfig, symbol: str, size: float, price: float, *, is_buy: bool,
                reduce_only: bool, market: bool, slippage_value: Optional[float] = None,
                risk: Optional[RiskState] = None) -> GuardReport:
    """`buy`, `sell` (`market`: no limit price) and `modify_order` (never `market`), with
    the prepared size and price. A reduce-only order is risk-reducing: the exchange refuses
    one that would increase a position, so only perp_only and the slippage apply to it.
    Any other order needs `risk`, the account state its guardrails need (`needs`, and the
    mid for a sell), and is valued at `valued_price`."""
    checks = [perp_only(symbol)]
    if not reduce_only:
        if risk is None or ("leverage" in needs(account) and risk.leverage is None):
            raise ValueError("an order that adds risk needs the account's risk state")
        valued = valued_price(price, is_buy, risk.mid)
        checks.append(symbol_allowed(symbol, account.symbols))
        if account.max_order_usd is not None:
            checks.append(order_notional(size, valued, account.max_order_usd))
        checks.extend(_risk_checks(account, _decimal(size) * valued, risk))
    if market:
        checks.append(slippage(slippage_value, account.max_slippage))
    return _report(checks)


def _risk_checks(account: AccountConfig, value: Decimal, risk: RiskState) -> list[Check]:
    """The guardrails of an order that adds risk, with the order's value."""
    margin = _margin(value, risk)
    return [*_order_guardrails(account, margin, risk),
            *_account_guardrails(account, margin, risk, 1, "this order", value), *_loss_guardrail(account, risk)]


def _margin(value: Decimal, risk: RiskState) -> Decimal:
    """The margin of an order of `value`: divided by the market's leverage (NaN without it)."""
    leverage = risk.leverage
    return value / leverage.value if leverage is not None and value.is_finite() else Decimal("NaN")


def _order_guardrails(account: AccountConfig, margin: Decimal, risk: RiskState) -> list[Check]:
    """The guardrails about one order that adds risk: the market's leverage, isolated margin,
    its margin."""
    checks: list[Check] = []
    leverage = risk.leverage  # read whenever a guardrail here needs it (needs; check_order makes sure)
    if account.check_order_leverage and leverage is not None:
        checks.append(market_leverage(leverage.value, _limit(account.max_leverage, "max_leverage")))
    if account.isolated_only and leverage is not None:
        checks.append(isolated(leverage.type))
    if account.max_margin_pct is not None:
        checks.append(margin_per_trade(margin, risk.account_value, account.max_margin_pct))
    return checks


def _loss_guardrail(account: AccountConfig, risk: RiskState) -> list[Check]:
    """max_risk_pct of one order that adds risk: its loss at the stop losses."""
    if account.max_risk_pct is None:
        return []
    return [trade_risk(risk.loss_at_stops, risk.account_value, account.max_risk_pct)]


def _account_guardrails(account: AccountConfig, margin: Decimal, risk: RiskState, adding: int,
                        what: str, value: Decimal) -> list[Check]:
    """The guardrails about the account, for `adding` orders that add risk together (a batch)
    with `margin` and `value` in all: the margin in use with theirs, the last 24 hours, the
    order rate, today's loss and the total notional."""
    checks: list[Check] = []
    if account.max_total_margin_pct is not None:
        checks.append(total_margin(risk.margin_used, risk.pending_margin, margin, risk.account_value,
                                   account.max_total_margin_pct, what))
    if account.max_daily_loss_pct is not None:
        checks.append(daily_loss(risk.day_loss, risk.day_capital, account.max_daily_loss_pct))
    if account.max_orders_per_hour is not None:
        checks.append(order_rate(risk.orders_last_hour, account.max_orders_per_hour, adding))
    if _active(account.max_day_loss_pct, account.max_day_loss_mode):
        checks.append(today_loss(risk, _limit(account.max_day_loss_pct, "max_day_loss_pct"), account.max_day_loss_mode))
    if _active(account.max_total_notional_x, account.max_total_notional_mode):
        checks.append(total_notional(risk, value, _limit(account.max_total_notional_x, "max_total_notional_x"),
                                     account.max_total_notional_mode))
    return checks


@dataclass(frozen=True)
class BatchOrder:
    """One limit order of a batch (place_orders, place_ladder), with its size and price as
    they will be signed."""
    symbol: str
    is_buy: bool
    size: float
    price: float
    reduce_only: bool


def check_batch(account: AccountConfig, orders: Sequence[BatchOrder],
                risks: Sequence[Optional[RiskState]]) -> GuardReport:
    """`place_orders`, `place_ladder`: limit orders, never market. Each order is checked as
    check_order checks one (perp_only; for one that adds risk the allowlist, max_order_usd
    and the guardrails about the order: the market's leverage, isolated_only,
    max_margin_pct, max_risk_pct), each check named by its place in the batch. The
    guardrails about the account see the batch's orders that add risk together, as if all
    fill: max_total_margin_pct with their margins summed, max_daily_loss_pct, and
    max_orders_per_hour with all of them counted. Without an order that adds risk (all
    reduce_only) there are none of those. `risks`: each order's risk state, None for a
    reduce-only one."""
    checks: list[Check] = []
    margin, total, adding, shared = Decimal(0), Decimal(0), 0, None
    for number, (order, risk) in enumerate(zip(orders, risks, strict=True), start=1):
        own = [perp_only(order.symbol)]
        if not order.reduce_only:
            if risk is None or ("leverage" in needs(account) and risk.leverage is None):
                raise ValueError("an order that adds risk needs the account's risk state")
            valued = valued_price(order.price, order.is_buy, risk.mid)
            value = _decimal(order.size) * valued
            own.append(symbol_allowed(order.symbol, account.symbols))
            if account.max_order_usd is not None:
                own.append(order_notional(order.size, valued, account.max_order_usd))
            order_margin = _margin(value, risk)
            own.extend([*_order_guardrails(account, order_margin, risk), *_loss_guardrail(account, risk)])
            margin, total, adding, shared = margin + order_margin, total + value, adding + 1, risk
        checks.extend(replace(check, detail=f"order {number} ({order.symbol}): {check.detail}") for check in own)
    if shared is not None:
        checks.extend(_account_guardrails(account, margin, shared, adding, f"the batch's {adding} orders that add risk",
                                          total))
    return _report(checks)


def check_close(account: AccountConfig, symbol: str, slippage_value: Optional[float]) -> GuardReport:
    """`close`: risk-reducing, a market order."""
    return _report([perp_only(symbol), slippage(slippage_value, account.max_slippage)])


def check_symbol(symbol: str) -> GuardReport:
    """The risk-reducing actions on a symbol that place no market order: `stop_loss`,
    `take_profit`, `update_stop_loss`, `update_take_profit` and the cancels."""
    return _report([perp_only(symbol)])


def check_leverage(account: AccountConfig, symbol: str, value: int, is_cross: bool = False) -> GuardReport:
    """`set_leverage`; with isolated_only, cross margin is refused."""
    checks = [perp_only(symbol), symbol_allowed(symbol, account.symbols),
              leverage(value, _limit(account.max_leverage, "max_leverage"))]
    if account.isolated_only:
        checks.append(isolated("cross" if is_cross else "isolated"))
    return _report(checks)


# ── helpers ─────────────────────────────────────────────────────────


def _report(checks: Iterable[Check]) -> GuardReport:
    return GuardReport(tuple(checks))


def _limit(value: Any, name: str) -> Any:
    """A limit every trading account has (config validation requires it)."""
    if value is None:
        raise ValueError(f"{name} is not configured: the account cannot trade")
    return value


def _finite(value: Any) -> TypeGuard[Decimal]:
    """A finite Decimal: what a guardrail can compare (None, NaN and infinity cannot pass)."""
    return isinstance(value, Decimal) and value.is_finite()


def _money(value: Decimal) -> str:
    return f"{_text(round(value, 2))} USD" if value.is_finite() else str(value)


def _text(value: Any) -> str:
    """A number for a message, without exponent or trailing zeros (86.7010 is 86.701)."""
    number = _decimal(value)
    if not number.is_finite():
        return str(number)
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
