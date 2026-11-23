"""Trade tools: one tool per library method, with its name, parameters,
defaults and units. Registered only when trading is switched on (--enable-trading).

Every tool runs the same steps, under the lock of the account's exchange account: refuse it
while the account is halted (unless it reduces risk), check the signer, prepare the
order with the library (`prepare_order`: the size and price it signs), check the guards
on that order, reserve a cloid the agent passed, send, and write one line to the server log.
Trading behaviour is the library's: a tool never re-implements it.
"""

from __future__ import annotations

import logging
import time
from functools import partial
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from hyperliquid.utils.types import Cloid

from ... import _answers
from ..._inputs import MIN_ORDER_USD
from ..._protection import prepare_levels, protection_legs
from ...exceptions import (
    ActionOutcomeUnknownException, ActionRejectedException, AuthenticationRequiredException, HyperliquidException,
    InvalidArgumentException, OrdersNotCanceledException, PositionNotFoundException, UnknownSymbolException,
)
from ...models import PERP_MARKETS, Order, ProtectionSync
from .. import convert, guards
from .. import risk as risk_math
from ..accounts import AccountRegistry, Target, require_enabled_dexes
from ..errors import ToolError, error_body, library_details, scrub
from ..server import ToolSpec

logger = logging.getLogger(__name__)

ACCOUNT = {"type": "string", "description": "A configured account that can trade (see list_accounts). "
                                            "Without it: the only account that can trade."}
SYMBOL = {"type": "string", "minLength": 1,
          "description": "The perp market, exactly as Hyperliquid names it (BTC, xyz:NVDA)."}
SIZE = {"type": "number", "exclusiveMinimum": 0,
        "description": "In base units (coins or contracts), never USD; rounded down to the market's lot size."}
PRICE_TEXT = ("In USD (the market's collateral); rounded as Hyperliquid requires (5 significant figures; "
              "from 100000 on, a whole number).")
PRICE = {"type": "number", "exclusiveMinimum": 0, "description": PRICE_TEXT}
CLOID = {"type": "string", "pattern": "^0x[0-9a-fA-F]{32}$",
         "description": "Client order id: 0x and 32 hex digits, unique per order. Optional: without it the server "
                        "makes one. One you choose is known before sending, so the order can be looked up with "
                        "get_order_status_by_cloid even if this call's answer is lost. A cloid used before for "
                        "this account (passed or made, by any server of this config, in the last 30 days) is refused, "
                        "unless that order was provably not sent."}
ORDER_ID = {"type": "integer", "minimum": 0, "description": "The order id (oid), as get_open_orders shows it."}
REDUCE_ONLY = {"type": "boolean", "default": False,
               "description": "Only reduce a position: Hyperliquid refuses the order if it would increase one."}
# Said by each tool that places orders that add risk (guards: limits with a mode).
WARN = (" A limit in warn mode that breaks, or cannot be checked, does not itself block the call; if the call "
        "succeeds, it is in warnings: [{rule, detail}] besides data.")
HALTED_HINT = ("while halted only risk-reducing calls run: close, close_all_positions, stop_loss, take_profit, "
               "update_stop_loss, update_take_profit, sync_protection, orders with reduce_only, cancels; the halt "
               "applies to every server of this config that can trade and lasts until none of them runs (quit "
               "every chat or the app); a read-only server does not keep it")


def trade_tools(registry: AccountRegistry) -> list[ToolSpec]:
    """The trade tools, in the order clients list them."""
    return [
        _order_tool(registry, "buy", is_buy=True),
        _order_tool(registry, "sell", is_buy=False),
        _place_orders(registry),
        _place_ladder(registry),
        _open_position(registry),
        _place_trigger_entry(registry),
        _modify_order(registry),
        _cancel_order(registry),
        _cancel_order_by_cloid(registry),
        _cancel_orders(registry),
        _close(registry),
        _close_all_positions(registry),
        _protection_tool(registry, "stop_loss"),
        _protection_tool(registry, "take_profit"),
        _update_protection(registry, "update_stop_loss"),
        _update_protection(registry, "update_take_profit"),
        _sync_protection(registry),
        _cancel_all_orders(registry),
        _set_leverage(registry),
        _kill_switch(registry),
    ]


Act = Callable[[Any, Target, dict[str, Any]], tuple[Any, str]]
"""A tool's own part: (client, target, arguments) -> (data, outcome for the server log)."""


def _trade_tool(registry: AccountRegistry, name: str, description: str, properties: dict[str, Any],
                required: list[str], act: Act, *, reduces_risk: Callable[[dict[str, Any]], bool],
                idempotent: bool = False, destructive: bool = True,
                check: Optional[Callable[[dict[str, Any]], None]] = None,
                symbols: Optional[Callable[[dict[str, Any]], list[str]]] = None) -> ToolSpec:
    """A trade tool: the steps every one runs (module docstring), around `act`. `check`: the
    tool's own argument rules the schema cannot say, run first: an argument error is
    INVALID_ARGUMENT before the halt, the signer or any read. `symbols`: the markets of a
    batch (else the one `symbol`)."""
    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        target = registry.resolve_trade(arguments.get("account"))
        symbol = arguments.get("symbol", "-")
        try:
            if check is not None:
                check(arguments)
            names = symbols(arguments) if symbols is not None else (
                [arguments["symbol"]] if "symbol" in arguments else [])
            symbol = ",".join(dict.fromkeys(names)) or "-"
            if names:  # a market of a dex the account does not trade: say how to enable it
                require_enabled_dexes(registry.account_perp_dexs(_config(target)), names)
            with registry.lock(target), guards.collect_warnings() as warnings:
                if registry.is_halted(target) and not reduces_risk(arguments):
                    raise ToolError("KILL_SWITCH_ENGAGED", f"account {target.name} is halted", hint=HALTED_HINT)
                # Looked up before every action; one that reduces risk uses the confirmed
                # result when Hyperliquid cannot be asked, so a close still works then.
                registry.check_signer(target, reduces_risk=reduces_risk(arguments))
                data, outcome = act(registry.trade_client(target), target, arguments)
        except Exception as error:  # the symbol as %r: one line, whatever the agent sent
            code = error.code if isinstance(error, HyperliquidException) else "INTERNAL"  # its traceback: once, by the server
            logger.info("%s %r (account %s): %s", name, symbol, target.name, code)
            raise
        logger.info("%s %r (account %s): %s", name, symbol, target.name, outcome)
        envelope = {"account": target.name, "address": target.address, "base_url": target.base_url, "data": data}
        # The limits in warn mode that the call broke, or could not check: it went through.
        return {**envelope, "warnings": warnings} if warnings else envelope

    schema = {"type": "object", "properties": {"account": ACCOUNT, **properties},
              "required": required, "additionalProperties": False}
    return ToolSpec(name=name, description=description, input_schema=schema, run=run,
                    read_only=False, destructive=destructive, idempotent=idempotent)


def _placed(order: Any) -> tuple[dict[str, Any], str]:
    return convert.placed_order(order), f"order {order.order_id} {order.status}"


# Raised by the library before anything is sent: an order failing so can be placed again
# under the same cloid. So can one failing with a retryable error: for an order only a rate
# limit or a connect timeout, which the exchange did not process.
NOT_SENT = (InvalidArgumentException, PositionNotFoundException, AuthenticationRequiredException)


def _not_sent(error: BaseException) -> bool:
    """Whether the library raised `error` before sending, or Hyperliquid provably did not process it."""
    return isinstance(error, NOT_SENT) or getattr(error, "retryable", False)


def _placing(registry: AccountRegistry, target: Target, cloid: Any, place: Callable[[str], Any]) -> Any:
    """Place one order under a reserved cloid (the agent's, or a new one): released if the
    order was not sent; else every cloid in the outcome stays used."""
    reserved = _before_sending(lambda: registry.reserve_cloid(target, cloid))
    try:
        result = place(reserved)
    except Exception as error:
        if _not_sent(error) or _blocked(error):
            _after_sending(partial(registry.release_cloid, target, reserved))
        else:
            _after_sending(partial(_remember, registry, target, error))
        raise
    _after_sending(partial(_remember, registry, target, result))
    return result


def _counted(registry: AccountRegistry, target: Target, count: int, send: Callable[[], Any]) -> Any:
    """Send an order (or a batch); the `count` orders of it that add risk count for
    max_orders_per_hour (True: one): counted before it is sent, so a crash after sending
    cannot leave them uncounted, and uncounted when it provably was not sent (NOT_SENT, or
    retryable)."""
    counted = _before_sending(lambda: registry.count_risk_order(target, int(count))) if count else None
    try:
        return send()
    except Exception as error:
        if counted is not None and (_not_sent(error)):
            _after_sending(lambda: registry.release_risk_order(target, counted, int(count)))
        raise


def _blocked(error: Exception) -> bool:
    """GUARD_BLOCKED: refused before the order went out, by a guard or the bookkeeping."""
    return isinstance(error, ToolError) and error.code == "GUARD_BLOCKED"


def _before_sending(write: Callable[[], Any]) -> Any:
    """Shared-state bookkeeping before an order goes out: if it cannot be written (a full
    disk), the order is not sent, and the error says so."""
    try:
        return write()
    except OSError as error:
        raise ToolError("GUARD_BLOCKED", f"the shared state cannot be written ({error.strerror or error}); the "
                                         "order was not sent") from error


def _after_sending(write: Callable[[], Any]) -> None:
    """Shared-state bookkeeping after an order was sent (or provably not): never in place of
    the order's outcome, which the agent must see as it is. Any failure is logged."""
    try:
        write()
    except OSError as error:
        logger.error("the shared state could not be written after an order: %s", error.strerror or error)
    except Exception:
        logger.exception("the shared state could not be written after an order")


def _remembering(registry: AccountRegistry, target: Target, call: Callable[[], Any]) -> Any:
    """Run a library call that places orders under cloids it makes itself (close,
    update_stop_loss, ...): each one in its outcome is remembered as used, as the agent may
    see it in the result or in an error's details."""
    try:
        result = call()
    except Exception as error:
        _after_sending(partial(_remember, registry, target, error))
        raise
    _after_sending(partial(_remember, registry, target, result))
    return result


def _remember(registry: AccountRegistry, target: Target, value: Any) -> None:
    """Mark every cloid in `value` as used, in one write."""
    cloids = _cloids_in(value)
    if cloids:
        registry.remember_cloids(target, cloids)


def _cloids_in(value: Any, seen: frozenset[int] = frozenset()) -> list[str]:
    """Every cloid in `value`: of each order (an order, a mapping of them), and of an
    exception's documented attributes, its own `cloid` included (OrderOutcomeUnknownException:
    the order may still arrive). Each object is visited once on a path, so a cycle ends."""
    if id(value) in seen:
        return []
    seen = seen | {id(value)}
    if isinstance(value, Order):
        return [value.cloid] if value.cloid else []
    if isinstance(value, Mapping):  # its own cloid: e.g. a level of PROTECTION_NOT_PLACED that may still arrive
        own = [value["cloid"]] if isinstance(value.get("cloid"), str) else []
        return own + [cloid for item in value.values() for cloid in _cloids_in(item, seen)]
    if isinstance(value, (list, tuple)):
        return [cloid for item in value for cloid in _cloids_in(item, seen)]
    if isinstance(value, HyperliquidException):
        return _cloids_in(library_details(value), seen)
    if isinstance(value, ProtectionSync):  # resize_protection's result: its placed orders
        return _cloids_in(value.placed, seen)
    return []


# ── buy, sell ─────────────────────────────────────────────────────────


SIZE_OR_NOTIONAL = {**SIZE, "description": "In base units (coins or contracts); or pass notional_usd instead. "
                                            "Rounded down to the market's lot size."}
NOTIONAL_USD = {"type": "number", "exclusiveMinimum": 0,
                "description": "Instead of size: the order's value in USD (the market's collateral). The size is "
                               "notional_usd divided by the limit price as Hyperliquid rounds it (for a market order "
                               "the current mid), rounded down to the lot size, so the order is worth at most this "
                               "much at that price. Hyperliquid's minimum is 10 USD after rounding: pass a little "
                               "more."}


def _size(client: Any, symbol: str, is_buy: bool, arguments: dict[str, Any], limit_price: Optional[float]) -> Any:
    """The order's size in base units: `size` as given, or `notional_usd` divided by the
    price as it will be signed (a limit order's rounded limit price; a market order's mid,
    read now), rounded down to the lot size. Call it after the arguments are checked and
    before any cloid is reserved: it can read the mid."""
    size, notional = arguments.get("size"), arguments.get("notional_usd")
    if size is not None:
        return size
    lot = _lot(client, symbol)
    if limit_price is None:
        # The library's own checks of the symbol (UNKNOWN_SYMBOL) before the mid is read: they
        # make no request with a price given, and the price here is only nominal.
        client.prepare_order(symbol, float(lot), is_buy, 1.0)
        price = convert.decimal(client.get_price(symbol))
    else:  # the limit price as the library signs it (rounding can raise it): size it at that price
        price = convert.decimal(client.prepare_order(symbol, float(lot), is_buy, limit_price)[1])
    if not price.is_finite() or price <= 0:
        raise ToolError("INVALID_ARGUMENT", f"no usable price for {symbol} to size notional_usd at ({price})")
    units = (convert.decimal(notional) / price).quantize(lot, rounding=ROUND_DOWN)
    if units < lot:
        smallest = format((lot * price).normalize(), "f")
        raise ToolError("INVALID_ARGUMENT", f"notional_usd {notional} is less than one lot of {symbol} ({lot}) at "
                                            f"{price}", hint=f"at least {smallest} USD; Hyperliquid's minimum is 10 USD")
    return float(units)  # the library compares sizes with its float lot size


def _lot(client: Any, symbol: str) -> Decimal:
    """The market's lot size; 1 for a symbol the client does not know (prepare_order and
    get_price refuse it with UNKNOWN_SYMBOL)."""
    specs = client.market_specs.get(symbol) or {}
    return Decimal(1).scaleb(-int(specs.get("size_decimals", 0)))


def _one_size(arguments: dict[str, Any]) -> None:
    """size or notional_usd, exactly one (a JSON Schema oneOf at the top level is refused by
    some clients' APIs, so it is checked here)."""
    if ("size" in arguments) == ("notional_usd" in arguments):
        raise ToolError("INVALID_ARGUMENT", "pass size or notional_usd, exactly one")


def _order_arguments(arguments: dict[str, Any]) -> None:
    """buy, sell: the argument rules the schema cannot say."""
    _one_size(arguments)
    if arguments.get("post_only", False) and arguments.get("limit_price") is None:
        raise ToolError("INVALID_ARGUMENT", "post_only: only for a limit order", hint="pass limit_price too")


def _order_tool(registry: AccountRegistry, name: str, is_buy: bool) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, limit_price = arguments["symbol"], arguments.get("limit_price")
        reduce_only, post_only = arguments.get("reduce_only", False), arguments.get("post_only", False)
        slippage, market = arguments.get("slippage"), limit_price is None
        size, price = client.prepare_order(symbol, _size(client, symbol, is_buy, arguments, limit_price), is_buy,
                                           limit_price, slippage)
        risk = None if reduce_only else _risk_state(registry, client, target, symbol, is_buy)
        guards.check_order(_config(target), symbol, size, price, is_buy=is_buy, reduce_only=reduce_only,
                           market=market, slippage_value=slippage, risk=risk).enforce()
        # The prepared size and price, as buy()/sell() would sign them: IOC for a market order.
        order = _placing(registry, target, arguments.get("cloid"), lambda cloid: _counted(
            registry, target, not reduce_only, lambda: client.create_order(
                symbol, size, is_buy, limit_price=price, reduce_only=reduce_only, post_only=post_only,
                time_in_force="Ioc" if market else None, cloid=cloid)))
        return _placed(order)

    side = "Buy" if is_buy else "Sell"
    return _trade_tool(
        registry, name,
        f"{side} a perp. Without limit_price a market order: immediate or cancel at the mid price "
        f"{'plus' if is_buy else 'minus'} slippage (default and maximum: the account's max_slippage), so it can "
        "fill partly; with limit_price a limit order that rests until filled or canceled. Size in base units "
        "(size), or in USD (notional_usd), exactly one. Checked against the account's limits first "
        "(GUARD_BLOCKED; a market buy is valued at its worst price, the mid plus slippage, so notional_usd at "
        "exactly max_order_usd can be blocked)." + WARN + " Returns the order with the fields of get_open_orders "
        "(created_at null: Hyperliquid's answer has no time), status open or filled and average_fill_price. On "
        "OUTCOME_UNKNOWN never call again: follow the hint.",
        {"symbol": SYMBOL, "size": SIZE_OR_NOTIONAL, "notional_usd": NOTIONAL_USD,
         "limit_price": {**PRICE, "description": "The limit price; without it a market order. " + PRICE_TEXT},
         "reduce_only": REDUCE_ONLY,
         "post_only": {"type": "boolean", "default": False,
                       "description": "Limit orders only: rest on the book as maker, or be rejected (ORDER_REJECTED) "
                                      "if it would match at once."},
         "slippage": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
                      "description": "Market orders only: the worst price as a fraction from the mid (0.01 = 1 %), "
                                     "at most the account's max_slippage."},
         "cloid": CLOID},
        ["symbol"], act, reduces_risk=lambda arguments: arguments.get("reduce_only", False), check=_order_arguments)


# ── place_orders, place_ladder ────────────────────────────────────────

MAX_BATCH = 40  # orders per call: the server's own bound (Hyperliquid states none)
BATCH_OUTCOME_HINT = ("do not place the batch again: look each order up with get_order_status_by_cloid "
                      "(details.cloids, in the order sent); an order not found yet can still arrive")
POST_ONLY = {"type": "boolean", "default": False,
             "description": "Rest on the book as maker, or be rejected (that order alone) if it would match at once."}


def _place_batch(registry: AccountRegistry, client: Any, target: Target,
                 orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Limit orders in one action (the library's bulk_order), each prepared as buy and sell
    prepare one, checked by guards.check_batch, under cloids reserved together. Each order
    is placed or rejected on its own: the result has one row per order, in the order sent.
    `orders`: symbol, is_buy, size, limit_price, and optional reduce_only, post_only, cloid."""
    prepared = []
    for order in orders:
        size, price = client.prepare_order(order["symbol"], order["size"], order["is_buy"], order["limit_price"])
        prepared.append(guards.BatchOrder(order["symbol"], order["is_buy"], size, price,
                                          order.get("reduce_only", False)))
    adding = [(order.symbol, order.is_buy) for order in prepared if not order.reduce_only]
    states = iter(_risk_states(registry, client, target, adding) if adding else [])
    guards.check_batch(_config(target), prepared,
                       [None if order.reduce_only else next(states) for order in prepared]).enforce()
    tifs = ["Alo" if order.get("post_only", False) else "Gtc" for order in orders]

    def send(cloids: list[str]) -> Any:
        try:
            return client.bulk_order([
                {"symbol": order.symbol, "is_buy": order.is_buy, "size": order.size, "limit_price": order.price,
                 "reduce_only": order.reduce_only, "time_in_force": tif, "cloid": cloid}
                for order, tif, cloid in zip(prepared, tifs, cloids)])
        except ActionOutcomeUnknownException as error:  # the library's error carries no cloids: the agent needs them
            raise ToolError("OUTCOME_UNKNOWN", str(error), hint=BATCH_OUTCOME_HINT,
                            details={"action": error.action, "cloids": cloids}) from error

    cloids, response = _placing_batch(registry, target, [order.get("cloid") for order in orders], len(adding), send)
    try:  # one documented status per order (bulk_order checks); the batch was sent whatever fails here
        statuses = response["response"]["data"]["statuses"]
        rows = [_batch_row(order, tif, cloid, status)
                for order, tif, cloid, status in zip(prepared, tifs, cloids, statuses, strict=True)]
    except Exception as error:
        logger.error("a batch's answer could not be read", exc_info=error)
        raise ToolError("OUTCOME_UNKNOWN", f"the batch was sent, but its answer could not be read "
                                           f"({type(error).__name__})", hint=BATCH_OUTCOME_HINT,
                        details={"action": "bulk order", "cloids": cloids}) from error
    return {"orders": rows, **{status: sum(1 for row in rows if row["status"] == status)
                               for status in ("open", "filled", "rejected")}}


def _placing_batch(registry: AccountRegistry, target: Target, cloids: list[Optional[str]], adding: int,
                   send: Callable[[list[str]], Any]) -> tuple[list[str], Any]:
    """_placing for a batch: every cloid (the agent's, or a new one) reserved in one write,
    the orders that add risk counted together; all released if the batch was not sent."""
    reserved = _before_sending(lambda: registry.reserve_cloids(target, cloids))
    try:
        return reserved, _counted(registry, target, adding, lambda: send(reserved))
    except Exception as error:
        if _not_sent(error) or _blocked(error):
            _after_sending(lambda: registry.release_cloids(target, reserved))
        raise


def _batch_row(order: guards.BatchOrder, tif: str, cloid: str, status: Any) -> dict[str, Any]:
    """One order of a batch's result: the placed order, as buy and sell return one (the
    library reads its status as it reads a single order's), or the order rejected alone,
    with the code and Hyperliquid's reason (scrubbed and cut, like an error's message)."""
    answer = {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}
    try:
        placed = _answers.order_from_response(answer, order.symbol, order.is_buy, order.size, order.price,
                                             {"limit": {"tif": tif}}, order.reduce_only, f"{order.symbol} order",
                                             cloid=Cloid(cloid))
    except ActionRejectedException as rejection:
        return {"status": "rejected", "symbol": order.symbol, "side": convert.side(order.is_buy),
                "size": convert.decimal(order.size), "limit_price": convert.decimal(order.price),
                "reduce_only": order.reduce_only, "time_in_force": tif, "cloid": cloid, "code": rejection.code,
                "reason": scrub(rejection.reason)}
    return convert.placed_order(placed)


def _batch_outcome(data: dict[str, Any]) -> str:
    return f"{len(data['orders'])} orders: {data['open']} open, {data['filled']} filled, {data['rejected']} rejected"


def _batch_cloids(orders: list[dict[str, Any]]) -> None:
    """A cloid given twice in one batch (in any case of its hex digits): refused at once."""
    given = [order["cloid"].lower() for order in orders if "cloid" in order]
    if len(set(given)) != len(given):
        raise ToolError("INVALID_ARGUMENT", "a cloid is given twice in the batch", hint="each order needs its own cloid")


BATCH_ORDER = {
    "type": "object", "additionalProperties": False, "required": ["symbol", "is_buy", "size", "limit_price"],
    "properties": {
        "symbol": SYMBOL, "is_buy": {"type": "boolean", "description": "true: a buy order; false: a sell order."},
        "size": SIZE, "limit_price": {**PRICE, "description": "The limit price. " + PRICE_TEXT},
        "reduce_only": REDUCE_ONLY, "post_only": POST_ONLY, "cloid": CLOID}}


def _place_orders(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        data = _place_batch(registry, client, target, arguments["orders"])
        return data, _batch_outcome(data)

    return _trade_tool(
        registry, "place_orders",
        f"Place up to {MAX_BATCH} limit orders in one request, on one or several perps: each rests until filled or "
        "canceled (with post_only: as maker, or that order is rejected). Every order is checked against the "
        "account's limits as buy and sell check one; the limits about the account (max_total_margin_pct, the daily "
        "loss, max_orders_per_hour) see the batch's orders that add risk together, as if all fill. One failing "
        "blocking check stops the whole batch (GUARD_BLOCKED, nothing sent)." + WARN + " Hyperliquid places or "
        "rejects each order on its "
        "own: returns orders in the order sent, each open, filled, or rejected with code and reason, and the "
        "counts. A batch of only reduce_only orders is risk-reducing (allowed while halted). On OUTCOME_UNKNOWN "
        "never call again: look each order up by its cloid (details.cloids).",
        {"orders": {"type": "array", "minItems": 1, "maxItems": MAX_BATCH, "items": BATCH_ORDER,
                    "description": "The orders, placed in this order."}},
        ["orders"], act, reduces_risk=lambda arguments: all(o.get("reduce_only", False) for o in arguments["orders"]),
        check=lambda arguments: _batch_cloids(arguments["orders"]),
        symbols=lambda arguments: [order["symbol"] for order in arguments["orders"]])


def _plain(value: Decimal) -> Decimal:
    """A sum without trailing zeros (89.66500 is 89.665, 100.0 is 100)."""
    return Decimal(format(value.normalize(), "f"))


def _ladder_arguments(arguments: dict[str, Any]) -> None:
    _one_size(arguments)
    if convert.decimal(arguments["from_price"]) == convert.decimal(arguments["to_price"]):
        raise ToolError("INVALID_ARGUMENT", "from_price and to_price are the same: a ladder needs two prices",
                        hint="one order at one price: buy or sell")


def _place_ladder(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, is_buy, steps = arguments["symbol"], arguments["is_buy"], arguments["steps"]
        start, end = convert.decimal(arguments["from_price"]), convert.decimal(arguments["to_price"])
        sizing: dict[str, Any] = {key: arguments[key] for key in ("size", "notional_usd") if key in arguments}
        orders = []
        for step in range(steps):  # evenly spaced, both ends included
            price = float(start + (end - start) * step / (steps - 1))
            orders.append({"symbol": symbol, "is_buy": is_buy, "limit_price": price,
                           "size": _size(client, symbol, is_buy, sizing, price),
                           "reduce_only": arguments.get("reduce_only", False),
                           "post_only": arguments.get("post_only", False)})
        data = _place_batch(registry, client, target, orders)
        sent = [(row["size"], row["limit_price"]) for row in data["orders"]]
        data.update(submitted_size=_plain(sum((size for size, _ in sent), Decimal(0))),
                    submitted_value=_plain(sum((size * price for size, price in sent), Decimal(0))),
                    filled_size=_plain(sum((row["filled_size"] for row in data["orders"] if row["status"] == "filled"),
                                           Decimal(0))))
        return data, _batch_outcome(data)

    return _trade_tool(
        registry, "place_ladder",
        f"Place a ladder of limit orders in one request: steps orders (2 to {MAX_BATCH}) on one perp at prices "
        "evenly spaced from from_price to to_price, both included, each of size (base units) or notional_usd (USD, "
        "divided by that step's price as Hyperliquid rounds it, rounded down to the lot size). Placed and checked as "
        "place_orders places and checks them; returns its result plus submitted_size and submitted_value (of every "
        "step sent, rejected ones included) and filled_size (of the steps that filled at once). Prices are not "
        "checked against the market: steps on the wrong side of the mid fill at once as taker, or with post_only "
        "are rejected.",
        {"symbol": SYMBOL, "is_buy": {"type": "boolean", "description": "true: buy orders; false: sell orders."},
         "from_price": {**PRICE, "description": "The first step's price. " + PRICE_TEXT},
         "to_price": {**PRICE, "description": "The last step's price. " + PRICE_TEXT},
         "steps": {"type": "integer", "minimum": 2, "maximum": MAX_BATCH, "description": "How many orders."},
         "size": {**SIZE, "description": "Of each step, in base units; or notional_usd instead. Rounded down to the "
                                         "market's lot size."},
         "notional_usd": {**NOTIONAL_USD, "description": "Instead of size: each step's value in USD, divided by that "
                                                         "step's price as Hyperliquid rounds it, rounded down to the "
                                                         "lot size."},
         "reduce_only": REDUCE_ONLY, "post_only": POST_ONLY},
        ["symbol", "is_buy", "from_price", "to_price", "steps"], act,
        reduces_risk=lambda arguments: arguments.get("reduce_only", False), check=_ladder_arguments)


# ── open_position ─────────────────────────────────────────────────────


def _levels(what: str, adds_up: str, of: str = "the filled size", price: Optional[str] = None) -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "maxItems": 10, "items": {
        "type": "object", "additionalProperties": False, "required": ["price", "fraction"],
        "properties": {"price": {**PRICE, "description": (price or f"The {what}'s trigger price.") + " " + PRICE_TEXT},
                       "fraction": {"type": "number", "exclusiveMinimum": 0, "maximum": 1,
                                    "description": "The share of the position it closes (0.5: half)."}}},
        "description": f"The {what}s, each closing a share of {of}; the shares add up to {adds_up}."}


ENTRY_TIF = {"type": "string", "enum": ["Gtc", "Ioc"],
             "description": "With a limit price only. Gtc (default) rests with one full-size stop and at most "
                            "one full-size target attached. Ioc fills immediately up to the price cap and "
                            "cancels the remainder; supports multiple stops and targets for the filled size."}


def _prepare_open_position(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[float, float, str]:
    """The same rounded entry and protection validation for preview and submission. No writes."""
    symbol, is_long = arguments["symbol"], arguments["side"] == "long"
    limit, tif = arguments.get("limit_price"), arguments.get("time_in_force")
    if tif is not None and limit is None:
        raise InvalidArgumentException("time_in_force requires limit_price (entry_price in plan_trade)")
    tif = tif or ("Ioc" if limit is None else "Gtc")
    stops, profits = arguments["stop_losses"], arguments.get("take_profits", [])
    if sum((convert.decimal(level["fraction"]) for level in stops), Decimal(0)) != 1:
        raise ToolError("INVALID_ARGUMENT", "stop_losses: the fractions must add up to 1, so the whole position "
                                            "is protected", hint="for a position without a full stop, use buy or sell")
    legs = protection_legs("stop_loss", None, [(v["price"], v["fraction"]) for v in stops])
    legs += protection_legs("take_profit", None, [(v["price"], v["fraction"]) for v in profits] or None)
    slippage = arguments.get("slippage")
    if slippage is None and target.account is not None:
        slippage = float(target.account.max_slippage)
    size, price = client.prepare_order(symbol, arguments["size"], is_long, limit, slippage)
    if convert.decimal(size) * convert.decimal(price) < MIN_ORDER_USD:
        raise InvalidArgumentException(f"the entry is worth less than Hyperliquid's minimum order of {MIN_ORDER_USD} USD")
    prepare_levels(client, symbol, size, is_long, legs,
                   price if tif != "Ioc" else client._mid_price(symbol), tif != "Ioc")
    return size, price, tif


def _open_position_checks(registry: AccountRegistry, client: Any, target: Target, arguments: dict[str, Any],
                          size: float, price: float, *, preview: bool = False) -> tuple[guards.GuardReport, guards.RiskState]:
    """Read and evaluate the submission guards; preview uses unsigned, explicitly addressed reads."""
    symbol, is_long = arguments["symbol"], arguments["side"] == "long"
    limit = arguments.get("limit_price")
    state = _risk_states(registry, client, target, [(symbol, is_long)], preview=preview)[0]
    if _config(target).max_risk_pct is not None:
        state = replace(state, loss_at_stops=_before_sending_read("the loss at the stop losses", lambda: _loss_at_stops(
            client, target, symbol, is_long, convert.decimal(size), arguments["stop_losses"],
            None if limit is None else convert.decimal(price))))
    return guards.check_order(_config(target), symbol, size, price, is_buy=is_long, reduce_only=False,
                              market=limit is None, slippage_value=arguments.get("slippage"), risk=state), state


def preview_open_position(registry: AccountRegistry, client: Any, target: Target,
                          arguments: dict[str, Any]) -> dict[str, Any]:
    """Local submission eligibility from current reads, never a reservation or a signer check."""
    account = target.account
    checks: list[guards.Check] = []
    result: dict[str, Any] = {"eligible": None, "arguments": arguments, "prepared_entry": None,
                              "trading_enabled": account is not None and registry.can_trade(account.name),
                              "margin": None, "checks": [],
                              "perp_dexs": ["", *registry.account_perp_dexs(account)] if account else None,
                              "note": "Snapshot of local checks only; signer authorization and exchange acceptance "
                                      "are not checked. Submission reads and checks again; this reserves nothing."}
    try:
        if account is not None:
            require_enabled_dexes(registry.account_perp_dexs(account), [arguments["symbol"]])
        size, price, tif = _prepare_open_position(client, target, arguments)
        result["arguments"] = {**arguments, "size": size}
        for kind in ("stop_losses", "take_profits"):
            if kind in arguments:
                result["arguments"][kind] = [
                    {"price": client._format_price(arguments["symbol"], v["price"]), "fraction": v["fraction"]}
                    for v in arguments[kind]]
        if "limit_price" in arguments:
            result["arguments"]["limit_price"] = price
        result["prepared_entry"] = {"size": size, "limit_price": price, "time_in_force": tif,
                                    "protection": "after_fill" if tif == "Ioc" else "attached"}
        checks.append(guards.Check("entry_and_protection", True, "entry and protection meet local submission requirements"))
        if account is not None and account.trading:
            halted = _before_sending_read("the shared halt", lambda: registry.preview_halted(target))
            checks.append(guards.Check("kill_switch", not halted,
                                       "account is halted" if halted else "account is not halted"))
            report, state = _open_position_checks(registry, client, target, arguments, size, price, preview=True)
            checks.extend(report.checks)
            value = convert.decimal(size) * guards.valued_price(price, arguments["side"] == "long", state.mid)
            proposed = value / state.leverage.value if state.leverage is not None else Decimal("NaN")
            used, pending = state.margin_used, state.pending_margin
            total = used + pending + proposed if used is not None and pending is not None else None
            result["margin"] = {key: value if value is not None and value.is_finite() else None
                                for key, value in {"used": used, "pending": pending, "proposed": proposed,
                                                   "total": total, "max_total_margin_pct": account.max_total_margin_pct}.items()}
            result["eligible"] = all(c.passed or c.mode == "warn" for c in checks)
        else:
            result["note"] += " No configured trading policy applies to this watch-only target."
    except HyperliquidException as error:
        blocked = any(not c.passed and c.mode == "block" for c in checks)
        checks.append(guards.Check(error.code or "preparation", False, str(error)))
        result["error"] = error_body(error)["error"]
        # An unavailable read leaves eligibility unestablished; a known rejection is false.
        result["eligible"] = False if blocked or error.code in {"INVALID_ARGUMENT", "UNKNOWN_SYMBOL"} else None
    result["checks"] = [c.as_dict() for c in checks]
    result["observed_at"] = convert.iso(int(time.time() * 1000))
    return result


def _open_position(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, is_long = arguments["symbol"], arguments["side"] == "long"
        stops, profits = arguments["stop_losses"], arguments.get("take_profits", [])
        size, price, tif = _prepare_open_position(client, target, arguments)
        report, _ = _open_position_checks(registry, client, target, arguments, size, price)
        report.enforce()
        opening = client.open_long_position if is_long else client.open_short_position
        orders = _remembering(registry, target, lambda: _counted(registry, target, True, lambda: opening(
            symbol, size, limit_price=price, time_in_force=tif,
            stop_losses=[(level["price"], level["fraction"]) for level in stops],
            take_profits=[(level["price"], level["fraction"]) for level in profits] or None)))
        entry = orders["entry"]
        if tif != "Ioc":  # resting: Hyperliquid places its stop loss and take profit once it fills
            from . import risk as risk_tools  # the prices as sent
            attached = [{"kind": kind, "trigger_price": price, "size": entry.size}
                        for kind, levels in (("stop_loss", stops), ("take_profit", profits))
                        for price, _ in risk_tools.sent_levels(client, symbol, is_long, levels)]
            return ({"entry": convert.placed_order(entry), "attached": attached},
                    f"entry {entry.order_id} {entry.status}, {len(attached)} attached")
        requested = [f"stop_loss_{i}" for i in range(1, len(stops) + 1)]
        requested += [f"take_profit_{i}" for i in range(1, len(profits) + 1)]
        data = {"entry": convert.placed_order(entry),
                "stop_losses": [convert.placed_order(o) for name, o in orders.items() if name.startswith("stop_loss")],
                "take_profits": [convert.placed_order(o) for name, o in orders.items()
                                 if name.startswith("take_profit")],
                # A level whose share rounds to 0 for a small fill: its part went to the nearest of its kind.
                "warnings": [f"{name} is not placed: its share rounds to 0 for the filled size {entry.filled_size}"
                             + ("; the nearest placed of its kind covers it" if any(
                                 placed.startswith(name.rsplit("_", 1)[0]) for placed in orders) else
                                "; no order of its kind is placed")
                             for name in requested if name not in orders]}
        return data, (f"entry {entry.order_id} {entry.status}, {len(data['stop_losses'])} stop losses, "
                      f"{len(data['take_profits'])} take profits")

    return _trade_tool(
        registry, "open_position",
        "Open a perp position with its stop losses (required: together the whole position) and take profits, in "
        "one call. At market: the entry is immediate or cancel at the mid price plus or minus slippage (default "
        "and maximum: the account's max_slippage), so it can fill partly; once it fills, each stop loss and take "
        "profit is placed for its share of the filled size. With limit_price and time_in_force=Ioc the same "
        "multi-level workflow fills immediately up to that price cap and cancels any remainder; stops and "
        "targets are checked against the current mid. With limit_price and time_in_force=Gtc (default) it "
        "rests, or fills at once if the price is already there, with one stop loss and at most one take profit "
        "(each fraction 1) attached: Hyperliquid places them once the entry has filled completely (or its rest "
        "is canceled for lack of margin); until then get_open_orders shows them under the entry's children, and "
        "a partly filled entry's filled part has none; canceling the entry cancels them. Plan the size with "
        "plan_trade first. Checked against "
        "the account's limits first (GUARD_BLOCKED; a limit in warn mode that breaks or cannot be checked does "
        "not itself block the call, and if it succeeds is in warnings: [{rule, detail}] besides data); prices and the 10 USD minimum of partial levels before the entry (INVALID_ARGUMENT: nothing "
        "sent). If the entry filled but a level could not be placed: "
        "PROTECTION_NOT_PLACED, the position is open without the levels in details.missing (price and size of "
        "each): place them with stop_loss / take_profit, or close. Returns the entry, the stop losses and the "
        "take profits, and data.warnings (a list of texts, when there are any) for a level not placed because its "
        "share rounds to 0 for a small fill. "
        "With a Gtc limit it returns the entry and attached (kind, trigger_price, size). On OUTCOME_UNKNOWN never "
        "call again: check get_positions; an immediate entry may have filled without protection, while a Gtc entry may be "
        "placed with its stop loss and take profit attached (check get_open_orders and its children before "
        "placing anything).",
        {"symbol": SYMBOL, "side": {"type": "string", "enum": ["long", "short"]}, "size": SIZE,
         "limit_price": {**PRICE, "description": "The entry's price cap. Gtc (default) rests with a single bracket; "
                                                 "Ioc fills immediately, cancels the remainder and supports multiple levels."},
         "time_in_force": ENTRY_TIF,
         "stop_losses": _levels("stop loss", "1"), "take_profits": _levels("take profit", "at most 1"),
         "slippage": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
                      "description": "The entry's worst price as a fraction from the mid (0.01 = 1 %), at most the "
                                     "account's max_slippage."}},
        ["symbol", "side", "size", "stop_losses"], act, reduces_risk=lambda arguments: False)


# ── place_trigger_entry ───────────────────────────────────────────────


def _place_trigger_entry(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, is_long = arguments["symbol"], arguments["side"] == "long"
        trigger, limit = arguments["trigger_price"], arguments.get("limit_price")
        # Sized at the price it enters at (a stop limit's limit, else the trigger) and valued at
        # the most it can enter at, as the library signs it: a stop market's limit is the 10 %
        # beyond the rounded trigger, rounded again (_market_trigger_limit).
        size = _size(client, symbol, is_long, arguments, trigger if limit is None else limit)
        size, trigger_px = client.prepare_order(symbol, size, is_long, trigger)
        tolerance = _answers.TPSL_MARKET_SLIPPAGE_PCT / 100
        worst = client.prepare_order(symbol, size, is_long, trigger_px * (1 + tolerance if is_long else 1 - tolerance)
                                     if limit is None else limit)[1]
        # No stop loss limits its loss: under max_risk_pct the guard refuses it.
        risk = _risk_state(registry, client, target, symbol, is_long)
        guards.check_order(_config(target), symbol, size, worst, is_buy=is_long, reduce_only=False, market=False,
                           risk=risk).enforce()
        order = _placing(registry, target, arguments.get("cloid"), lambda cloid: _counted(
            registry, target, True, lambda: client.trigger_order(symbol, is_long, size, trigger, limit_price=limit,
                                                                 cloid=cloid)))
        return _placed(order)

    return _trade_tool(
        registry, "place_trigger_entry",
        "Place a stop order that opens or adds to a perp position once the price gets there: long buys when the "
        "mark price rises to trigger_price (it must be above the current price), short sells when it falls to it "
        "(below). It fires on a touch, not on a candle close. Without limit_price it executes as a market order "
        "within Hyperliquid's 10 % tolerance for market triggers, so in a gap it can fill well past the trigger "
        "(the account's max_slippage does not apply); with limit_price, as a limit order at that price, which in "
        "a fast move may fill partly or not at all. No protection is attached: Hyperliquid attaches no stop loss "
        "or take profit to a stop order. Once it fills, place them with the stop_loss and take_profit tools; "
        "until then the position has none (get_changes shows the fill and the position's unprotected_size). "
        "get_open_orders shows it as type trigger_entry. Refused when the account has max_risk_pct (no stop limits its loss). The "
        "account's limits are checked now, as for an order that adds risk, valued at the most it can enter at "
        "(GUARD_BLOCKED); not again when it fires. kill_switch cancels it regardless of the position's side. "
        "Returns the order: open while it waits (and once triggered while a stop limit is "
        "partly filled: see filled_size). On OUTCOME_UNKNOWN never call again: look it up with "
        "get_order_status_by_cloid.",
        {"symbol": SYMBOL, "side": {"type": "string", "enum": ["long", "short"]}, "size": SIZE,
         "notional_usd": {**NOTIONAL_USD, "description": "Instead of size: the entry's value in USD (the market's "
                          "collateral), at trigger_price (with limit_price: at that price) as Hyperliquid rounds it, "
                          "rounded down to the lot size. Hyperliquid's minimum is 10 USD after rounding."},
         "trigger_price": PRICE,
         "limit_price": {**PRICE, "description": "Makes it a stop limit: the worst price once triggered "
                                                 "(long: at or above trigger_price, short: at or below)."},
         "cloid": CLOID},
        ["symbol", "side", "trigger_price"], act, reduces_risk=lambda arguments: False, check=_one_size)


# ── modify_order ──────────────────────────────────────────────────────


def _modify_order(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, is_buy, reduce_only = arguments["symbol"], arguments["is_buy"], arguments.get("reduce_only", False)
        order_id = arguments["order_id"]
        # Hyperliquid does not modify a trigger order into a limit one ("Attempted to modify to
        # invalid new order"): refused before anything is sent.
        current = next((o for o in client.get_open_orders(symbol) if o.order_id == order_id), None)
        if current is not None and current.trigger_price is not None:
            raise ToolError("INVALID_ARGUMENT", f"order {order_id} is a trigger order: trigger orders can't be modified",
                            hint="move a stop loss or take profit with update_stop_loss / update_take_profit; "
                                 "cancel a trigger entry and place it anew")
        size, price = client.prepare_order(symbol, arguments["size"], is_buy, arguments["price"])
        # The full new order is checked, like a new one.
        # The order modified is replaced: its own share of the open orders is not counted twice.
        risk = None if reduce_only else _risk_state(registry, client, target, symbol, is_buy,
                                                    replacing=arguments["order_id"])
        guards.check_order(_config(target), symbol, size, price, is_buy=is_buy, reduce_only=reduce_only,
                           market=False, risk=risk).enforce()
        order = _placing(registry, target, arguments.get("cloid"), lambda cloid: _counted(
            registry, target, not reduce_only, lambda: client.modify_order(
                order_id, symbol, is_buy, size, price, arguments["order_type"], reduce_only, cloid=cloid)))
        return _placed(order)

    return _trade_tool(
        registry, "modify_order",
        "Change an open limit order: its side, size, price, time in force and reduce_only, all given anew. "
        "Trigger orders can't be modified (INVALID_ARGUMENT, nothing sent): move a stop loss or take profit "
        "with update_stop_loss / update_take_profit. The modified order gets the cloid given, or a new one "
        "(in the answer). Checked against the account's limits like a new order (a limit in warn mode that "
        "breaks or cannot be checked does not itself block the call, and if it succeeds is in warnings: "
        "[{rule, detail}] besides data). Returns the order after the "
        "modify: open, or filled if the new price matched at once. Use its order_id from then on: Hyperliquid "
        "can give the modified order a new one.",
        {"order_id": ORDER_ID, "symbol": SYMBOL,
         "is_buy": {"type": "boolean", "description": "true: a buy order; false: a sell order."},
         "size": SIZE, "price": PRICE,
         "order_type": {"type": "object", "required": ["limit"], "additionalProperties": False,
                        "description": 'A limit order: {"limit": {"tif": "Gtc"}}, or "Alo" to rest as maker only.',
                        "properties": {"limit": {"type": "object", "required": ["tif"], "additionalProperties": False,
                                                 "properties": {"tif": {"type": "string", "enum": ["Gtc", "Alo"]}}}}},
         "reduce_only": REDUCE_ONLY, "cloid": CLOID},
        ["order_id", "symbol", "is_buy", "size", "price", "order_type"], act,
        reduces_risk=lambda arguments: arguments.get("reduce_only", False))


# ── cancel_order, cancel_order_by_cloid ───────────────────────────────


def _cancel_order(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        guards.check_symbol(arguments["symbol"]).enforce()
        return _canceled(client.cancel_order(arguments["order_id"], arguments["symbol"]))

    return _trade_tool(
        registry, "cancel_order",
        "Cancel one open order (stop losses and take profits included) by its order id. Returns canceled: "
        "false if it was not open any more (filled, canceled, or never placed).",
        {"order_id": ORDER_ID, "symbol": SYMBOL}, ["order_id", "symbol"], act,
        reduces_risk=lambda arguments: True, idempotent=True)


def _cancel_order_by_cloid(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        guards.check_symbol(arguments["symbol"]).enforce()
        return _canceled(client.cancel_order_by_cloid(arguments["cloid"], arguments["symbol"]))

    return _trade_tool(
        registry, "cancel_order_by_cloid",
        "Cancel one open order by the client order id it was placed with. Returns canceled: false if it was "
        "not open (filled, canceled, or never placed).",
        {"cloid": {**CLOID, "description": "The order's client order id (cloid)."}, "symbol": SYMBOL},
        ["cloid", "symbol"], act, reduces_risk=lambda arguments: True, idempotent=True)


FILTERS = ("is_buy", "min_price", "max_price")


def _cancel_arguments(arguments: dict[str, Any]) -> None:
    """cancel_orders: orders, or symbol with its filters; min_price at most max_price."""
    if ("orders" in arguments) == ("symbol" in arguments):
        raise ToolError("INVALID_ARGUMENT", "pass orders, or symbol (with is_buy, min_price, max_price), exactly one")
    if "orders" in arguments and any(key in arguments for key in FILTERS):
        raise ToolError("INVALID_ARGUMENT", "is_buy, min_price and max_price select orders of symbol, not of orders")
    low, high = arguments.get("min_price"), arguments.get("max_price")
    if low is not None and high is not None and convert.decimal(low) > convert.decimal(high):
        raise ToolError("INVALID_ARGUMENT", f"min_price {low} is above max_price {high}")


def _cancel_orders(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        skipped: list[int] = []
        if "orders" in arguments:
            cancels = [{"symbol": order["symbol"], "order_id": order["order_id"]} for order in arguments["orders"]]
            for symbol in dict.fromkeys(cancel["symbol"] for cancel in cancels):
                guards.check_symbol(symbol).enforce()
        else:
            symbol = arguments["symbol"]
            guards.check_symbol(symbol).enforce()
            if symbol not in client.market_specs:  # else an unknown symbol would match nothing, as if canceled
                raise UnknownSymbolException(f"Unknown symbol {symbol!r}")
            chosen = [order for order in client.get_open_orders(symbol) if _chosen(order, symbol, arguments)]
            # Cancelling a limit order cancels the stop loss and take profit attached to it (TP/SL docs).
            skipped = [order.order_id for order in chosen if order.children]
            cancels = [{"symbol": order.symbol, "order_id": order.order_id} for order in chosen if not order.children]
        if not cancels:
            return {"canceled": [], "not_open": [], "skipped": skipped}, "nothing to cancel"
        statuses = client.bulk_cancel(cancels)["response"]["data"]["statuses"]  # every one done, or it raised
        canceled = [cancel["order_id"] for cancel, status in zip(cancels, statuses) if status == "success"]
        not_open = [cancel["order_id"] for cancel, status in zip(cancels, statuses) if status != "success"]
        return ({"canceled": canceled, "not_open": not_open, "skipped": skipped},
                f"canceled {len(canceled)}, not open {len(not_open)}, skipped {len(skipped)}")

    return _trade_tool(
        registry, "cancel_orders",
        f"Cancel several open orders in one request: up to {MAX_BATCH} by order id and symbol (stop losses and take "
        "profits included), or the open limit orders of one symbol, optionally of one side (is_buy) and within a "
        "limit price range (min_price, max_price, both included). The symbol form never cancels stop losses or "
        "take profits, and skips a limit order with a stop loss or take profit attached (cancelling it would "
        "cancel them too; cancel it by order id). Returns canceled, not_open (filled, canceled or never placed) "
        "and skipped, as order ids. Orders the exchange did not cancel: ORDERS_NOT_CANCELED, with details.failed "
        "(the others were canceled). Allowed while the account is halted.",
        {"orders": {"type": "array", "minItems": 1, "maxItems": MAX_BATCH,
                    "items": {"type": "object", "additionalProperties": False, "required": ["order_id", "symbol"],
                              "properties": {"order_id": ORDER_ID, "symbol": SYMBOL}},
                    "description": "The orders to cancel, by order id and symbol."},
         "symbol": {**SYMBOL, "description": "Instead of orders: cancel this market's open limit orders, as is_buy, "
                                             "min_price and max_price select them."},
         "is_buy": {"type": "boolean", "description": "With symbol: only buy orders (true) or sell orders (false)."},
         "min_price": {**PRICE, "description": "With symbol: only orders with a limit price at or above this."},
         "max_price": {**PRICE, "description": "With symbol: only orders with a limit price at or below this."}},
        [], act, reduces_risk=lambda arguments: True, idempotent=True, check=_cancel_arguments,
        symbols=lambda arguments: ([order["symbol"] for order in arguments["orders"]] if "orders" in arguments
                                   else [arguments["symbol"]]))


def _chosen(order: Order, symbol: str, arguments: dict[str, Any]) -> bool:
    """An open plain limit order of `symbol` that cancel_orders' filters select."""
    if order.symbol != symbol or order.type != "limit" or order.limit_price is None:
        return False
    if "is_buy" in arguments and order.is_buy != arguments["is_buy"]:
        return False
    low, high = arguments.get("min_price"), arguments.get("max_price")
    return (low is None or order.limit_price >= convert.decimal(low)) and (
        high is None or order.limit_price <= convert.decimal(high))


# ── close, close_all_positions ────────────────────────────────────────

ALWAYS = ("Allowed while the account is halted, and never blocked by the allowlist, max_order_usd or the risk "
          "guardrails.")


def _close(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, slippage = arguments["symbol"], arguments.get("slippage")
        guards.check_close(_config(target), symbol, slippage).enforce()
        return _placed(_remembering(registry, target, lambda: client.close(symbol, slippage=slippage)))

    return _trade_tool(
        registry, "close",
        "Close the whole position in a perp: a reduce-only market order, immediate or cancel at the mid price "
        "plus or minus slippage (default and maximum: the account's max_slippage). Returns the closing order. "
        "Filled only partly (not enough liquidity within the slippage) or rejected: POSITION_NOT_CLOSED, with "
        "the size still open (details.remaining) and the order; close again, with more slippage if allowed, or "
        "keep the rest. To reduce a position only partly: buy or sell with reduce_only. No position: "
        "POSITION_NOT_FOUND. " + ALWAYS,
        {"symbol": SYMBOL,
         "slippage": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
                      "description": "The worst price as a fraction from the mid (0.01 = 1 %), at most the "
                                     "account's max_slippage."}},
        ["symbol"], act, reduces_risk=lambda arguments: True)


def _close_all_positions(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        closed = _remembering(registry, target, client.close_all_positions)
        return ({"closed": {symbol: convert.placed_order(order) for symbol, order in closed.items()}},
                f"closed {', '.join(closed) or 'nothing'}")

    return _trade_tool(
        registry, "close_all_positions",
        "Close every open perp position of the account, each as close does, at the account's max_slippage. "
        "Covers Hyperliquid's own perps and the account's enabled HIP-3 dexes only: positions on other dexes "
        "are not seen. Every position is tried, also after one fails. Returns closed: the closing order by "
        "symbol (empty without positions). Not every position closed: CLOSE_POSITIONS_INCOMPLETE, with closed "
        "and failed (the error per symbol) in details. " + ALWAYS,
        {}, [], act, reduces_risk=lambda arguments: True)


# ── stop_loss, take_profit, update_stop_loss, update_take_profit ─────


def _protection_tool(registry: AccountRegistry, name: str) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol = arguments["symbol"]
        guards.check_symbol(symbol).enforce()
        place = client.stop_loss if name == "stop_loss" else client.take_profit
        return _placed(_placing(registry, target, arguments.get("cloid"), lambda cloid: place(
            symbol, arguments["size"], arguments["trigger_price"], cloid=cloid)))

    what, when = ("stop loss", "falls to (long) or rises to (short)") if name == "stop_loss" else (
        "take profit", "rises to (long) or falls to (short)")
    return _trade_tool(
        registry, name,
        f"Place a {what} for an open position: a reduce-only market order that triggers when the mark price "
        f"{when} trigger_price. The side comes from the position (sell for a long, buy for a short). Size in "
        "base units: the position's size protects all of it. When triggered it executes as a market order, with "
        "Hyperliquid's 10 % slippage tolerance for TP/SL. Returns the order: open, or filled if the price was "
        f"already past the trigger. To move it later: update_{name}. No position: POSITION_NOT_FOUND. " + ALWAYS,
        {"symbol": SYMBOL, "size": SIZE,
         "trigger_price": {**PRICE, "description": "The price that triggers it. " + PRICE_TEXT},
         "cloid": CLOID},
        ["symbol", "size", "trigger_price"], act, reduces_risk=lambda arguments: True)


def _update_protection(registry: AccountRegistry, name: str) -> ToolSpec:
    kind = "stop loss" if name == "update_stop_loss" else "take profit"

    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol = arguments["symbol"]
        guards.check_symbol(symbol).enforce()
        before = client.get_open_orders(symbol)  # to say how many the new one replaces
        method = getattr(client, name)
        order = _remembering(registry, target, lambda: method(symbol, arguments["new_price"]))
        data, outcome = _placed(order)
        # The old ones it replaced: of its kind, on the side that closes the position, as the library picks them.
        replaced = [o for o in before if o.type == name.removeprefix("update_") and o.is_buy == order.is_buy]
        if len(replaced) > 1:
            data["warnings"] = [f"{len(replaced)} {kind}s were open before this call: {name} replaces them with this "
                                f"one for the whole position; to keep several levels, use sync_protection"]
        return data, outcome

    return _trade_tool(
        registry, name,
        f"Move the {kind} of an open position to new_price: places a new {kind} for the whole live position "
        f"first, then cancels the old ones, so the position is never left without one. Without a {kind} yet, "
        f"one is created. Several old ones are replaced by this one (data.warnings says so): to move one level "
        f"of several, use sync_protection. Returns the new order. Not as asked (the new order does not cover the whole position, "
        "or an old one could not be canceled): PROTECTION_REPLACEMENT_INCOMPLETE, with new_order, "
        "old_order_ids (may still be open) and position in details. No position: POSITION_NOT_FOUND. " + ALWAYS,
        {"symbol": SYMBOL, "new_price": {**PRICE, "description": f"The new trigger price of the {kind}. " + PRICE_TEXT}},
        ["symbol", "new_price"], act, reduces_risk=lambda arguments: True)


def _sync_protection(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol = arguments["symbol"]
        guards.check_symbol(symbol).enforce()

        def pairs(name: str) -> Optional[list[tuple[float, float]]]:
            levels = arguments.get(name)
            return None if levels is None else [(level["price"], level["fraction"]) for level in levels]
        result = _remembering(registry, target, lambda: client.resize_protection(
            symbol, pairs("stop_losses"), pairs("take_profits"),
            take_profit_type=arguments.get("take_profit_type", "trigger")))
        data: dict[str, Any] = {
            "symbol": result.symbol, "position_size": result.position_size,
            "placed": [convert.placed_order(o) for o in result.placed], "kept": [convert.order(o) for o in result.kept],
            "canceled": result.canceled, "untouched": [convert.order(o) for o in result.untouched]}
        # From the sizes as placed or kept (rounded to the lot), not the fractions asked.
        covered = sum((o.remaining_size for o in [*result.placed, *result.kept] if o.type == "stop_loss"), Decimal(0))
        size = abs(result.position_size)
        if "stop_losses" in arguments and size and covered < size:
            note = "; untouched orders not counted" if any(o.type == "stop_loss" for o in result.untouched) else ""
            data["warnings"] = [f"the stop losses of this call cover {_plain(covered)} of the position's "
                                f"{_plain(size)} ({_plain(round(covered * 100 / size, 1))} %){note}"]
        return data, f"placed {len(result.placed)}, kept {len(result.kept)}, canceled {len(result.canceled)}"

    return _trade_tool(
        registry, "sync_protection",
        "Resize the stop losses and/or take profits of an open position to its live size, level by level: each "
        "level is a price and the fraction of the live position it closes (a list's fractions add up to at most "
        "1). Pass stop_losses, take_profits or both, each with at least one level (an empty list does not remove "
        "a kind; cancel_orders does). A list given replaces that kind's orders (an order already open at a "
        "level's price with its size is kept, no request); a list left out leaves that kind alone. Stop losses are market triggers (as stop_loss). "
        "take_profit_type: trigger (default, as take_profit) or limit (resting reduce-only post-only limits, "
        "maker); no stop limits. Native position TP/SL set in the Hyperliquid app and orders with TP/SL "
        "attached are not changed (untouched); take-profit limits that would exceed the position with untouched "
        "resting limits are refused. Hyperliquid keeps a position's resting reduce-only limits at most as large "
        "as the position, cancelling one itself otherwise (seen live), so: new stop losses are placed first and "
        "the old ones cancelled only once every stop level is read open with its size; take profits where a "
        "limit is involved (an old one, or take_profit_type limit): the old ones that match no level are "
        "cancelled first, then the new ones placed (coverage may temporarily decrease; kept and untouched "
        "ones stay; none placed if an old one cannot be cancelled), triggers replacing triggers are "
        "placed first. Every level is checked before anything is sent "
        "(side of the mid, one lot, Hyperliquid's 10 USD minimum, no two at one price). Returns placed, kept, "
        "canceled (order ids) and untouched, and data.warnings (a list of texts) when the stop losses given, "
        "as placed or kept, cover less than the position (untouched stops not counted). Not as asked: PROTECTION_SYNC_INCOMPLETE with placed, kept, canceled, retained (old orders "
        "deliberately left open), not_canceled (old orders whose cancel failed: may still be open), missing "
        "(every level not in place, each "
        "with outcome_unknown and its cloid when it may still arrive) and position in details; nothing is "
        "undone. No position: POSITION_NOT_FOUND. " + ALWAYS,
        {"symbol": SYMBOL, "stop_losses": _levels("stop loss", "at most 1", "the live position"),
         "take_profits": _levels("take profit", "at most 1", "the live position",
                                 "The take profit's trigger price, or its limit price with take_profit_type limit."),
         "take_profit_type": {"type": "string", "enum": ["trigger", "limit"], "default": "trigger",
                              "description": "trigger (market take profit triggers) or limit (resting reduce-only "
                                             "limit orders, filled as maker)."}},
        ["symbol"], act, reduces_risk=lambda arguments: True)


# ── cancel_all_orders, set_leverage ───────────────────────────────────


def _cancel_all_orders(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol = arguments.get("symbol")
        if symbol is not None:
            guards.check_symbol(symbol).enforce()
        client.cancel_all_orders(symbol)
        return None, "canceled all"

    return _trade_tool(
        registry, "cancel_all_orders",
        "Cancel every open perp order of the account, or of one symbol: limit orders, and stop losses and take "
        "profits too, so open positions are then without protection (close them, or place new ones with "
        "stop_loss / take_profit). Covers Hyperliquid's own perps and the account's enabled HIP-3 dexes; spot "
        "orders stay open. Returns null once every order is canceled. Orders the exchange did not cancel: "
        "ORDERS_NOT_CANCELED, with details.failed (symbol, order_id, reason per order still open; the others "
        "were canceled). Allowed while the account is halted.",
        {"symbol": {**SYMBOL, "description": "Only this market's orders, exactly as Hyperliquid names it."}},
        [], act, reduces_risk=lambda arguments: True, idempotent=True)


def _set_leverage(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, leverage, is_cross = arguments["symbol"], arguments["leverage"], arguments.get("is_cross", True)
        guards.check_leverage(_config(target), symbol, leverage, is_cross).enforce()
        client.set_leverage(symbol, leverage, is_cross)
        # Hyperliquid's "ok" carries none of it: the values that were set.
        return ({"symbol": symbol, "leverage": leverage, "is_cross": is_cross},
                f"leverage {leverage} {'cross' if is_cross else 'isolated'}")

    return _trade_tool(
        registry, "set_leverage",
        "Set the leverage of a perp market for the account, with cross or isolated margin; it is used for new "
        "orders. Hyperliquid lets the leverage of an open position be raised; other changes with a position "
        "open, such as switching between cross and isolated, it can refuse (ACTION_REJECTED with its reason). "
        "At most the account's max_leverage and the market's own maximum. Isolated-only markets (list_markets: "
        "margin_mode no_cross or strict_isolated) need is_cross false. Returns the values set. Refused while the "
        "account is halted.",
        {"symbol": SYMBOL,
         "leverage": {"type": "integer", "minimum": 1, "description": "The leverage, a whole number (5 = 5x)."},
         "is_cross": {"type": "boolean", "default": True, "description": "true: cross margin; false: isolated."}},
        ["symbol", "leverage"], act, reduces_risk=lambda arguments: False, idempotent=True, destructive=False)


# ── kill_switch ───────────────────────────────────────────────────────


def _kill_switch(registry: AccountRegistry) -> ToolSpec:
    def targets(arguments: dict[str, Any]) -> list[Target]:
        every = arguments.get("all_accounts", False)
        if every and "account" in arguments:
            raise ToolError("INVALID_ARGUMENT", "pass account or all_accounts, not both")
        if every:
            return registry.trading_targets()
        try:
            return [registry.resolve_trade(arguments.get("account"))]
        except ToolError as error:
            if error.code != "AMBIGUOUS_ACCOUNT":
                raise
            raise ToolError("AMBIGUOUS_ACCOUNT", str(error), hint=f"{error.hint}, or all_accounts: true") from None

    def halt(arguments: dict[str, Any]) -> None:
        """The halt, at once: run by the server in its event loop when the call arrives, before
        it waits for a thread. Only resolution and the halt: no request, no logging (a blocked
        stderr would stall every later call)."""
        _halt_every(registry, targets(arguments))

    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        halted = targets(arguments)
        local_only = _halt_every(registry, halted)  # already halted by halt(); again for a direct call
        logger.warning("kill_switch: halted %s", ", ".join(str(target.name) for target in halted))
        accounts = []
        for target in halted:
            entries = _cancel_entries(registry, target)
            accounts.append({**_live(registry, target), "entries": entries})
        result = {"halted": [target.name for target in halted], "accounts": accounts}
        if local_only:
            raise ToolError("INTERNAL", f"halted in this server only: {', '.join(local_only)} (the shared halt "
                                        "cannot be written); every other account asked for is halted for every "
                                        "server", hint="other chats on those accounts are not halted: quit them, "
                                        "or the assistant", details=result)
        return result

    return ToolSpec(
        name="kill_switch",
        description=(
            "Emergency stop: halts trading at once for the account (or every account with all_accounts), before "
            "anything else; a call already running finishes. From then on only risk-reducing calls run: close, "
            "close_all_positions, stop_loss, take_profit, update_stop_loss, update_take_profit, sync_protection, "
            "orders with reduce_only, cancels. Then it cancels every open perp order without reduce_only, "
            "including orders opposing a position: these can reverse it. Reduce-only orders stay (the stop loss "
            "and take profit attached to a canceled entry go with it); it closes "
            "nothing. To flatten, call cancel_all_orders and close_all_positions next; they act on the account's "
            "dexes (dexes_traded), so a position reported on another dex needs enable_perp_dexs first. Returns, "
            "per account, entries (canceled, not_open: filled or gone before; not_canceled with the reason; error "
            "when the orders could not be read or the cancel's outcome is unknown, then nothing is known to be "
            "canceled) and the perp positions (symbol, dex, side, size, unprotected_size: the size without a "
            "stop) and open perp orders (count, of which stop losses and take profits) on the dexes read "
            "(dexes_read; spot not included), read after that; a failed read is reported in their place. The halt applies to every server of this config (each chat of an assistant "
            "may run its own) and lasts until none of those that can trade runs (a read-only server does not keep "
            "it); it does not stop bots, or servers whose config file is in another folder."
        ),
        input_schema={"type": "object", "properties": {
            "account": ACCOUNT,
            "all_accounts": {"type": "boolean", "default": False, "description": "Halt every account that can trade."},
        }, "required": [], "additionalProperties": False},
        run=run, read_only=False, destructive=True, idempotent=True, own_capacity=True, first=halt,
    )


def _halt_every(registry: AccountRegistry, targets: list[Target]) -> list[str]:
    """Halt every target, also after one whose shared halt failed (each is halted in this
    server first). Returns the accounts the other servers may not see halted: an error then
    comes after the entries are canceled, not instead of it."""
    failed = []
    for target in targets:
        try:
            registry.halt(target)
        except ToolError:
            failed.append(str(target.name))
    return failed


def _cancel_entries(registry: AccountRegistry, target: Target) -> dict[str, Any]:
    """After the halt, cancel every non-reduce-only perp order on the dexes `target` trades.
    An opposing order can reverse a position, so no position read is needed to select orders.
    Hold the account's lock so a trade call still running finishes first and its order is seen.
    """
    result: dict[str, Any] = {"canceled": [], "not_open": [], "not_canceled": [], "error": None}
    try:
        with registry.lock(target):
            registry.check_signer(target, reduces_risk=True)
            client = registry.trade_client(target)
            entries = [o for o in client.get_open_orders() if o.market in PERP_MARKETS and not o.reduce_only]
            known = [o for o in entries if o.symbol in client.market_specs]
            result["not_canceled"] = [{"symbol": o.symbol, "order_id": o.order_id, "reason": "market unknown to "
                                       "the client: call refresh_market_specs()"} for o in entries if o not in known]
            if known:
                try:
                    answer = client.bulk_cancel([{"symbol": o.symbol, "order_id": o.order_id} for o in known])
                except OrdersNotCanceledException as error:
                    result.update(canceled=error.canceled, not_open=error.not_open,
                                  not_canceled=error.failed + result["not_canceled"])
                else:
                    statuses = answer["response"]["data"]["statuses"]
                    result["canceled"] = [o.order_id for o, status in zip(known, statuses) if status == "success"]
                    result["not_open"] = [o.order_id for o, status in zip(known, statuses) if status != "success"]
    except Exception as error:
        result["error"] = error_body(error)["error"]
    return result


def _live(registry: AccountRegistry, target: Target) -> dict[str, Any]:
    """What is still live on a halted account; a failed read in place of the figures."""
    live: dict[str, Any] = {
        "account": target.name, "address": target.address, "base_url": target.base_url,
        "halted": registry.is_halted(target), "dexes_read": None,
        "dexes_traded": ["default", *registry.account_perp_dexs(_config(target))],
    }
    try:
        client = registry.read_client(target)
        # The dexes of the client that reads, which enable_perp_dexs may have replaced meanwhile.
        live["dexes_read"] = [dex or "default" for dex in client.perp_dexs]
        positions = [p for p in client.get_positions(address=target.address) if p.size != 0]
        orders = [o for o in client.get_open_orders(address=target.address) if o.market in PERP_MARKETS]
    except Exception as error:
        return {**live, "positions": None, "open_orders": None, "error": error_body(error)["error"]}
    closing = {p.symbol: p.is_short for p in positions}  # the side (is_buy) that closes each position

    def protecting(kind: str) -> int:
        return sum(1 for o in orders if o.type == kind and o.symbol in closing and o.is_buy == closing[o.symbol])

    from .changes import _coverage  # the size of each position without a stop, as get_changes reports it
    return {**live,
            "positions": [{**{key: convert.position(p)[key] for key in ("symbol", "dex", "side", "size")},
                           "unprotected_size": _coverage(p, orders)["unprotected_size"]} for p in positions],
            "open_orders": {"count": len(orders), "stop_losses": protecting("stop_loss"),
                            "take_profits": protecting("take_profit")},
            "error": None}


def _canceled(canceled: bool) -> tuple[dict[str, Any], str]:
    return {"canceled": canceled}, "canceled" if canceled else "not open"


def _risk_state(registry: AccountRegistry, client: Any, target: Target, symbol: str,
                is_buy: bool, replacing: Optional[int] = None) -> guards.RiskState:
    """What the account's guardrails need before an order that adds risk, read with its own
    client: the market's leverage, for a sell the mid, and the rest only as far as a
    configured guardrail needs it (guards.needs). A failed read is GUARD_BLOCKED, retryable
    as its cause: the order is not sent."""
    return _risk_states(registry, client, target, [(symbol, is_buy)], replacing)[0]


def _risk_states(registry: AccountRegistry, client: Any, target: Target,
                 orders: list[tuple[str, bool]], replacing: Optional[int] = None, *,
                 preview: bool = False) -> list[guards.RiskState]:
    """_risk_state for each (symbol, is_buy) of a batch: the account and the portfolio read
    once, the leverage once per proposed or pending market that needs it. A read only
    rules in warn mode need (guards.soft) that fails makes them "cannot be checked" instead.
    `replacing`: an order a modify replaces, left out of the open orders."""
    account = _config(target)
    wanted, soft = guards.needs(account), guards.soft(account)
    if preview:
        wanted |= {"orders", "account", "leverage"}  # also report used/pending/proposed margin

    def account_read(method: str, *args: Any) -> Any:
        return getattr(client, method)(*args, **({"address": target.address} if preview else {}))
    markets = list(dict.fromkeys(symbol for symbol, _ in orders))
    one = len(markets) == 1
    unread: dict[str, str] = {}

    def attempt(what: str, read: str, call: Callable[[], Any]) -> Any:
        """The answer of one read; None after a failure only warn rules need it for."""
        try:
            return call()
        except Exception as error:  # nothing is sent yet, whatever failed
            if read not in soft:
                raise _read_failed(what, error) from error
            cause = error.code if isinstance(error, HyperliquidException) else type(error).__name__
            unread.update((rule, f"{what} could not be read: {cause}") for rule in soft[read])
            return None

    count = (_before_sending_read("the shared order count", lambda: registry.peek_risk_orders_last_hour(target))
             if account.max_orders_per_hour is not None else 0) \
        if preview else _before_sending(lambda: registry.risk_orders_last_hour(target))
    shared: dict[str, Any] = {"orders_last_hour": count}
    # Read open orders before the positions (in the summary): a fill between the two
    # can count twice, but cannot disappear from both reads.
    open_orders = attempt("the open orders", "orders", partial(account_read, "get_open_orders")) if "orders" in wanted else None
    pending = [o for o in open_orders if o.order_id != replacing and not o.reduce_only and o.market in PERP_MARKETS] \
        if open_orders is not None else []
    leverage_markets = list(dict.fromkeys([*markets, *(o.symbol for o in pending)])) \
        if account.max_total_margin_pct is not None or preview else markets
    leverages = {symbol: attempt("the market's leverage" if len(leverage_markets) == 1 else f"the leverage of {symbol}", "leverage",
                                 partial(account_read, "get_leverage", symbol))
                 for symbol in leverage_markets} if "leverage" in wanted else {}
    mids = {symbol: convert.decimal(attempt("the market's mid price" if one else f"the mid price of {symbol}", "mid",
                                            partial(client.get_price, symbol)))
            for symbol in dict.fromkeys(symbol for symbol, is_buy in orders if not is_buy)}
    summary = attempt("the account's value and margin", "account", partial(account_read, "get_account_summary")) \
        if "account" in wanted else None
    if summary is not None:
        shared.update(_margins(summary))
    if "day" in wanted or "today" in wanted:
        portfolio = attempt("the account's portfolio", "portfolio", partial(account_read, "get_portfolio"))
        if portfolio is not None and "day" in wanted:
            shared.update(zip(("day_loss", "day_capital"), guards.portfolio_day_loss(portfolio)))
        if portfolio is not None and "today" in wanted:
            shared.update(guards.today(portfolio, account.day_start, account.tz))
    if open_orders is not None and summary is not None:
        # A pending sell is valued at the mid when that is higher, as a new one is.
        selling = any(not o.is_buy for o in pending)
        prices = attempt("the mid prices", "orders", client.get_price) if selling else {}
        if prices is not None:
            pending_mids = {symbol: convert.decimal(price) for symbol, price in prices.items()}
            if account.max_total_margin_pct is not None or preview:
                shared["pending_margin"] = guards.pending_margin(pending, leverages, pending_mids)
            if account.max_total_notional_x is not None and account.max_total_notional_mode != "off":
                shared["open_notional"] = guards.open_notional(summary, pending, pending_mids)
    shared["unread"] = tuple(unread.items())
    return [guards.RiskState(**shared, leverage=leverages.get(symbol), mid=None if is_buy else mids[symbol])
            for symbol, is_buy in orders]


def _read_failed(what: str, error: Exception) -> ToolError:
    """GUARD_BLOCKED for a guardrail read that failed: what, why, and that the order was not
    sent. A library error keeps its code and retryable; anything else (an answer the library
    could not parse) is logged and not retryable."""
    if isinstance(error, HyperliquidException):
        body = error_body(error)["error"]
        cause, details, retryable = f"{body['code']}: {body['message']}", {"cause": body}, error.retryable
    else:
        logger.error("a risk guardrail read failed", exc_info=error)
        cause, details, retryable = f"an answer in no documented format ({type(error).__name__})", {}, False
    failure = ToolError("GUARD_BLOCKED", f"the risk guardrails could not read {what} ({cause}); the order was not "
                                         "sent", hint="call again" if retryable else None, details=details)
    failure.retryable = retryable
    return failure


def _loss_at_stops(client: Any, target: Target, symbol: str, is_long: bool, size: Decimal,
                   stops: list[dict[str, Any]], limit: Optional[Decimal] = None) -> Optional[Decimal]:
    """The loss of an open_position if every stop loss fills, as plan_trade computes it (a
    market entry, or a limit entry at its `limit`; the taker fee, as a Gtc limit can fill at
    once; fees and slippage estimated from the order book); None if not estimable."""
    from . import risk as risk_tools  # its reads: the order book and the fee rates; the prices as sent
    return risk_math.planned_loss(
        size, is_long=is_long, limit=limit, resting=False,
        stops=risk_tools.sent_levels(client, symbol, is_long, stops),
        fees=risk_tools._fees(client, symbol, target), book=risk_tools._book(client, symbol),
        size_decimals=int(client.market_specs[symbol]["size_decimals"]))


def _before_sending_read(what: str, read: Callable[[], Any]) -> Any:
    """A guardrail read before the order goes out, failing as _risk_state's do."""
    try:
        return read()
    except Exception as error:  # nothing is sent yet, whatever failed
        raise _read_failed(what, error) from error


def _margins(summary: Any) -> dict[str, Any]:
    return {"account_value": summary.account_value, "margin_used": summary.margin_used}


def _config(target: Target) -> Any:
    """The configuration of a trading account (resolve_trade returns only those)."""
    if target.account is None:
        raise ToolError("TRADING_DISABLED", f"{target.address} is not a configured account")
    return target.account
