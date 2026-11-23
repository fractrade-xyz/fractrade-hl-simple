"""The risk tools: read only, they place nothing.

get_position_risk: an open position with its stop losses and take profits: what each level
makes or loses, what every stop and every take profit would, risk/reward, warnings.
plan_trade: the same for a trade before it is entered, and its size from a risk in USD or %
of the account. The arithmetic is risk.py; here the reads (position, open orders, order
book, fee rates, account value, leverage).
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfoNotFoundError

from ...exceptions import InvalidArgumentException, PositionNotFoundException
from ..._inputs import MIN_ORDER_USD
from ..._protection import split_size
from ...models import PERP_MARKETS, Order
from .. import guards, risk
from ..accounts import AccountRegistry, Target
from ..convert import decimal as _decimal, iso
from ..errors import ToolError, error_body
from ..server import ToolSpec
from .read import OBSERVATION_TEXT, SYMBOL, _observation, _wallet_tool

PRICE = {"type": "number", "exclusiveMinimum": 0}


def risk_tools(registry: AccountRegistry) -> list[ToolSpec]:
    return [_get_position_risk(registry), _get_account_risk(registry), _plan_trade(registry)]


# ── get_position_risk ─────────────────────────────────────────────────


def _get_position_risk(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = arguments["symbol"]
        position = next((p for p in client.get_positions(address=target.address)
                         if p.symbol == symbol and p.size != 0), None)
        if position is None:
            raise PositionNotFoundException(f"no {symbol} position")
        is_long = position.size > 0
        size = abs(position.size)
        exits, skipped = _exits(client.get_open_orders(symbol, address=target.address), is_long, size)
        result = risk.outcomes(
            is_long=is_long, entry=position.entry_price, size=size, exits=exits,
            fees=_fees(client, symbol, target), book=_book(client, symbol),
            account_value=_account_value(client, target), liquidation_price=position.liquidation_price,
            max_daily_loss_pct=_daily_loss(target))
        result["warnings"].extend(skipped)
        return {**_observation(client, symbol=symbol), "data": {"symbol": symbol, "unrealized_pnl": position.unrealized_pnl,
                         "liquidation_price": position.liquidation_price,
                         "leverage": position.leverage.value, "margin_mode": position.leverage.type, **result}}

    return _wallet_tool(
        registry, "get_position_risk",
        "Risk of an open perp position from its stop losses and take profits (its reduce-only open orders): "
        "per level the PnL after fees and estimated slippage, the total if every stop fills and if every "
        "take profit fills (in USD and % of the account), risk/reward, break-even win rate, the size without "
        "a stop, and warnings (no stop, a stop beyond liquidation or already crossed, the daily loss limit). "
        "PnL from the entry price. Use it rather than computing outcomes yourself: it includes fees and "
        "slippage. Read only. No position: POSITION_NOT_FOUND." + OBSERVATION_TEXT,
        {"symbol": {**SYMBOL, "description": "The position's market, exactly as Hyperliquid names it."}},
        read, required=["symbol"])


# ── get_account_risk ──────────────────────────────────────────────────

LOW_FREE_MARGIN_PCT = Decimal(10)  # warn below this % of the account value
HOUR_MS = 3_600_000
BETA_HOURS = 720  # 30 days of hourly candles


def _get_account_risk(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        positions = [p for p in client.get_positions(address=target.address) if p.size != 0]
        orders = client.get_open_orders(address=target.address)
        summary = client.get_account_summary(target.address)
        account_value = summary.account_value if isinstance(summary.account_value, Decimal) else None
        account = target.account
        btc = _hourly(client, "BTC") if arguments.get("with_beta") and positions else None
        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        stops_net = profits_net = long_notional = short_notional = Decimal(0)
        to_stops = Decimal(0)  # the PnL from now until every stop has filled
        weighted: Optional[Decimal] = Decimal(0) if btc is not None else None
        for position in positions:
            symbol, is_long, size = position.symbol, position.size > 0, abs(position.size)
            exits, skipped = _exits([o for o in orders if o.symbol == symbol], is_long, size)
            book = _book(client, symbol)
            result = risk.outcomes(
                is_long=is_long, entry=position.entry_price, size=size, exits=exits,
                fees=_fees(client, symbol, target), book=book, account_value=account_value,
                liquidation_price=position.liquidation_price)
            # Hyperliquid's own valuation at the mark price, which needs no order book.
            notional = abs(position.position_value)
            mark = notional / size
            if is_long:
                long_notional += notional
            else:
                short_notional += notional
            liquidation = position.liquidation_price
            distance = None if liquidation is None or not mark else risk.percent(abs(mark - liquidation) / mark)
            stops, profits = result["if_all_stops"], result["if_all_take_profits"]
            row_warnings = [*result["warnings"], *skipped, *_breaks(account, position, stops, account_value)]
            beta = None
            if btc is not None:
                beta = _beta(btc if symbol == "BTC" else _hourly(client, symbol), btc)
                if beta is None:
                    row_warnings.append("BTC beta not established (fewer than 24 hourly returns in common)")
                    weighted = None
                elif weighted is not None:
                    weighted += (notional if is_long else -notional) * beta
            if stops:
                stops_net += stops["net_pnl"]
                to_stops += stops["net_pnl"] - position.unrealized_pnl * stops["size"] / size
            profits_net += profits["net_pnl"] if profits else Decimal(0)
            rows.append({
                "symbol": symbol, "side": result["side"], "size": size, "entry_price": position.entry_price,
                "mark_price": risk.usd(mark) if mark >= 1 else mark, "mid_price": book.mid,
                "notional": risk.usd(notional), "unrealized_pnl": position.unrealized_pnl,
                "leverage": position.leverage.value, "margin_mode": position.leverage.type,
                "margin_used": position.margin_used, "liquidation_price": liquidation,
                "liquidation_distance_percent": distance, "if_all_stops": stops, "if_all_take_profits": profits,
                "risk_reward": result["risk_reward"], "unprotected_size": result["unprotected_size"],
                **({"beta_btc": beta} if btc is not None else {}), "warnings": row_warnings})
            warnings.extend(f"{symbol}: {w}" for w in row_warnings)
        pending, mids, pending_warnings = _pending_margin(client, target, orders)
        maximum = None if account is None else account.max_total_margin_pct
        margin, margin_warnings = _margin(summary, account_value, pending, maximum)
        warnings.extend(pending_warnings)
        warnings.extend(margin_warnings)
        portfolio = client.get_portfolio(target.address)
        change, capital = guards.portfolio_day_change(portfolio)
        loss = None if change is None else max(Decimal(0), -change)
        warnings.extend(_account_breaks(account, account_value, change, capital, to_stops))
        today = _today(portfolio, account, arguments)
        warnings.extend(_limit_breaks(account, portfolio, summary, orders, account_value, mids))
        return {**_observation(client), "data": {
            "positions": rows,
            "totals": {
                # As per position: the stops' percent_of_account is the loss (positive), the take profits' the gain.
                "if_all_stops": {"net_pnl": risk.usd(stops_net), "percent_of_account": _of(-stops_net, account_value)},
                "if_all_take_profits": {"net_pnl": risk.usd(profits_net),
                                        "percent_of_account": _of(profits_net, account_value)},
                "unprotected_positions": sum(1 for row in rows if row["unprotected_size"] > 0),
                "long_notional": risk.usd(long_notional), "short_notional": risk.usd(short_notional),
                "net_notional": risk.usd(long_notional - short_notional),
                **({"btc_beta_weighted_notional": None if weighted is None else risk.usd(weighted)}
                   if btc is not None else {})},
            "margin": margin,
            "daily_loss": {"loss_24h": None if loss is None else risk.usd(loss),
                           "percent_of_account": None if loss is None or not capital else risk.percent(loss / capital),
                           "max_daily_loss_pct": None if account is None else account.max_daily_loss_pct},
            "day_loss": today,
            "warnings": warnings,
            "notes": "PnL after fees and estimated slippage, per position as get_position_risk. Notional at the "
                     "mark price, in each market's collateral counted as USD. Positions of every dex this server "
                     "reads. Margin totals and budget cover those dexes; submission checks the account's traded "
                     "dexes. The remaining margin budget is a policy limit, not an exchange balance or a reservation.",
        }}

    return _wallet_tool(
        registry, "get_account_risk",
        "Risk of all open perp positions of an account together, also those opened outside this server (e.g. "
        "in the Hyperliquid app): per position its outcomes at its stop losses and take profits after fees and "
        "slippage (as get_position_risk), unprotected size, margin, liquidation price and distance from the "
        "mark; in total the loss if every stop fills and the gain if every take profit does (USD and % of the "
        "account), long, short and net notional, margin used and free, estimated pending entry margin and "
        "their total, the max_total_margin_pct budget and its remainder (negative when exceeded; null when "
        "unconfigured or unknown), the last 24 hours' loss against "
        "max_daily_loss_pct, today's loss (day_loss: since Hyperliquid's last record before the trading day's start, "
        "against the account value then), and warnings: no stop, a stop beyond liquidation, free margin below 10 %, "
        "and any position that breaks the account's guardrails (symbols, max_leverage, max_risk_pct, "
        "max_total_margin_pct, max_daily_loss_pct, also if every stop fills, max_day_loss_pct and "
        "max_total_notional_x in warn or block mode, by the config's day start). with_beta: also each "
        "position's BTC beta from 30 days of hourly candles and the BTC-beta-weighted net notional (one more "
        "request per position other than BTC, and one for BTC). Pending margin uses the submission calculation: "
        "remaining non-reduce-only perp orders at each market's current leverage; unavailable inputs leave it "
        "null with a warning. One leverage read per pending market, plus mid prices for pending sells. "
        "Positions and margin estimates of every dex this server reads. Read only." + OBSERVATION_TEXT,
        {"with_beta": {"type": "boolean", "default": False,
                       "description": "Also the BTC beta of each position (30 days of hourly candles)."},
         "day_start": {"type": "string", "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$",
                       "description": "For day_loss only: when the trading day starts, HH:MM (default: the "
                                      "account's day_start, else 00:00). The limits always use the config."},
         "tz": {"type": "string", "minLength": 1,
                "description": "For day_loss only: the IANA time zone of day_start, e.g. Europe/Athens (default: "
                               "the account's tz, else UTC)."}},
        read)


def _pending_margin(client: Any, target: Target, orders: list[Order]
                    ) -> tuple[Optional[Decimal], dict[str, Decimal], list[str]]:
    """The submission calculation, without a proposed order. Reuse sell mids for the notional audit.
    A failed extra read must not turn unknown exposure into zero or discard the position report.
    """
    pending = [order for order in orders if not order.reduce_only and order.market in PERP_MARKETS]
    mids: dict[str, Decimal] = {}
    try:
        if any(not order.is_buy for order in pending):
            mids = {symbol: _decimal(price) for symbol, price in client.get_price().items()}
        leverages = {symbol: client.get_leverage(symbol, address=target.address)
                     for symbol in dict.fromkeys(order.symbol for order in pending)}
        value = guards.pending_margin(pending, leverages, mids)
    except Exception as error:
        code = error_body(error)["error"]["code"]
        return None, mids, [f"pending entry margin could not be established ({code})"]
    if not value.is_finite():
        return None, mids, ["pending entry margin could not be established: an order's value or leverage is unknown"]
    return value, mids, []


def _margin(summary: Any, account_value: Optional[Decimal], pending: Optional[Decimal],
            maximum: Optional[Decimal]) -> tuple[dict[str, Any], list[str]]:
    """Margin used and free, by the account mode: a unified account's USDC available to trade;
    a standard account's withdrawable per dex (its spot balance backs no perp position)."""
    margin_used = summary.margin_used if isinstance(summary.margin_used, Decimal) \
        and summary.margin_used.is_finite() and summary.margin_used >= 0 else None
    warnings: list[str] = []
    dexes: Optional[list[dict[str, Any]]] = None
    free: Optional[Decimal]
    base: Optional[Decimal]
    if isinstance(summary.available_to_trade, Decimal):
        free, base = summary.available_to_trade, account_value
    elif summary.mode == "standard":
        dexes = [{"dex": d.dex or "default", "account_value": d.account_value, "margin_used": d.margin_used,
                  "withdrawable": d.withdrawable} for d in summary.dexes]
        known = [d for d in summary.dexes if d.withdrawable is not None and d.account_value is not None]
        free = sum((d.withdrawable for d in known), Decimal(0)) if known else None
        base = sum((d.account_value for d in known), Decimal(0)) if known else None
        for d in known:
            if d.margin_used > 0 and d.account_value and d.withdrawable * 100 / d.account_value < LOW_FREE_MARGIN_PCT:
                warnings.append(f"free margin on the {d.dex or 'default'} dex is "
                                f"{risk.percent(d.withdrawable / d.account_value)} % of its value, below "
                                f"{LOW_FREE_MARGIN_PCT} %")
    else:  # portfolio margin, unknown: no single free figure
        free = base = None
    if dexes is None and free is not None and base and free * 100 / base < LOW_FREE_MARGIN_PCT:
        warnings.append(f"free margin is {risk.percent(free / base)} % of the account value, below "
                        f"{LOW_FREE_MARGIN_PCT} %")
    margin: dict[str, Any] = {"account_value": account_value, "margin_used": margin_used, "free_margin": free,
              "free_margin_percent": None if free is None or not base else risk.percent(free / base)}
    total = None if margin_used is None or pending is None else margin_used + pending
    limit = account_value * maximum / 100 if maximum is not None and account_value is not None \
        and account_value.is_finite() and account_value > 0 else None
    margin.update(pending_margin=pending, total_margin=total, max_total_margin_pct=maximum, margin_limit=limit,
                  remaining_margin_budget=None if limit is None or total is None else limit - total)
    if maximum is not None:
        check = guards.total_margin(margin_used, pending, Decimal(0), account_value, maximum, "open orders")
        if not check.passed:
            warnings.append(f"{check.detail} (max_total_margin_pct)")
    if dexes is not None:
        margin["dexes"] = dexes
    return margin, warnings


def _of(amount: Decimal, account_value: Optional[Decimal]) -> Optional[Decimal]:
    return None if account_value is None or account_value <= 0 else risk.percent(amount / account_value)


def _breaks(account: Any, position: Any, stops: Optional[dict[str, Any]], account_value: Optional[Decimal]) -> list[str]:
    """The account's guardrails that the position breaks, however it was opened."""
    if account is None:
        return []
    found = []
    if account.symbols is not None and position.symbol not in account.symbols:
        found.append("not in the account's symbols")
    if account.max_leverage is not None and position.leverage.value > account.max_leverage:
        found.append(f"leverage {position.leverage.value} is above max_leverage {account.max_leverage}")
    if account.max_risk_pct is not None and stops is not None and account_value and account_value > 0:
        loss_pct = -stops["net_pnl"] * 100 / account_value  # from the cents, not the rounded percentage
        if loss_pct > account.max_risk_pct:
            found.append(f"the loss at its stops ({stops['percent_of_account']} % of the account) is beyond "
                         f"max_risk_pct ({account.max_risk_pct} %)")
    return found


def _account_breaks(account: Any, account_value: Optional[Decimal], change: Optional[Decimal],
                    capital: Optional[Decimal], to_stops: Decimal) -> list[str]:
    """The account-wide loss limits broken: the last 24 hours' loss, and that loss
    if every stop filled now (the PnL from now to the stops, gains of the day included)."""
    if account is None:
        return []
    found = []
    if account.max_daily_loss_pct is not None and change is not None and capital and capital > 0:
        loss_pct = max(Decimal(0), -change) * 100 / capital
        projected = max(Decimal(0), -(change + to_stops)) * 100 / capital
        if loss_pct >= account.max_daily_loss_pct:
            found.append(f"the last 24 hours' loss {risk.percent(loss_pct / 100)} % reached max_daily_loss_pct "
                         f"({account.max_daily_loss_pct} %): no new risk")
        elif projected >= account.max_daily_loss_pct:
            found.append(f"if every stop filled now, the last 24 hours' loss would be "
                         f"{risk.percent(projected / 100)} %, reaching max_daily_loss_pct ({account.max_daily_loss_pct} %)")
    return found


def _today(portfolio: Any, account: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """day_loss: the loss since Hyperliquid's last portfolio record at or before the trading
    day's start, by the call's day_start and tz, else the account's, else 00:00 UTC."""
    day_start = arguments.get("day_start") or (account.day_start if account is not None else "00:00")
    tz = arguments.get("tz") or (account.tz if account is not None else "UTC")
    try:
        state = guards.today(portfolio, day_start, tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolError("INVALID_ARGUMENT", f"tz: {tz!r} is no IANA time zone", hint="e.g. Europe/Athens or UTC") from None
    loss, capital = state["today_loss"], state["today_capital"]
    return {"start": iso(state["today_start"]), "since": iso(state["today_since"]),
            "loss": None if loss is None else risk.usd(loss),
            "percent_of_account": None if loss is None or not capital or capital <= 0 else risk.percent(loss / capital),
            "max_day_loss_pct": None if account is None else account.max_day_loss_pct}


def _limit_breaks(account: Any, portfolio: Any, summary: Any, orders: list[Any],
                  account_value: Optional[Decimal], mids: dict[str, Decimal]) -> list[str]:
    """max_day_loss_pct (by the config's day start) and max_total_notional_x broken now, in
    warn or block mode: trades made in the Hyperliquid app are not checked when made. The
    positions are the summary's, read after the open orders: an order that fills in between
    counts twice, never not at all."""
    if account is None:
        return []
    found = []
    if account.max_day_loss_pct is not None and account.max_day_loss_mode != "off":
        state = guards.RiskState(**guards.today(portfolio, account.day_start, account.tz))
        check = guards.today_loss(state, account.max_day_loss_pct, account.max_day_loss_mode)
        if not check.passed:
            found.append(f"{check.detail} (max_day_loss_pct, {account.max_day_loss_mode})")
    if account.max_total_notional_x is not None and account.max_total_notional_mode != "off":
        state = guards.RiskState(open_notional=guards.open_notional(summary, orders, mids), account_value=account_value)
        check = guards.total_notional(state, Decimal(0), account.max_total_notional_x, account.max_total_notional_mode,
                                      what=None)
        if not check.passed:
            found.append(f"{check.detail} (max_total_notional_x, {account.max_total_notional_mode})")
    return found


def _hourly(client: Any, symbol: str) -> list[Any]:
    """The closed hourly candles of the last 30 days."""
    now = time.time() * 1000
    return [c for c in client.get_candles(symbol, "1h", lookback=BETA_HOURS) if c.close_time <= now]


def _beta(asset: list[Any], btc: list[Any]) -> Optional[Decimal]:
    """The asset's beta to BTC from hourly returns in the same hours; None below a day of them."""
    a = {c.open_time: c.close for c in asset}
    b = {c.open_time: c.close for c in btc}
    hours = sorted(set(a) & set(b))
    pairs = [(float(a[t1] / a[t0] - 1), float(b[t1] / b[t0] - 1))
             for t0, t1 in zip(hours, hours[1:]) if t1 - t0 == HOUR_MS and a[t0] and b[t0]]
    if len(pairs) < 24:
        return None
    mean_a = sum(x for x, _ in pairs) / len(pairs)
    mean_b = sum(y for _, y in pairs) / len(pairs)
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in pairs)
    variance = sum((y - mean_b) ** 2 for _, y in pairs)
    return None if variance == 0 else Decimal(str(round(covariance / variance, 2)))


def _exits(orders: list[Any], is_long: bool, size: Decimal) -> tuple[list[risk.Exit], list[str]]:
    """The reduce-only orders that close the position: trigger orders (stop loss, take
    profit) at their trigger price, a resting limit order as a take profit at its price. A
    TP/SL for the whole position (set so in the Hyperliquid app) closes all of `size` when it
    triggers, whatever size it is listed with; one with a size of its own keeps that."""
    exits, skipped = [], []
    for order in orders:
        if not order.reduce_only or order.is_buy == is_long:
            continue
        left = order.size - order.filled_size
        if order.is_position_tpsl and left == 0:
            left = size
        trigger = order.order_type.trigger if order.order_type else None
        if order.type in ("stop_loss", "take_profit") and order.trigger_price is not None:
            market = bool(trigger and trigger.get("isMarket"))
            exits.append(risk.Exit(order.type, order.trigger_price, left, market=market, order_id=order.order_id,
                                   limit=None if market else order.limit_price))
        elif order.type == "limit" and order.limit_price is not None:
            exits.append(risk.Exit("take_profit", order.limit_price, left, market=False, resting=True,
                                   order_id=order.order_id))
        else:
            skipped.append(f"order {order.order_id} ({order.type}) is not counted")
    return exits, skipped


# ── plan_trade ───────────────────────────────────────────────────────


def _plan_trade(registry: AccountRegistry) -> ToolSpec:
    from .trade import ENTRY_TIF

    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        from .trade import preview_open_position
        client = registry.planning_client(target)
        symbol, is_long = arguments["symbol"], arguments["side"] == "long"
        side = arguments["side"]
        if ("stop_loss" in arguments) == ("stop_losses" in arguments):
            raise InvalidArgumentException("pass stop_loss or stop_losses, exactly one")
        raw_stops = ([{"price": arguments["stop_loss"], "fraction": 1}] if "stop_loss" in arguments
                     else arguments["stop_losses"])
        # The prices as they are sent: rounded as Hyperliquid requires (two can become one).
        stops = sent_levels(client, symbol, is_long, raw_stops)
        profits = sent_levels(client, symbol, is_long, arguments.get("take_profits", []))
        # A limit as it will be signed (rounded as Hyperliquid requires), as open_position checks it.
        limit = _decimal(client.prepare_order(symbol, 1, is_long, arguments["entry_price"])[1]) \
            if "entry_price" in arguments else None
        given = [key for key in ("size", "risk_usd", "risk_pct") if key in arguments]
        if len(given) != 1:
            raise InvalidArgumentException("pass exactly one of size, risk_usd and risk_pct")
        book, fees = _book(client, symbol), _fees(client, symbol, target)
        mid = book.mid
        if mid is None:
            raise InvalidArgumentException(f"{symbol} has no order book on both sides: nothing to plan against")
        tif = arguments.get("time_in_force", "Gtc" if limit is not None else "Ioc")
        if "time_in_force" in arguments and limit is None:
            raise InvalidArgumentException("time_in_force requires entry_price")
        reference = limit if limit is not None and tif != "Ioc" else mid
        for price, _ in stops:
            if (price >= reference) if is_long else (price <= reference):
                raise InvalidArgumentException(f"stop loss {price} must be {'below' if is_long else 'above'} the "
                                               f"entry ({reference}) for a {side}")
        for price, _ in profits:
            if (price <= reference) if is_long else (price >= reference):
                raise InvalidArgumentException(f"take profit {price} must be {'above' if is_long else 'below'} the "
                                               f"entry ({reference}) for a {side}")
        for what, levels in (("stop losses", stops), ("take profits", profits)):
            if sum((fraction for _, fraction in levels), Decimal(0)) > 1:
                raise InvalidArgumentException(f"the {what}' fractions add up to more than 1")
        # A limit entry on the passive side of the book rests (maker); else it executes now.
        resting = tif != "Ioc" and limit is not None and (
            (limit < book.asks[0][0]) if is_long else (limit > book.bids[0][0]))
        account_value = _account_value(client, target)
        decimals = int(client.market_specs[symbol]["size_decimals"])
        step = Decimal(1).scaleb(-decimals)
        limited = False
        if "size" in arguments:
            size = _decimal(arguments["size"]).quantize(step, rounding="ROUND_DOWN")
            risk_budget = None
        else:
            if sum((fraction for _, fraction in stops), Decimal(0)) != 1:
                raise InvalidArgumentException("sizing by risk needs stop losses for the whole size: their "
                                               "fractions must add up to 1")
            if "risk_usd" in arguments:
                risk_budget = _decimal(arguments["risk_usd"])
            elif account_value is None or account_value <= 0:
                raise InvalidArgumentException("risk_pct needs the account value, which this account mode does not "
                                               "establish: pass risk_usd")
            else:
                risk_budget = account_value * _decimal(arguments["risk_pct"]) / 100
            size, limited = risk.size_for_risk(is_long=is_long, limit=limit, resting=resting, stops=stops,
                                               risk=risk_budget, fees=fees, book=book, size_decimals=decimals)
        if size <= 0:
            raise InvalidArgumentException(f"the size rounds to 0 at {symbol}'s lot size ({step})"
                                           + (": the visible order book is too thin to estimate one" if limited else ""))
        estimated = risk.planned_entry(size, is_long=is_long, limit=limit, book=book)
        entry = mid if estimated is None else estimated
        entry_fee = (fees.maker if resting else fees.taker) * entry * size
        # Sized as open_position places them: each its fraction, the nearest of a kind the rest.
        legs = [(f"stop_loss_{i}", "stop_loss", price, fraction) for i, (price, fraction) in enumerate(stops, 1)]
        legs += [(f"take_profit_{i}", "take_profit", price, fraction) for i, (price, fraction) in enumerate(profits, 1)]
        exits, warnings = [], []
        for (_, kind, price, _), part in zip(legs, split_size(size, legs, decimals, is_long)):
            label = kind.replace("_", " ")
            if part <= 0:
                warnings.append(f"the {label} at {price} rounds to 0 at the lot size ({step}): left out")
                continue
            exits.append(risk.Exit(kind, price, part, market=True))
            if part * price < MIN_ORDER_USD:
                warnings.append(f"the {label} at {price} is worth {risk.usd(part * price)} USD, below Hyperliquid's "
                                f"minimum order of {MIN_ORDER_USD} USD (open_position refuses it)")
        result = risk.outcomes(is_long=is_long, entry=entry, size=size, exits=exits, fees=fees, book=book,
                               account_value=account_value, entry_fee=entry_fee, max_daily_loss_pct=_daily_loss(target))
        result["entry_price"] = entry.quantize(Decimal(1).scaleb(entry.adjusted() - 7))  # 8 significant digits
        result["warnings"].extend(warnings)
        notional = entry * size
        leverage = client.get_leverage(symbol, address=target.address)
        if estimated is None:
            result["warnings"].append("the entry is larger than the visible order book: its price is the mid")
        if limited:
            result["warnings"].append("the visible order book limited the size: a larger one could not be estimated")
        if notional < MIN_ORDER_USD:
            result["warnings"].append(f"the entry is worth {risk.usd(notional)} USD, below Hyperliquid's minimum "
                                      f"order of {MIN_ORDER_USD} USD")
        allowed = None if target.account is None else target.account.max_risk_pct
        if allowed is not None and account_value is not None and account_value > 0:
            # As open_position checks it, for the plan's size and stops: a market entry, or with a
            # limit from the limit at the taker fee (a Gtc limit can fill at once).
            loss = risk.planned_loss(size, is_long=is_long, limit=limit, resting=False, stops=stops, fees=fees,
                                     book=book, size_decimals=decimals)
            if loss is None or loss > account_value * allowed / 100:
                result["warnings"].append(f"the loss at the stop losses is above the account's max_risk_pct ({allowed} "
                                          "%), or not estimable: open_position would refuse it")
        opening: dict[str, Any] = {"symbol": symbol, "side": side, "size": float(size),
                                   "stop_losses": raw_stops}
        if target.name is not None:
            opening["account"] = target.name
        for key in ("take_profits", "time_in_force", "slippage"):
            if key in arguments:
                opening[key] = arguments[key]
        if limit is not None:
            opening["limit_price"] = float(limit)
        execution = preview_open_position(registry, client, target, opening)
        if tif == "Ioc" and limit is not None:
            result["warnings"].append("IOC limit estimates assume the requested size fills at the limit price; "
                                      "the entry may fill partly or not at all. Protection uses the filled size.")
        return {**_observation(client, symbol=symbol), "data": {
            "symbol": symbol,
            "entry": "limit (maker)" if resting else "limit (taker)" if limit is not None else "market (taker)",
            "execution": execution,
            "risk_budget": None if risk_budget is None else risk.usd(risk_budget),
            "notional": risk.usd(notional),
            "margin": risk.usd(notional / leverage.value), "leverage": leverage.value, "margin_mode": leverage.type,
            **result}}

    return _wallet_tool(
        registry, "plan_trade",
        "Plan a perp trade before entering it: with a stop loss (or several) and optional take profits, its "
        "outcomes as "
        "get_position_risk shows them (after fees and estimated slippage, the entry's fee included), and its "
        "size: as given, or one whose loss at the stops is at most risk_usd or risk_pct of the "
        "account under these estimates (today's order book and fees). Also the margin at the leverage the "
        "account has on the market, and warnings (orders below "
        "the 10 USD minimum, the daily loss limit). Use it rather than computing a size yourself. Read only: it "
        "places nothing; open it with open_position and the size it returns: its levels are sized the same "
        "way. data.execution previews the same local preparation and account guards as submission, with "
        "rounded open_position arguments, prepared entry, checks, used/pending/proposed margin, traded dex scope "
        "and observed_at (reads completed, not an atomic snapshot). eligible is true when local checks pass, "
        "false for a known blocker, null when not established or no trading policy applies. Warn-only failures "
        "do not block. trading_enabled reports this server's execution switch; a read-only server can still "
        "preview configured policy. No signer authorization or exchange acceptance is checked; submission "
        "rechecks and reserves nothing here. Multiple stops/targets work for market or Ioc limit entries; "
        "Gtc limits require a single full-size bracket. IOC estimates use the limit and taker fees for the "
        "requested size; the actual fill may be smaller or zero." + OBSERVATION_TEXT +
        " The top-level scope is the planner's actual account client, which can be narrower than account reports.",
        {"symbol": {**SYMBOL, "description": "The market, exactly as Hyperliquid names it."},
         "side": {"type": "string", "enum": ["long", "short"]},
         "stop_loss": {**PRICE, "description": "The stop loss's trigger price, for the whole size. Or stop_losses."},
         "stop_losses": {"type": "array", "minItems": 1, "maxItems": 10, "items": {
             "type": "object", "additionalProperties": False, "required": ["price", "fraction"],
             "properties": {"price": {**PRICE, "description": "The stop loss's trigger price."},
                            "fraction": {"type": "number", "exclusiveMinimum": 0, "maximum": 1,
                                         "description": "The share of the size it closes (0.5: half)."}}},
             "description": "Instead of stop_loss: several stop losses, each closing a share of the size; for "
                            "sizing by risk the shares add up to 1."},
         "take_profits": {"type": "array", "maxItems": 10, "items": {
             "type": "object", "additionalProperties": False, "required": ["price", "fraction"],
             "properties": {"price": {**PRICE, "description": "The take profit's trigger price."},
                            "fraction": {"type": "number", "exclusiveMinimum": 0, "maximum": 1,
                                         "description": "The share of the size it closes (0.5: half)."}}},
             "description": "Take profits, each closing a share of the size; the shares add up to at most 1."},
         "entry_price": {**PRICE, "description": "A limit entry at this price; without it a market entry."},
         "time_in_force": ENTRY_TIF,
         "slippage": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
                      "description": "Market-entry price tolerance, as in open_position; defaults to the account cap."},
         "size": {**PRICE, "description": "The size in base units, never USD. Or risk_usd / risk_pct."},
         "risk_usd": {**PRICE, "description": "Size the trade so it loses at most this at the stop."},
         "risk_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100,
                      "description": "Size the trade so it loses at most this % of the account at the stop."}},
        read, required=["symbol", "side"])


# ── reads ─────────────────────────────────────────────────────────────


def sent_levels(client: Any, symbol: str, is_long: bool, levels: list[dict[str, Any]]) -> list[tuple[Decimal, Decimal]]:
    """(price, fraction) of stop losses or take profits, each price as it is sent: rounded as
    Hyperliquid requires, the way the library rounds every order's price (prepare_order)."""
    return [(_decimal(client.prepare_order(symbol, 1, not is_long, level["price"])[1]), _decimal(level["fraction"]))
            for level in levels]


def _book(client: Any, symbol: str) -> risk.Book:
    raw = client.get_order_book(symbol, decimal=True)
    return risk.Book(bids=tuple((Decimal(level["price"]), Decimal(level["size"])) for level in raw["bids"]),
                     asks=tuple((Decimal(level["price"]), Decimal(level["size"])) for level in raw["asks"]))


def _fees(client: Any, symbol: str, target: Target) -> risk.Fees:
    rates = client.get_fee_rates(symbol, address=target.address)
    return risk.Fees(taker=rates.taker, maker=rates.maker)


def _account_value(client: Any, target: Target) -> Optional[Decimal]:
    """None where the account mode establishes no value (portfolio margin): no % then."""
    value = client.get_account_summary(target.address).account_value
    return value if isinstance(value, Decimal) else None


def _daily_loss(target: Target) -> Optional[Decimal]:
    return None if target.account is None else target.account.max_daily_loss_pct
