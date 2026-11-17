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
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, Optional

from ...exceptions import (
    AuthenticationRequiredException, HyperliquidException, InvalidArgumentException, PositionNotFoundException,
)
from ...models import PERP_MARKETS, Order
from .. import convert, guards
from .. import risk as risk_math
from ..accounts import AccountRegistry, Target, require_enabled_dexes
from ..errors import ToolError, error_body, library_details
from ..server import ToolSpec

logger = logging.getLogger(__name__)

ACCOUNT = {"type": "string", "description": "A configured account that can trade (see list_accounts). "
                                            "Without it: the only account that can trade."}
SYMBOL = {"type": "string", "minLength": 1,
          "description": "The perp market, exactly as Hyperliquid names it (BTC, xyz:NVDA)."}
SIZE = {"type": "number", "exclusiveMinimum": 0,
        "description": "In base units (coins or contracts), never USD; rounded down to the market's lot size."}
PRICE_TEXT = "In USD (the market's collateral); rounded as Hyperliquid requires (5 significant figures)."
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
HALTED_HINT = ("while halted only risk-reducing calls run: close, close_all_positions, stop_loss, take_profit, "
               "update_stop_loss, update_take_profit, orders with reduce_only, cancels; the halt applies to every "
               "chat's server of this config and lasts until none of them runs (quit every chat or the app)")


def trade_tools(registry: AccountRegistry) -> list[ToolSpec]:
    """The trade tools, in the order clients list them."""
    return [
        _order_tool(registry, "buy", is_buy=True),
        _order_tool(registry, "sell", is_buy=False),
        _open_position(registry),
        _modify_order(registry),
        _cancel_order(registry),
        _cancel_order_by_cloid(registry),
        _close(registry),
        _close_all_positions(registry),
        _protection_tool(registry, "stop_loss"),
        _protection_tool(registry, "take_profit"),
        _update_protection(registry, "update_stop_loss"),
        _update_protection(registry, "update_take_profit"),
        _cancel_all_orders(registry),
        _set_leverage(registry),
        _kill_switch(registry),
    ]


Act = Callable[[Any, Target, dict[str, Any]], tuple[Any, str]]
"""A tool's own part: (client, target, arguments) -> (data, outcome for the server log)."""


def _trade_tool(registry: AccountRegistry, name: str, description: str, properties: dict[str, Any],
                required: list[str], act: Act, *, reduces_risk: Callable[[dict[str, Any]], bool],
                idempotent: bool = False, destructive: bool = True) -> ToolSpec:
    """A trade tool: the steps every one runs (module docstring), around `act`."""
    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        target = registry.resolve_trade(arguments.get("account"))
        symbol = arguments.get("symbol", "-")
        try:
            if "symbol" in arguments:  # a market of a dex the account does not trade: say how to enable it
                require_enabled_dexes(registry.account_perp_dexs(_config(target)), [symbol])
            with registry.lock(target):
                if registry.is_halted(target) and not reduces_risk(arguments):
                    raise ToolError("KILL_SWITCH_ENGAGED", f"account {target.name} is halted", hint=HALTED_HINT)
                # Looked up again before every action that adds risk; one that reduces risk
                # uses the kept result, so it never fails on the lookup.
                registry.check_signer(target, fresh=not reduces_risk(arguments))
                data, outcome = act(registry.trade_client(target), target, arguments)
        except Exception as error:  # the symbol as %r: one line, whatever the agent sent
            code = error.code if isinstance(error, HyperliquidException) else "INTERNAL"  # its traceback: once, by the server
            logger.info("%s %r (account %s): %s", name, symbol, target.name, code)
            raise
        logger.info("%s %r (account %s): %s", name, symbol, target.name, outcome)
        return {"account": target.name, "address": target.address, "base_url": target.base_url, "data": data}

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


def _placing(registry: AccountRegistry, target: Target, cloid: Any, place: Callable[[str], Any]) -> Any:
    """Place one order under a reserved cloid (the agent's, or a new one): released if the
    order was not sent; else every cloid in the outcome stays used."""
    reserved = _before_sending(lambda: registry.reserve_cloid(target, cloid))
    try:
        result = place(reserved)
    except Exception as error:
        if isinstance(error, NOT_SENT) or getattr(error, "retryable", False) or _blocked(error):
            _after_sending(lambda: registry.release_cloid(target, reserved))
        else:
            _after_sending(lambda: _remember(registry, target, error))
        raise
    _after_sending(lambda: _remember(registry, target, result))
    return result


def _counted(registry: AccountRegistry, target: Target, adds_risk: bool, send: Callable[[], Any]) -> Any:
    """Send an order; one that adds risk counts for max_orders_per_hour: counted before it is
    sent, so a crash after sending cannot leave it uncounted, and uncounted when it provably
    was not sent (NOT_SENT, or retryable)."""
    counted = _before_sending(lambda: registry.count_risk_order(target)) if adds_risk else None
    try:
        return send()
    except Exception as error:
        if counted is not None and (isinstance(error, NOT_SENT) or getattr(error, "retryable", False)):
            _after_sending(lambda: registry.release_risk_order(target, counted))
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
        _after_sending(lambda: _remember(registry, target, error))
        raise
    _after_sending(lambda: _remember(registry, target, result))
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
    return []


# ── buy, sell ─────────────────────────────────────────────────────────


def _order_tool(registry: AccountRegistry, name: str, is_buy: bool) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, limit_price = arguments["symbol"], arguments.get("limit_price")
        reduce_only, post_only = arguments.get("reduce_only", False), arguments.get("post_only", False)
        slippage, market = arguments.get("slippage"), limit_price is None
        if market and post_only:  # checked here, not by the library, so a cloid is not used up by it
            raise ToolError("INVALID_ARGUMENT", "post_only: only for a limit order", hint="pass limit_price too")
        size, price = client.prepare_order(symbol, arguments["size"], is_buy, limit_price, slippage)
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
        "fill partly; with limit_price a limit order that rests until filled or canceled. Size in base units, "
        "never USD. Checked against the account's limits first (GUARD_BLOCKED). Returns the order: status open "
        "or filled, filled size, average fill price, order id and cloid. On OUTCOME_UNKNOWN never call again: "
        "follow the hint.",
        {"symbol": SYMBOL, "size": SIZE,
         "limit_price": {**PRICE, "description": "The limit price; without it a market order. " + PRICE_TEXT},
         "reduce_only": REDUCE_ONLY,
         "post_only": {"type": "boolean", "default": False,
                       "description": "Limit orders only: rest on the book as maker, or be canceled."},
         "slippage": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
                      "description": "Market orders only: the worst price as a fraction from the mid (0.01 = 1 %), "
                                     "at most the account's max_slippage."},
         "cloid": CLOID},
        ["symbol", "size"], act, reduces_risk=lambda arguments: arguments.get("reduce_only", False))


# ── open_position ─────────────────────────────────────────────────────


def _levels(what: str, adds_up: str) -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "maxItems": 10, "items": {
        "type": "object", "additionalProperties": False, "required": ["price", "fraction"],
        "properties": {"price": {**PRICE, "description": f"The {what}'s trigger price. " + PRICE_TEXT},
                       "fraction": {"type": "number", "exclusiveMinimum": 0, "maximum": 1,
                                    "description": "The share of the position it closes (0.5: half)."}}},
        "description": f"The {what}s, each closing a share of the filled size; the shares add up to {adds_up}."}


def _open_position(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, is_long = arguments["symbol"], arguments["side"] == "long"
        stops, profits = arguments["stop_losses"], arguments.get("take_profits", [])
        if sum((convert.decimal(level["fraction"]) for level in stops), Decimal(0)) != 1:
            raise ToolError("INVALID_ARGUMENT", "stop_losses: the fractions must add up to 1, so the whole position "
                                                "is protected", hint="for a position without a full stop, use buy or sell")
        slippage = arguments.get("slippage")
        # The entry as it will be signed: immediate or cancel at the mid plus or minus slippage.
        size, price = client.prepare_order(symbol, arguments["size"], is_long, None, slippage)
        risk = _risk_state(registry, client, target, symbol, is_long)
        if _config(target).max_risk_pct is not None:  # what the trade loses if every stop fills
            risk = replace(risk, loss_at_stops=_before_sending_read("the loss at the stop losses", lambda: _loss_at_stops(
                client, target, symbol, is_long, convert.decimal(size), stops)))
        guards.check_order(_config(target), symbol, size, price, is_buy=is_long, reduce_only=False, market=True,
                           slippage_value=slippage, risk=risk).enforce()
        opening = client.open_long_position if is_long else client.open_short_position
        orders = _remembering(registry, target, lambda: _counted(registry, target, True, lambda: opening(
            symbol, size, limit_price=price, time_in_force="Ioc",
            stop_losses=[(level["price"], level["fraction"]) for level in stops],
            take_profits=[(level["price"], level["fraction"]) for level in profits] or None)))
        entry = orders["entry"]
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
        "Open a perp position at market with its stop losses (required: together the whole position) and take "
        "profits, in one call. The entry is immediate or cancel at the mid price plus or minus slippage (default "
        "and maximum: the account's max_slippage), so it can fill partly; once it fills, each stop loss and take "
        "profit is placed for its share of the filled size. Plan the size with plan_trade first. Checked against "
        "the account's limits first (GUARD_BLOCKED); prices and the 10 USD minimum of partial levels before the "
        "entry (INVALID_ARGUMENT: nothing sent). If the entry filled but a level could not be placed: "
        "PROTECTION_NOT_PLACED, the position is open without the levels in details.missing (price and size of "
        "each): place them with stop_loss / take_profit, or close. Returns the entry, the stop losses and the "
        "take profits. On OUTCOME_UNKNOWN never call again: check get_positions; a filled entry has no stop loss "
        "yet.",
        {"symbol": SYMBOL, "side": {"type": "string", "enum": ["long", "short"]}, "size": SIZE,
         "stop_losses": _levels("stop loss", "1"), "take_profits": _levels("take profit", "at most 1"),
         "slippage": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5,
                      "description": "The entry's worst price as a fraction from the mid (0.01 = 1 %), at most the "
                                     "account's max_slippage."}},
        ["symbol", "side", "size", "stop_losses"], act, reduces_risk=lambda arguments: False)


# ── modify_order ──────────────────────────────────────────────────────


def _modify_order(registry: AccountRegistry) -> ToolSpec:
    def act(client: Any, target: Target, arguments: dict[str, Any]) -> tuple[Any, str]:
        symbol, is_buy, reduce_only = arguments["symbol"], arguments["is_buy"], arguments.get("reduce_only", False)
        size, price = client.prepare_order(symbol, arguments["size"], is_buy, arguments["price"])
        # The full new order is checked, like a new one.
        risk = None if reduce_only else _risk_state(registry, client, target, symbol, is_buy)
        guards.check_order(_config(target), symbol, size, price, is_buy=is_buy, reduce_only=reduce_only,
                           market=False, risk=risk).enforce()
        order = _counted(registry, target, not reduce_only, lambda: client.modify_order(
            arguments["order_id"], symbol, is_buy, size, price, arguments["order_type"], reduce_only))
        return _placed(order)

    return _trade_tool(
        registry, "modify_order",
        "Change an open limit order: its side, size, price, time in force and reduce_only, all given anew. "
        "Hyperliquid does not modify trigger orders: move a stop loss or take profit with update_stop_loss / "
        "update_take_profit. Checked against the account's limits like a new order. Returns the order after the "
        "modify: open, or filled if the new price matched at once. Use its order_id from then on: Hyperliquid "
        "can give the modified order a new one.",
        {"order_id": ORDER_ID, "symbol": SYMBOL,
         "is_buy": {"type": "boolean", "description": "true: a buy order; false: a sell order."},
         "size": SIZE, "price": PRICE,
         "order_type": {"type": "object", "required": ["limit"], "additionalProperties": False,
                        "description": 'A limit order: {"limit": {"tif": "Gtc"}}, or "Alo" to rest as maker only.',
                        "properties": {"limit": {"type": "object", "required": ["tif"], "additionalProperties": False,
                                                 "properties": {"tif": {"type": "string", "enum": ["Gtc", "Alo"]}}}}},
         "reduce_only": REDUCE_ONLY},
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
        method = getattr(client, name)
        return _placed(_remembering(registry, target, lambda: method(symbol, arguments["new_price"])))

    return _trade_tool(
        registry, name,
        f"Move the {kind} of an open position to new_price: places a new {kind} for the whole live position "
        f"first, then cancels the old ones, so the position is never left without one. Without a {kind} yet, "
        "one is created. Returns the new order. Not as asked (the new order does not cover the whole position, "
        "or an old one could not be canceled): PROTECTION_REPLACEMENT_INCOMPLETE, with new_order, "
        "old_order_ids (may still be open) and position in details. No position: POSITION_NOT_FOUND. " + ALWAYS,
        {"symbol": SYMBOL, "new_price": {**PRICE, "description": f"The new trigger price of the {kind}. " + PRICE_TEXT}},
        ["symbol", "new_price"], act, reduces_risk=lambda arguments: True)


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
        _halt_every(registry, halted)  # already halted by halt(); again for a direct call
        logger.warning("kill_switch: halted %s", ", ".join(str(target.name) for target in halted))
        return {"halted": [target.name for target in halted], "accounts": [_live(registry, t) for t in halted]}

    return ToolSpec(
        name="kill_switch",
        description=(
            "Emergency stop: halts trading at once for the account (or every account with all_accounts), before "
            "anything else; a call already running finishes. From then on only risk-reducing calls run: close, "
            "close_all_positions, stop_loss, take_profit, update_stop_loss, update_take_profit, orders with "
            "reduce_only, cancels. It cancels and closes nothing, so stop losses stay in place: to flatten, call "
            "cancel_all_orders and close_all_positions next; they act on the account's dexes (dexes_traded), so a "
            "position reported on another dex needs enable_perp_dexs first. Returns, per account, the perp "
            "positions (symbol, dex, side, size) and open perp orders (count, of which stop losses and take "
            "profits) on the dexes read (dexes_read; spot not included), read after the halt; a failed read is "
            "reported in their place. The halt applies to every server of this config (each chat of an assistant "
            "may run its own) and lasts until none of them runs; it does not stop bots, or servers whose config file is in "
            "another folder."
        ),
        input_schema={"type": "object", "properties": {
            "account": ACCOUNT,
            "all_accounts": {"type": "boolean", "default": False, "description": "Halt every account that can trade."},
        }, "required": [], "additionalProperties": False},
        run=run, read_only=False, destructive=False, idempotent=True, own_capacity=True, first=halt,
    )


def _halt_every(registry: AccountRegistry, targets: list[Target]) -> None:
    """Halt every target, also after one whose shared halt failed (each is halted in this
    server first); then one error naming every account the other servers may not see halted."""
    failed = []
    for target in targets:
        try:
            registry.halt(target)
        except ToolError:
            failed.append(str(target.name))
    if failed:
        raise ToolError("INTERNAL", f"halted in this server only: {', '.join(failed)} (the shared halt cannot be "
                                    "written); every other account asked for is halted for every server",
                        hint="other chats on those accounts are not halted: quit them, or the assistant")


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
    return {**live,
            "positions": [{key: convert.position(p)[key] for key in ("symbol", "dex", "side", "size")}
                          for p in positions],
            "open_orders": {"count": len(orders), "stop_losses": sum(1 for o in orders if o.type == "stop_loss"),
                            "take_profits": sum(1 for o in orders if o.type == "take_profit")},
            "error": None}


def _canceled(canceled: bool) -> tuple[dict[str, Any], str]:
    return {"canceled": canceled}, "canceled" if canceled else "not open"


def _risk_state(registry: AccountRegistry, client: Any, target: Target, symbol: str,
                is_buy: bool) -> guards.RiskState:
    """What the account's guardrails need before an order that adds risk, read with its own
    client: the market's leverage, for a sell the mid, and the rest only as far as a
    configured guardrail needs it (guards.needs). A failed read is GUARD_BLOCKED, retryable
    as its cause: the order is not sent."""
    wanted = guards.needs(_config(target))
    reads: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    if "leverage" in wanted:
        reads.append(("the market's leverage", lambda: {"leverage": client.get_leverage(symbol)}))
    if not is_buy:
        reads.append(("the market's mid price", lambda: {"mid": convert.decimal(client.get_price(symbol))}))
    if "account" in wanted:
        reads.append(("the account's value and margin", lambda: _margins(client.get_account_summary())))
    if "day" in wanted:
        reads.append(("the last 24 hours' PnL", lambda: dict(zip(("day_loss", "day_capital"),
                                                                   _day_loss(client.get_portfolio())))))
    state: dict[str, Any] = {"orders_last_hour": _before_sending(lambda: registry.risk_orders_last_hour(target))}
    for what, read in reads:
        try:
            state.update(read())
        except Exception as error:  # nothing is sent yet, whatever failed
            raise _read_failed(what, error) from error
    return guards.RiskState(**state)


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
                   stops: list[dict[str, Any]]) -> Optional[Decimal]:
    """The loss of an open_position if every stop loss fills, as plan_trade computes it (a
    market entry; fees and slippage estimated from the order book); None if not estimable."""
    from . import risk as risk_tools  # its reads: the order book and the fee rates; the prices as sent
    return risk_math.planned_loss(
        size, is_long=is_long, limit=None, resting=False, stops=risk_tools.sent_levels(client, symbol, is_long, stops),
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


def _day_loss(portfolio: Any) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """The loss over the last 24 hours and the account value without it, from Hyperliquid's
    portfolio ("day": account value and cumulative PnL histories, sampled at the same times,
    the last one current): the last value minus the PnL, which is the value 24 hours ago
    plus deposits, minus withdrawals. (None, None), which the guardrail refuses, if either
    history is missing or empty, they end at different times, or a figure is no number."""
    try:
        day = dict(portfolio).get("day")
        if not isinstance(day, dict):
            return None, None
        values, pnl = day.get("accountValueHistory"), day.get("pnlHistory")
        if not isinstance(values, list) or not isinstance(pnl, list) or not values or not pnl \
                or values[-1][0] != pnl[-1][0]:
            return None, None
        last, first, value = (Decimal(str(pnl[-1][1])), Decimal(str(pnl[0][1])), Decimal(str(values[-1][1])))
        if not (last.is_finite() and first.is_finite() and value.is_finite()):
            return None, None
        change = last - first
        return max(Decimal(0), -change), value - change
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, ArithmeticError):
        return None, None


def _config(target: Target) -> Any:
    """The configuration of a trading account (resolve_trade returns only those)."""
    if target.account is None:
        raise ToolError("TRADING_DISABLED", f"{target.address} is not a configured account")
    return target.account
