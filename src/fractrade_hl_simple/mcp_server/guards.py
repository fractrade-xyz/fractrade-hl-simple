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

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, TypeGuard

from ..models import PERP_MARKETS, coin_market
from .config import AccountConfig
from .convert import decimal as _decimal
from .errors import ToolError


@dataclass(frozen=True)
class Check:
    """The outcome of one rule."""
    rule: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class GuardReport:
    """The rules checked for one action, in order."""
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def enforce(self) -> None:
        """Raise GUARD_BLOCKED, with every check in its details, unless all passed."""
        failed = [check for check in self.checks if not check.passed]
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
    is in the market's collateral token (USDC on every dex that has listed markets, 2026-11)."""
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
    day_loss: Optional[Decimal] = None          # loss over the last 24 h (0 for a gain)
    day_capital: Optional[Decimal] = None       # account value without that PnL: 24 h ago plus transfers
    orders_last_hour: int = 0                   # orders that added risk, by this server
    mid: Optional[Decimal] = None               # the market's mid price, read for a sell
    loss_at_stops: Optional[Decimal] = None     # open_position: the loss if every stop fills


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


def total_margin(margin_used: Optional[Decimal], margin: Decimal, account_value: Optional[Decimal],
                 max_pct: Decimal) -> Check:
    """Margin in use with the new order's, at most `max_pct` % of the account value."""
    if (not _finite(account_value) or account_value <= 0 or not _finite(margin_used) or margin_used < 0
            or not _finite(margin) or margin < 0):
        return Check("max_total_margin_pct", False, "the account value or the margin in use is not established, "
                                                    "so max_total_margin_pct cannot be checked")
    limit, total = account_value * max_pct / 100, margin_used + margin
    if total <= limit:
        return Check("max_total_margin_pct", True, f"margin in use with this order {_money(total)} is within "
                                                   f"{_text(max_pct)} % of the account value ({_money(limit)})")
    return Check("max_total_margin_pct", False, f"margin in use with this order {_money(total)} is above "
                                                f"{_text(max_pct)} % of the account value ({_money(limit)})")


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


def order_rate(count: int, max_per_hour: int) -> Check:
    """Orders that add risk in the last hour, before this one, below `max_per_hour`."""
    if count < max_per_hour:
        return Check("max_orders_per_hour", True, f"{count} orders that add risk in the last hour, below {max_per_hour}")
    return Check("max_orders_per_hour", False, f"{count} orders that add risk in the last hour reached "
                                               f"max_orders_per_hour {max_per_hour}")


def trade_risk(loss: Optional[Decimal], account_value: Optional[Decimal], max_pct: Decimal) -> Check:
    """max_risk_pct: what the trade loses if every stop loss fills (after fees and estimated
    slippage), at most `max_pct` % of the account value. None: no stop losses with the order
    (buy, sell, modify_order), or not estimable: refused."""
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


def needs(account: AccountConfig) -> set[str]:
    """What an order that adds risk must read for `account`'s guardrails: "leverage" (the
    market's, for check_order_leverage, isolated_only and the margins), "account" (value and
    margin in use), "day" (the last 24 hours). A sell's mid is read besides (valued_price)."""
    wanted: set[str] = set()
    margins = account.max_margin_pct is not None or account.max_total_margin_pct is not None
    if account.check_order_leverage or account.isolated_only or margins:
        wanted.add("leverage")
    if margins or account.max_risk_pct is not None:
        wanted.add("account")
    if account.max_daily_loss_pct is not None:
        wanted.add("day")
    return wanted


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
    checks: list[Check] = []
    leverage = risk.leverage  # read whenever a guardrail here needs it (needs; check_order makes sure)
    if account.check_order_leverage and leverage is not None:
        checks.append(market_leverage(leverage.value, _limit(account.max_leverage, "max_leverage")))
    if account.isolated_only and leverage is not None:
        checks.append(isolated(leverage.type))
    margin = value / leverage.value if leverage is not None and value.is_finite() else Decimal("NaN")
    if account.max_margin_pct is not None:
        checks.append(margin_per_trade(margin, risk.account_value, account.max_margin_pct))
    if account.max_total_margin_pct is not None:
        checks.append(total_margin(risk.margin_used, margin, risk.account_value, account.max_total_margin_pct))
    if account.max_daily_loss_pct is not None:
        checks.append(daily_loss(risk.day_loss, risk.day_capital, account.max_daily_loss_pct))
    if account.max_orders_per_hour is not None:
        checks.append(order_rate(risk.orders_last_hour, account.max_orders_per_hour))
    if account.max_risk_pct is not None:
        checks.append(trade_risk(risk.loss_at_stops, risk.account_value, account.max_risk_pct))
    return checks


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
