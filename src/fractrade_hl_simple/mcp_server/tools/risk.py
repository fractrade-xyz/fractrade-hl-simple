"""The risk tools: read only, they place nothing.

get_position_risk: an open position with its stop losses and take profits: what each level
makes or loses, what every stop and every take profit would, risk/reward, warnings.
plan_trade: the same for a trade before it is entered, and its size from a risk in USD or %
of the account. The arithmetic is risk.py; here the reads (position, open orders, order
book, fee rates, account value, leverage).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from ...exceptions import InvalidArgumentException, PositionNotFoundException
from ...hyperliquid import MIN_ORDER_USD, split_size
from .. import risk
from ..accounts import AccountRegistry, Target
from ..convert import decimal as _decimal
from ..server import ToolSpec
from .read import SYMBOL, _wallet_tool

PRICE = {"type": "number", "exclusiveMinimum": 0}


def risk_tools(registry: AccountRegistry) -> list[ToolSpec]:
    return [_get_position_risk(registry), _plan_trade(registry)]


# ── get_position_risk ─────────────────────────────────────────────────


def _get_position_risk(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = arguments["symbol"]
        position = next((p for p in client.get_positions(address=target.address)
                         if p.symbol == symbol and p.size != 0), None)
        if position is None:
            raise PositionNotFoundException(f"no {symbol} position")  # type: ignore[no-untyped-call]
        is_long = position.size > 0
        size = abs(position.size)
        exits, skipped = _exits(client.get_open_orders(symbol, address=target.address), is_long, size)
        result = risk.outcomes(
            is_long=is_long, entry=position.entry_price, size=size, exits=exits,
            fees=_fees(client, symbol, target), book=_book(client, symbol),
            account_value=_account_value(client, target), liquidation_price=position.liquidation_price,
            max_daily_loss_pct=_daily_loss(target))
        result["warnings"].extend(skipped)
        return {"data": {"symbol": symbol, "unrealized_pnl": position.unrealized_pnl,
                         "liquidation_price": position.liquidation_price,
                         "leverage": position.leverage.value, "margin_mode": position.leverage.type, **result}}

    return _wallet_tool(
        registry, "get_position_risk",
        "Risk of an open perp position from its stop losses and take profits (its reduce-only open orders): "
        "per level the PnL after fees and estimated slippage, the total if every stop fills and if every "
        "take profit fills (in USD and % of the account), risk/reward, break-even win rate, the size without "
        "a stop, and warnings (no stop, a stop beyond liquidation or already crossed, the daily loss limit). "
        "PnL from the entry price. Use it rather than computing outcomes yourself: it includes fees and "
        "slippage. Read only. No position: POSITION_NOT_FOUND.",
        {"symbol": {**SYMBOL, "description": "The position's market, exactly as Hyperliquid names it."}},
        read, required=["symbol"])


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
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol, is_long = arguments["symbol"], arguments["side"] == "long"
        side = arguments["side"]
        if ("stop_loss" in arguments) == ("stop_losses" in arguments):
            raise InvalidArgumentException("pass stop_loss or stop_losses, exactly one")
        raw_stops = ([{"price": arguments["stop_loss"], "fraction": 1}] if "stop_loss" in arguments
                     else arguments["stop_losses"])
        # The prices as they are sent: rounded as Hyperliquid requires (two can become one).
        stops = sent_levels(client, symbol, is_long, raw_stops)
        profits = sent_levels(client, symbol, is_long, arguments.get("take_profits", []))
        limit = _decimal(arguments["entry_price"]) if "entry_price" in arguments else None
        given = [key for key in ("size", "risk_usd", "risk_pct") if key in arguments]
        if len(given) != 1:
            raise InvalidArgumentException("pass exactly one of size, risk_usd and risk_pct")
        book, fees = _book(client, symbol), _fees(client, symbol, target)
        mid = book.mid
        if mid is None:
            raise InvalidArgumentException(f"{symbol} has no order book on both sides: nothing to plan against")
        reference = limit if limit is not None else mid
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
        resting = limit is not None and ((limit < book.asks[0][0]) if is_long else (limit > book.bids[0][0]))
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
                warnings.append(f"the {label} at {price} is worth {risk._usd(part * price)} USD, below Hyperliquid's "
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
            result["warnings"].append(f"the entry is worth {risk._usd(notional)} USD, below Hyperliquid's minimum "
                                      f"order of {MIN_ORDER_USD} USD")
        allowed = None if target.account is None else target.account.max_risk_pct
        if allowed is not None and account_value is not None and account_value > 0:
            # Exactly as open_position checks it (unrounded), for the plan's size and stops.
            loss = risk.planned_loss(size, is_long=is_long, limit=None, resting=False, stops=stops, fees=fees,
                                     book=book, size_decimals=decimals)
            if loss is None or loss > account_value * allowed / 100:
                result["warnings"].append(f"the loss at the stop losses is above the account's max_risk_pct ({allowed} "
                                          "%), or not estimable: open_position would refuse it")
        return {"data": {
            "symbol": symbol,
            "entry": "limit (maker)" if resting else "limit (taker)" if limit is not None else "market (taker)",
            "risk_budget": None if risk_budget is None else risk._usd(risk_budget),
            "notional": risk._usd(notional),
            "margin": risk._usd(notional / leverage.value), "leverage": leverage.value, "margin_mode": leverage.type,
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
        "way.",
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
