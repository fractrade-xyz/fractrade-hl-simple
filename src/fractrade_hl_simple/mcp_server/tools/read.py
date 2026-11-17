"""Read tools. Each tool's input schema and output shape is its public interface:
written out here; the server checks the arguments exactly (no type coercion, no unknown
parameters). Tools about one wallet read it through the client without a key of its
base_url, passing the address, and answer in the common envelope (`account`, `address`, `base_url`).
enable_perp_dexs is here too: it changes which HIP-3 dexes are read, not what is traded."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from ...exceptions import UnknownSymbolException
from ...hyperliquid import HyperliquidClient
from ...models import (
    PERP_MARKETS, AccountSummary, CollateralBalance, DexAccount, Fill, Market, SpotMarket, coin_market,
)
from ..accounts import AccountRegistry, Target, require_enabled_dexes
from ..convert import dex as _dex, iso as _iso, order as _order, position as _position, side as _side
from ..config import BASE_URLS, AccountConfig, Config
from ..errors import ToolError
from ..server import ToolSpec

NO_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

# Which wallet a tool reads; the same for every tool about one wallet.
ACCOUNT_PARAMETERS: dict[str, Any] = {
    "account": {"type": "string", "description": "A configured account name (see list_accounts). "
                "Without account and address: the default account."},
    "address": {"type": "string", "pattern": "^0x[0-9a-fA-F]{40}$",
                "description": "Any public address, instead of account."},
    "base_url": {"type": "string", "enum": list(BASE_URLS),
                 "description": "Only with address: the Hyperliquid API to read (default mainnet)."},
}
SYMBOL = {"type": "string", "minLength": 1,
          "description": "Only this market, exactly as Hyperliquid names it (BTC, xyz:NVDA)."}
TIME = {"type": "string", "minLength": 1,
        "description": "ISO 8601 time, e.g. 2026-11-01T00:00:00Z (UTC if no offset)."}
MARKET_KIND = {"type": "string", "enum": ["perp", "spot"], "default": "perp",
               "description": "perp (default) or spot: symbols are then tokens (HYPE, quoted in USDC) or "
                              "pairs with another quote (HYPE/USDT0)."}
FILLS_LIMIT = 500
RECENT_FILLS = 2000      # userFills returns at most the 2000 most recent fills


def read_tools(config: Config, registry: AccountRegistry) -> list[ToolSpec]:
    """The read tools, in the order clients list them."""
    return [
        _list_accounts(config, registry),
        _enable_perp_dexs(registry),
        _get_account(registry),
        _get_positions(registry),
        _get_open_orders(registry),
        _get_fills(registry),
        _get_order_status(registry),
        _get_order_status_by_cloid(registry),
        _get_portfolio(registry),
        _get_funding_payments(registry),
        _get_prices(registry),
        _list_markets(registry),
        _get_market_info(registry),
        _get_order_book(registry),
        _get_funding_rates(registry),
        *_risk_tools(registry),
    ]


def _risk_tools(registry: AccountRegistry) -> list[ToolSpec]:
    from .risk import risk_tools  # imports this module's shared pieces
    return risk_tools(registry)


# ── shared pieces ─────────────────────────────────────────────────────


def _schema(properties: Optional[dict[str, Any]] = None, required: Iterable[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": {**ACCOUNT_PARAMETERS, **(properties or {})},
            "required": list(required), "additionalProperties": False}


def _wallet_tool(registry: AccountRegistry, name: str, description: str, properties: Optional[dict[str, Any]],
                 read: Callable[[Any, Target, dict[str, Any]], dict[str, Any]],
                 required: Iterable[str] = ()) -> ToolSpec:
    """A tool about one wallet: resolves the target, reads with its client, wraps the result."""
    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        target = registry.resolve_read(arguments.get("account"), arguments.get("address"), arguments.get("base_url"))
        result = read(registry.read_client(target), target, arguments)
        return {"account": target.name, "address": target.address, "base_url": target.base_url, **result}

    return ToolSpec(name=name, description=description, input_schema=_schema(properties, required), run=run)


def _listed(items: list[Any], total: Optional[int] = None) -> dict[str, Any]:
    """The list fields of the envelope."""
    total = len(items) if total is None else total
    return {"count": len(items), "truncated": total > len(items), "data": items}


def _ms(text: str, name: str) -> int:
    """An ISO 8601 time as milliseconds since the epoch; UTC if it has no offset."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ToolError("INVALID_ARGUMENT", f"{name}: not an ISO 8601 time", hint="e.g. 2026-11-01T00:00:00Z") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _name_key(symbol: str) -> str:
    """A market's name without its dex or quote: NVDA for xyz:NVDA, HYPE for HYPE/USDT0."""
    return symbol.split(":")[-1].split("/")[0].lower()


def _unknown_symbol(symbol: str, names: Iterable[str], note: str = "") -> UnknownSymbolException:
    """UnknownSymbolException for `symbol`, suggesting the markets of that name on other dexes
    or with another quote."""
    wanted = _name_key(symbol)
    similar = [name for name in names if _name_key(name) == wanted][:3]
    hint = f"; did you mean {', '.join(map(repr, similar))}?" if similar else note
    return UnknownSymbolException(f"Unknown symbol {symbol!r}{hint}")


def _check_unmatched_symbol(client: Any, symbol: Optional[str], every_dex: bool = False) -> None:
    """Called when a perp `symbol` filter matched nothing: refuse a symbol that is no market of
    the dexes read (delisted ones included), so a misspelled filter is UNKNOWN_SYMBOL, never an
    empty list that reads as "nothing there". A symbol that matched records is never refused.
    The client's market list is from its creation and can miss a newer listing: before
    refusing, the current markets are read with get_markets(), whose failures are the
    library's own (a network error stays retryable, never a wrong UNKNOWN_SYMBOL). With
    every_dex (fills and funding payments, which cover every dex), a symbol of a HIP-3 dex
    that is not read cannot be checked and passes; get_positions and get_open_orders refuse
    it, as they do not read that dex."""
    if symbol is None or symbol in client.market_specs:
        return
    dexes_read = [dex for dex in client.perp_dexs if dex]
    dex = _dex(symbol)
    if coin_market(symbol) == "hip3_perp" and dex not in dexes_read:
        if every_dex:
            return
        raise _unknown_symbol(symbol, client.market_specs,
                              f"; the {dex} dex is not enabled (read: {', '.join(['default', *dexes_read])}); "
                              "enable_perp_dexs adds it")
    current = [market.symbol for market in client.get_markets()]
    if symbol not in current:
        raise _unknown_symbol(symbol, current)


# ── list_accounts ─────────────────────────────────────────────────────


def _list_accounts(config: Config, registry: AccountRegistry) -> ToolSpec:
    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "default_account": config.default_account,
            "trading_tools": registry.trading_enabled,
            "accounts": [_account(account, registry) for account in config.accounts.values()],
        }

    return ToolSpec(
        name="list_accounts",
        description=(
            "The configured accounts: name, address, base_url (the Hyperliquid API, mainnet or "
            "testnet), mode (trade: this server can trade it; watch: read only), whether it is "
            "halted, its trading limits if configured, and perp_dexs: the HIP-3 dexes it trades "
            "besides Hyperliquid's own perps (config and enable_perp_dexs); reads at its network cover "
            "the dexes of every account there. Use a name as `account` in other tools."
        ),
        input_schema=NO_PARAMETERS,
        run=run,
        open_world=False,
    )


def _account(account: AccountConfig, registry: AccountRegistry) -> dict[str, Any]:
    target = registry.resolve_read(account=account.name)
    return {
        "name": account.name,
        "address": account.address,
        "base_url": account.base_url,
        "mode": "trade" if registry.can_trade(account.name) else "watch",
        "halted": registry.is_halted(target),
        "limits": _limits(account),
        "perp_dexs": list(registry.account_perp_dexs(account)),
    }


def _limits(account: AccountConfig) -> Optional[dict[str, Any]]:
    """The configured trading limits, or None for an account without trading configured."""
    if not account.trading:
        return None
    return {
        "max_order_usd": account.max_order_usd,
        "max_leverage": account.max_leverage,
        "max_slippage": account.max_slippage,
        "symbols": None if account.symbols is None else sorted(account.symbols),
        "max_margin_pct": account.max_margin_pct,
        "max_total_margin_pct": account.max_total_margin_pct,
        "max_daily_loss_pct": account.max_daily_loss_pct,
        "isolated_only": account.isolated_only,
        "max_orders_per_hour": account.max_orders_per_hour,
        "check_order_leverage": account.check_order_leverage,
        "max_risk_pct": account.max_risk_pct,
    }


# ── enable_perp_dexs ──────────────────────────────────────────────────


def _enable_perp_dexs(registry: AccountRegistry) -> ToolSpec:
    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        base_url = arguments.get("base_url", BASE_URLS[0])
        read, added = registry.enable_perp_dexs(base_url, arguments["dexes"])
        return {"base_url": base_url, "perp_dexs": list(read), "enabled": list(added),
                "note": "until the server restarts; to keep them, add perp_dexs to the config "
                        "([defaults] or the account)"}

    return ToolSpec(
        name="enable_perp_dexs",
        description=(
            "Read and trade the markets of HIP-3 dexes (builder-deployed perps: xyz stocks, commodities and "
            "forex, para, io, ...) at base_url too, for every account there, until the server restarts. "
            "Hyperliquid's own perps are always read; a HIP-3 dex only when enabled here or in the config, so "
            "positions and orders on other dexes are not seen. Names as in their markets' prefix (xyz for "
            "xyz:NVDA). Each dex adds requests to every read of positions and open orders. A dex the network "
            "does not have is INVALID_ARGUMENT. Returns the dexes now read there."
        ),
        input_schema={"type": "object", "properties": {
            "dexes": {"type": "array", "items": {"type": "string", "minLength": 1, "pattern": "^\\S+$"},
                      "minItems": 1, "maxItems": 20, "uniqueItems": True,
                      "description": "HIP-3 dex names, e.g. [\"xyz\", \"para\"]."},
            "base_url": MARKET_BASE_URL,
        }, "required": ["dexes"], "additionalProperties": False},
        run=run, read_only=False, destructive=False, idempotent=True,
    )


# ── get_account ───────────────────────────────────────────────────────


def _get_account(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        summary: AccountSummary = client.get_account_summary(address=target.address)
        orders = [o for o in client.get_open_orders(address=target.address) if o.market in PERP_MARKETS]
        positions = [p for dex in summary.dexes for p in dex.positions if p.size != 0]
        return {"data": {
            "mode": summary.mode,
            "account_value": summary.account_value,
            "available_to_trade": summary.available_to_trade,
            "margin_used": summary.margin_used,
            "sources": summary.sources,
            "balances": [_balance(b) for b in summary.balances],
            "dexes": [_dex_account(d) for d in summary.dexes],
            "positions": [_position(p) for p in positions],
            "open_orders": len(orders),
        }}

    return _wallet_tool(
        registry, "get_account",
        "A wallet's account: its mode (unified: one collateral balance per token, shared by spot and every "
        "dex; standard: each dex has its own; portfolio_margin; unknown), account value in USD (PnL of open "
        "positions included), USDC available to trade (unified), margin used, every token balance (outcome "
        "balances and tokens without a price unpriced), and for the dexes read (Hyperliquid's own perps and the "
        "enabled HIP-3 dexes; others are not read): each dex, the open positions and the number of open perp "
        "orders. A figure that is not "
        "established for the mode is null; sources says how each one was computed and what is left out. The "
        "figures come from separate requests a moment apart.",
        None, read)


def _balance(balance: CollateralBalance) -> dict[str, Any]:
    return {"token": balance.token, "market": balance.market, "total": balance.total, "hold": balance.hold,
            "available": balance.available, "price": balance.price, "usd_value": balance.usd_value}


def _dex_account(dex: DexAccount) -> dict[str, Any]:
    return {"dex": dex.dex, "account_value": dex.account_value, "withdrawable": dex.withdrawable,
            "margin_used": dex.margin_used, "cross_maintenance_margin_used": dex.cross_maintenance_margin_used,
            "positions": sum(1 for p in dex.positions if p.size != 0)}


# ── get_positions ─────────────────────────────────────────────────────


def _get_positions(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = arguments.get("symbol")
        positions = [p for p in client.get_positions(address=target.address)
                     if p.size != 0 and (symbol is None or p.symbol == symbol)]
        if not positions:
            _check_unmatched_symbol(client, symbol)
        return _listed([_position(p) for p in positions])

    return _wallet_tool(
        registry, "get_positions",
        "Open perp positions of a wallet, on Hyperliquid's own perps and the enabled HIP-3 dexes (see "
        "enable_perp_dexs): symbol, side (long/short), "
        "size in base units, entry price, notional, unrealized PnL, return on equity, leverage and margin "
        "mode, liquidation price, margin used, funding since open.",
        {"symbol": SYMBOL}, read)


# ── get_open_orders ───────────────────────────────────────────────────


def _get_open_orders(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol = arguments.get("symbol")
        if arguments.get("market", "perp") == "spot":
            orders = client.get_spot_open_orders(symbol, address=target.address)
        else:  # the default dex's open orders include the spot and outcome ones
            orders = [o for o in client.get_open_orders(symbol, address=target.address) if o.market in PERP_MARKETS]
            if not orders:
                _check_unmatched_symbol(client, symbol)
        return _listed([_order(o) for o in orders])

    return _wallet_tool(
        registry, "get_open_orders",
        "Open orders of a wallet, stop loss and take profit triggers included: order id, client order id "
        "(cloid), symbol, side (buy/sell), type, size, filled and remaining size, limit and trigger price, "
        "reduce-only, time in force, created at. market: perp (default, every dex) or spot (symbol is then a "
        "token, e.g. HYPE).",
        {"symbol": SYMBOL, "market": MARKET_KIND},
        read)


# ── get_fills ─────────────────────────────────────────────────────────


def _get_fills(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol, start, end = arguments.get("symbol"), arguments.get("start"), arguments.get("end")
        limit = arguments.get("limit", 50)
        if start is None and end is not None:
            raise ToolError("INVALID_ARGUMENT", "end: only with start", hint="pass start too, or neither")
        if start is not None:
            start_ms = _ms(start, "start")
            end_ms = None if end is None else _ms(end, "end")
            if end_ms is not None and end_ms < start_ms:
                raise ToolError("INVALID_ARGUMENT", "end: before start")
            history = client.get_fill_history(start_ms, end_ms, address=target.address)
            fills, reason = history.fills, history.truncated_reason
        else:
            fills = client.get_fills(address=target.address)
            reason = "recent_fills" if len(fills) >= RECENT_FILLS else None
        # Filtered here, not by the library: whether Hyperliquid's limits cut off older fills
        # shows only in all of them. Outcome fills are left out.
        if arguments.get("market", "perp") == "spot":
            # Spot fills carry Hyperliquid's pair name (@107): named as the spot methods name them.
            names = {market.coin: market.symbol for market in client.get_spot_markets()}
            named = [(names.get(f.symbol, f.symbol), f) for f in fills if f.market == "spot"]
            matching = [(name, f) for name, f in named if symbol is None or name == symbol]
            if not matching and symbol is not None and symbol not in names.values():
                raise _unknown_symbol(symbol, names.values())
        else:
            matching = [(f.symbol, f) for f in fills
                        if f.market in PERP_MARKETS and (symbol is None or f.symbol == symbol)]
            if not matching:
                _check_unmatched_symbol(client, symbol, every_dex=True)
        newest_first = sorted(matching, key=lambda named_fill: named_fill[1].time, reverse=True)
        listed = _listed([_fill(f, name) for name, f in newest_first[:limit]], total=len(newest_first))
        if reason is None and listed["truncated"]:
            reason = "limit"
        return {**listed, "truncated": reason is not None, "truncated_reason": reason}

    return _wallet_tool(
        registry, "get_fills",
        "Fills (executed trades) of a wallet, newest first: time, symbol, side (buy/sell), direction (e.g. Open "
        "Long), price, size, closed PnL, fee, taker, order id, trade id. market: perp (default, every dex) or spot "
        "(symbols as in get_open_orders: HYPE, HYPE/USDT0). Without start: from the 2000 most "
        "recent fills; with start (and optionally end): that time range, complete unless truncated says otherwise. "
        "truncated_reason: limit (more fills than limit), recent_fills (only the 2000 most recent were read; pass "
        "start for older ones), retention_window (Hyperliquid keeps only an account's 10000 most recent fills; older "
        "ones in the range are gone) or same_millisecond_overflow.",
        {"symbol": SYMBOL, "market": MARKET_KIND, "start": TIME, "end": TIME,
         "limit": {"type": "integer", "minimum": 1, "maximum": FILLS_LIMIT, "default": 50,
                   "description": f"Most fills to return (default 50, at most {FILLS_LIMIT})."}},
        read)


def _fill(fill: Fill, symbol: str) -> dict[str, Any]:
    return {
        "time": _iso(fill.time),
        "symbol": symbol,
        "side": _side(fill.side == "B"),
        "direction": fill.direction,
        "price": fill.price,
        "size": fill.size,
        "closed_pnl": fill.closed_pnl,
        "fee": fill.fee,
        "fee_token": fill.fee_token,
        "taker": fill.crossed,
        "order_id": fill.order_id,
        "trade_id": fill.trade_id,
        "start_position": fill.start_position,
    }


# ── get_order_status ──────────────────────────────────────────────────


def _get_order_status(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        return _order_status(client.get_order_status(arguments["order_id"], address=target.address))

    return _wallet_tool(
        registry, "get_order_status",
        "The status of one order of a wallet by its order id, open or not (filled, canceled, triggered, "
        "rejected, ...), with the order itself. found is false if the wallet has no such order.",
        {"order_id": {"type": "integer", "minimum": 0, "description": "The order id (oid)."}},
        read, required=["order_id"])


def _get_order_status_by_cloid(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        return _order_status(client.get_order_status_by_cloid(arguments["cloid"], address=target.address))

    return _wallet_tool(
        registry, "get_order_status_by_cloid",
        "The status of one order of a wallet by the client order id it was placed with, as get_order_status. "
        "The tool to use after OUTCOME_UNKNOWN: found false means the order is not known yet, which is no "
        "permission to place it again (it can still arrive).",
        {"cloid": {"type": "string", "pattern": "^0x[0-9a-fA-F]{32}$",
                   "description": "The client order id: 0x and 32 hex digits."}},
        read, required=["cloid"])


def _order_status(raw: dict[str, Any]) -> dict[str, Any]:
    """An orderStatus answer: found, the status and its time, the order."""
    if raw.get("status") != "order":  # "unknownOid": this wallet has no such order
        return {"data": {"found": False}}
    record = raw["order"]
    return {"data": {"found": True, "status": record["status"], "status_time": _iso(record.get("statusTimestamp")),
                     "order": _status_order(record["order"])}}


def _status_order(order: dict[str, Any]) -> dict[str, Any]:
    """The order of an orderStatus response, with the fields and values of get_open_orders: it has
    the format of frontendOpenOrders, so the library's parser reads it (type stop_loss, not
    Hyperliquid's "Stop Market")."""
    return _order(HyperliquidClient._parse_open_order(order, order["coin"]))


# ── get_portfolio ─────────────────────────────────────────────────────

_PERIODS = {"day": "day", "week": "week", "month": "month", "allTime": "all_time",
            "perpDay": "perp_day", "perpWeek": "perp_week", "perpMonth": "perp_month", "perpAllTime": "perp_all_time"}


def _get_portfolio(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"data": {_PERIODS.get(period, period): _period(values)
                         for period, values in client.get_portfolio(address=target.address)}}

    return _wallet_tool(
        registry, "get_portfolio",
        "Performance of a wallet per period (day, week, month, all_time; perp_*: perp only): account value "
        "at the end, PnL over the period, trading volume, and the period's first and last time.",
        None, read)


def _period(values: dict[str, Any]) -> dict[str, Any]:
    history, pnl = values.get("accountValueHistory") or [], values.get("pnlHistory") or []
    return {
        "account_value": Decimal(history[-1][1]) if history else None,
        "pnl": Decimal(pnl[-1][1]) - Decimal(pnl[0][1]) if pnl else None,  # the history is cumulative
        "volume": Decimal(values["vlm"]) if "vlm" in values else None,
        "from": _iso(history[0][0]) if history else None,
        "to": _iso(history[-1][0]) if history else None,
    }


# ── get_funding_payments ──────────────────────────────────────────────

DAY_MS = 86_400_000
FUNDING_DEFAULT_DAYS, FUNDING_MAX_DAYS = 7, 31  # every hour adds one payment per position


def _get_funding_payments(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, target: Target, arguments: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time() * 1000)
        end_ms = now if "end" not in arguments else _ms(arguments["end"], "end")
        start_ms = end_ms - FUNDING_DEFAULT_DAYS * DAY_MS if "start" not in arguments else _ms(arguments["start"], "start")
        if end_ms < start_ms:
            raise ToolError("INVALID_ARGUMENT", "end: before start")
        if end_ms - start_ms > FUNDING_MAX_DAYS * DAY_MS:
            raise ToolError("INVALID_ARGUMENT", f"start: the range is longer than {FUNDING_MAX_DAYS} days",
                            hint="ask for several shorter ranges")
        payments = client.get_user_funding(start_ms, end_ms, arguments.get("symbol"), address=target.address)
        if not payments:
            _check_unmatched_symbol(client, arguments.get("symbol"), every_dex=True)
        newest_first = sorted(payments, key=lambda p: p.time, reverse=True)
        listed = _listed([{"time": _iso(p.time), "symbol": p.symbol, "position_size": p.size,
                           "funding_rate": p.funding_rate, "usdc": p.usdc}
                          for p in newest_first[:arguments.get("limit", 100)]], total=len(newest_first))
        return {"from": _iso(start_ms), "to": _iso(end_ms), "total_usdc": sum((p.usdc for p in payments), Decimal(0)),
                **listed}

    return _wallet_tool(
        registry, "get_funding_payments",
        "Funding a wallet paid or received, newest first: time, symbol, the position size then (negative: short), "
        "the hourly rate and usdc (positive: received, negative: paid), plus total_usdc over the whole range. "
        f"Default range: the last {FUNDING_DEFAULT_DAYS} days; at most {FUNDING_MAX_DAYS} days.",
        {"symbol": SYMBOL, "start": TIME, "end": TIME,
         "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100,
                   "description": "Most payments to return (default 100, at most 500); total_usdc covers all."}},
        read)


# ── market tools ──────────────────────────────────────────────────────

MARKET_BASE_URL = {"type": "string", "enum": list(BASE_URLS),
                   "description": "The Hyperliquid API to read (default mainnet)."}
DEX = {"type": "string", "minLength": 1,
       "description": "Only the markets of one perp dex: default (Hyperliquid's own perps) or an enabled HIP-3 "
                      "dex such as xyz (list_accounts; enable_perp_dexs)."}
MARKETS_LIMIT = 200
XYZ_NOTE = ("xyz: markets trade around the clock. Outside the external oracle's schedule (for stocks about "
            "24/5), the oracle follows the dex's own order book, so prices then do not come from the stock "
            "exchange.")


def _market_tool(registry: AccountRegistry, name: str, description: str, properties: dict[str, Any],
                 read: Callable[[Any, dict[str, Any]], dict[str, Any]], required: Iterable[str] = (),
                 check: Optional[Callable[[dict[str, Any]], None]] = None) -> ToolSpec:
    """A tool about markets: reads with the client without a key of base_url. `check` refuses
    arguments before any client is made, so an argument error never looks like a network one;
    so is a perp symbol of a HIP-3 dex that is not enabled."""
    def run(arguments: dict[str, Any]) -> dict[str, Any]:
        if check is not None:
            check(arguments)
        base_url = arguments.get("base_url", BASE_URLS[0])
        if arguments.get("market", "perp") == "perp":
            require_enabled_dexes(registry.perp_dexs(base_url),
                                  [arguments["symbol"]] if "symbol" in arguments else arguments.get("symbols", []))
        return {"base_url": base_url, **read(registry.market_client(base_url), arguments)}

    schema = {"type": "object", "properties": {**properties, "base_url": MARKET_BASE_URL},
              "required": list(required), "additionalProperties": False}
    return ToolSpec(name=name, description=description, input_schema=schema, run=run)


def _live(markets: list[Market], dex: Optional[str] = None) -> list[Market]:
    """Markets that are not delisted, of `dex` ("default": Hyperliquid's own perps), most
    traded first."""
    # "default" is Hyperliquid's own perps (dex None), never a HIP-3 dex of that name.
    chosen = [m for m in markets if not m.delisted
              and (dex is None or (m.dex is None if dex == "default" else m.dex == dex))]
    return sorted(chosen, key=lambda m: m.day_volume_usd or Decimal(0), reverse=True)


def _by_symbol(markets: list[Market], symbols: Iterable[str]) -> list[Market]:
    """The markets named, in that order; UnknownSymbolException for the first unknown."""
    known = {m.symbol: m for m in markets}
    missing = [symbol for symbol in symbols if symbol not in known]
    if missing:
        raise _unknown_symbol(missing[0], known)
    return [known[symbol] for symbol in symbols]


def _limit(description: str, default: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": maximum, "default": default,
            "description": f"{description} (default {default}, at most {maximum})."}


SYMBOLS = {"type": "array", "items": SYMBOL, "minItems": 1, "maxItems": 50, "uniqueItems": True,
           "description": "These markets (at most 50), exactly as Hyperliquid names them."}


def _spot(arguments: dict[str, Any]) -> bool:
    """Whether a market tool is asked about spot pairs; dex is a perp filter."""
    if arguments.get("market", "perp") != "spot":
        return False
    if "dex" in arguments:
        raise ToolError("INVALID_ARGUMENT", "dex: only for perp markets", hint="leave it out with market spot")
    return True


def _check_dex(registry: AccountRegistry, arguments: dict[str, Any]) -> None:
    """Refuse a dex filter that is not read at base_url, before any client is made."""
    dex = arguments.get("dex")
    if dex is None or dex == "default":
        return
    read = registry.perp_dexs(arguments.get("base_url", BASE_URLS[0]))
    if dex not in read:
        raise ToolError("INVALID_ARGUMENT", f"dex: {dex!r} is not enabled",
                        hint=f"enabled: {', '.join(['default', *read])}; enable_perp_dexs adds a HIP-3 dex")


def _market_check(registry: AccountRegistry) -> Callable[[dict[str, Any]], None]:
    """The argument check of a market tool with the spot switch and the dex filter."""
    def check(arguments: dict[str, Any]) -> None:
        if not _spot(arguments):
            _check_dex(registry, arguments)
    return check


def _most_traded_spot(markets: list[SpotMarket]) -> list[SpotMarket]:
    return sorted(markets, key=lambda m: m.day_volume or Decimal(0), reverse=True)


def _spot_by_symbol(markets: list[SpotMarket], symbols: Iterable[str]) -> list[SpotMarket]:
    """The spot pairs named, in that order; UnknownSymbolException for the first unknown."""
    known = {m.symbol: m for m in markets}
    missing = [symbol for symbol in symbols if symbol not in known]
    if missing:
        raise _unknown_symbol(missing[0], known)
    return [known[symbol] for symbol in symbols]


def _spot_row(market: SpotMarket) -> dict[str, Any]:
    return {"symbol": market.symbol, "base": market.base, "quote": market.quote, "size_decimals": market.size_decimals,
            "mark_price": market.mark_price, "mid_price": market.mid_price, "day_volume": market.day_volume}


def _get_prices(registry: AccountRegistry) -> ToolSpec:
    def check(arguments: dict[str, Any]) -> None:
        if _spot(arguments):
            return
        _check_dex(registry, arguments)
        if "symbols" not in arguments and "dex" not in arguments:
            raise ToolError("INVALID_ARGUMENT", "arguments: pass symbols or dex",
                            hint="this tool never lists every market; see list_markets")

    def read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        symbols, dex = arguments.get("symbols"), arguments.get("dex")
        if _spot(arguments):
            pairs = client.get_spot_markets()
            picked = (_spot_by_symbol(pairs, symbols) if symbols is not None
                      else _most_traded_spot(pairs)[:arguments.get("limit", 50)])
            return _listed([{"symbol": m.symbol, "mid_price": m.mid_price, "mark_price": m.mark_price}
                            for m in picked], total=len(picked) if symbols is not None else len(pairs))
        markets = client.get_markets()
        if symbols is not None:  # delisted markets left out: their figures are stale
            live = {m.symbol for m in _live(markets, dex)}
            chosen = [m for m in _by_symbol(markets, symbols) if m.symbol in live]
            total = len(chosen)
        else:
            chosen = _live(markets, dex)
            total = len(chosen)
            chosen = chosen[:arguments.get("limit", 50)]
        return _listed([{"symbol": m.symbol, "mid_price": m.mid_price, "mark_price": m.mark_price} for m in chosen],
                       total=total)

    return _market_tool(
        registry, "get_prices",
        "Current mid and mark prices. Perp markets: the symbols given, or the most traded markets of a dex "
        "(default or an enabled HIP-3 dex); at least one of symbols and dex is required; delisted markets are "
        "left out (see "
        "get_market_info). market spot: the spot pairs given, or the most traded; prices in the quote token.",
        {"symbols": SYMBOLS, "dex": DEX, "market": MARKET_KIND,
         "limit": _limit("Most markets without symbols", 50, MARKETS_LIMIT)},
        read, check=check)


def _list_markets(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        query = (arguments.get("query") or "").lower()
        limit = arguments.get("limit", 50)
        if _spot(arguments):
            pairs = [m for m in _most_traded_spot(client.get_spot_markets()) if query in m.symbol.lower()]
            return _listed([_spot_row(m) for m in pairs[:limit]], total=len(pairs))
        chosen = [m for m in _live(client.get_markets(), arguments.get("dex")) if query in m.symbol.lower()]
        return _listed([_market_row(m) for m in chosen[:limit]], total=len(chosen))

    return _market_tool(
        registry, "list_markets",
        "Perp markets that are open for trading (Hyperliquid's own and the enabled HIP-3 dexes), most traded "
        "first: symbol, dex, max leverage, margin mode "
        "(cross, or no_cross/strict_isolated: isolated only), size decimals, mark price, hourly funding, open "
        "interest (base units), 24h volume (USD), whether at the open interest cap. Delisted markets are left out. "
        "market spot: the spot pairs, most traded first: symbol, base and quote token, size decimals, mark and mid "
        "price and 24h volume, in the quote token.",
        {"dex": DEX, "market": MARKET_KIND,
         "query": {"type": "string", "minLength": 1,
                   "description": "Only symbols containing this text (case-insensitive)."},
         "limit": _limit("Most markets", 50, MARKETS_LIMIT)},
        read, check=_market_check(registry))


def _market_row(market: Market) -> dict[str, Any]:
    return {
        "symbol": market.symbol,
        "dex": market.dex,
        "max_leverage": market.max_leverage,
        "margin_mode": market.margin_mode,
        "size_decimals": market.size_decimals,
        "mark_price": market.mark_price,
        "funding_hourly": market.funding,
        "open_interest": market.open_interest,
        "day_volume_usd": market.day_volume_usd,
        "at_open_interest_cap": market.at_open_interest_cap,
    }


def _get_market_info(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        market, = _by_symbol(client.get_markets(), [arguments["symbol"]])
        change = None
        if market.mark_price is not None and market.previous_day_price:
            change = round(float((market.mark_price / market.previous_day_price - 1) * 100), 2)
        return {"data": {
            **_market_row(market),
            "delisted": market.delisted,
            "mid_price": market.mid_price,
            "oracle_price": market.oracle_price,
            "previous_day_price": market.previous_day_price,
            "change_24h_percent": change,
            "premium": market.premium,
            "note": XYZ_NOTE if market.dex == "xyz" else None,
        }}

    return _market_tool(
        registry, "get_market_info",
        "Everything about one perp market: the list_markets fields plus mid, oracle and previous-day price, "
        "24h change in percent, premium, whether it is delisted, and a note on xyz: pricing outside the "
        "oracle's schedule.",
        {"symbol": SYMBOL}, read, required=["symbol"])


def _get_order_book(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        symbol, depth = arguments["symbol"], arguments.get("depth", 10)
        if arguments.get("market", "perp") == "spot":
            book = client.get_spot_order_book(symbol, decimal=True)
        else:
            book = client.get_order_book(symbol, decimal=True)
        bids = [_level(level) for level in book["bids"][:depth]]
        asks = [_level(level) for level in book["asks"][:depth]]
        bid, ask = (bids[0]["price"] if bids else None), (asks[0]["price"] if asks else None)
        mid = spread = spread_bps = None
        if bid is not None and ask is not None:
            mid, spread = (bid + ask) / 2, ask - bid
            spread_bps = round(float(spread / mid * 10000), 2)
        return {"data": {"symbol": symbol, "time": _iso(book.get("timestamp")), "best_bid": bid, "best_ask": ask,
                         "mid_price": mid, "spread": spread, "spread_bps": spread_bps, "bids": bids, "asks": asks}}

    return _market_tool(
        registry, "get_order_book",
        "The order book of a market: the best bids and asks (price, size in base units, number of orders), "
        "spread and spread in basis points. market: perp (default) or spot (symbol is then a token, e.g. HYPE).",
        {"symbol": SYMBOL, "depth": _limit("Levels per side", 10, 20),
         "market": MARKET_KIND},
        read, required=["symbol"])


def _level(level: dict[str, Any]) -> dict[str, Any]:
    return {"price": level["price"], "size": level["size"], "orders": level.get("num_orders")}


def _get_funding_rates(registry: AccountRegistry) -> ToolSpec:
    def read(client: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        markets = client.get_markets()
        live = _live(markets)
        if "symbols" in arguments:  # delisted markets left out: their figures are stale
            names = {m.symbol for m in live}
            chosen = [m for m in _by_symbol(markets, arguments["symbols"]) if m.symbol in names]
        else:
            chosen = live
        threshold = arguments.get("threshold")
        rated = [m for m in chosen if m.funding is not None
                 and (threshold is None or abs(m.funding) >= Decimal(str(threshold)))]
        rated.sort(key=lambda m: abs(m.funding or Decimal(0)), reverse=True)
        top = arguments.get("top", 20)
        return _listed([{"symbol": m.symbol, "funding_hourly": m.funding,
                         "funding_annualized": (m.funding or Decimal(0)) * 24 * 365, "mark_price": m.mark_price}
                        for m in rated[:top]], total=len(rated))

    return _market_tool(
        registry, "get_funding_rates",
        "Current funding rates of perp markets, largest first by size: the hourly rate and the rate annualized, "
        "both as fractions (0.0001 = 0.01%; positive: longs pay shorts). The symbols given, or every open "
        "market; delisted markets are left out.",
        {"symbols": SYMBOLS, "top": _limit("Most markets", 20, MARKETS_LIMIT),
         "threshold": {"type": "number", "minimum": 0,
                       "description": "Only rates at least this large (hourly, absolute), e.g. 0.0001."}},
        read)
