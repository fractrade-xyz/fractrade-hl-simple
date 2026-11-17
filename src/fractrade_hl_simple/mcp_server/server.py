"""The MCP server: the SDK's low-level Server with the tools' own
schemas, strict argument checks, one dispatcher and the error bodies of errors.py.

The SDK serves both protocol eras (the initialize handshake and 2026-07-28), server/discover,
stdio framing and cancellation. Every tool call passes through the dispatcher here: the
arguments are checked against the tool's JSON Schema (types exactly, no unknown fields), the
tool runs in a worker thread, and the result is compact JSON; any failure is the error body.

Importing this module needs the ``mcp`` extra.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import anyio
import anyio.to_thread
from jsonschema import Draft202012Validator, validators
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from ..hyperliquid import HyperliquidClient
from ..models import HyperliquidAccount
from .errors import ToolError, error_json, scrub

NAME = "fractrade-hl-simple"
TOOL_THREADS = 8  # tool calls at once; their own threads, apart from the SDK's stdio threads
OWN_THREADS = 2  # for tools with capacity of their own (kill_switch): never queued behind the others

INSTRUCTIONS = """\
Reads Hyperliquid accounts and markets: balances, positions, orders, fills, prices, order \
books and funding, for the configured accounts and any public address.
Accounts: pass `account` (a configured name; see list_accounts) or `address`; without \
either, read tools use the default account.
Symbols exactly as Hyperliquid names them: perps such as BTC, HIP-3 markets with their dex \
prefix such as xyz:NVDA, spot tokens by name such as HYPE. Sizes are in base units; prices, \
sizes and USD amounts are decimal strings.
Hyperliquid's own perps are always read. HIP-3 dexes (xyz, para, ...) only when enabled: in \
the config, or with enable_perp_dexs until the server restarts; list_accounts shows them. \
Positions and orders on a dex that is not enabled are not seen.
Outside a market's oracle schedule, xyz: equity prices come from the dex's own order book, \
not from the stock exchange.
A failed call returns {"error": {"code", "message", "retryable", ...}}: call again later \
only when retryable is true.
This server has no trading tools."""

# The instructions when the trade tools are registered: the read part, then trading.
TRADING_INSTRUCTIONS = INSTRUCTIONS.removesuffix("This server has no trading tools.") + """\
Trade tools trade real money for `account`, or without it the only account that can trade \
(never the default account), within its limits. Orders without limit_price are market \
orders. On OUTCOME_UNKNOWN never call again: follow the hint; an order not found by its cloid \
can still arrive. After a trade call without an answer, check \
positions, open orders and fills first. kill_switch halts trading but keeps stop losses; \
cancel_all_orders and close_all_positions are separate steps."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its public interface (name, description, JSON Schema) and what it runs.

    `run` gets the checked arguments and returns JSON data (dicts, lists, str, int, bool,
    None, Decimal); it runs in a worker thread and may raise any library exception.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]
    run: Callable[[dict[str, Any]], Any]
    read_only: bool = True
    open_world: bool = True  # talks to Hyperliquid
    # For a tool that is not read only (MCP's defaults: destructive, not idempotent).
    destructive: Optional[bool] = None
    idempotent: Optional[bool] = None
    own_capacity: bool = False  # runs in threads of its own, so busy tools can never delay it
    # Run at once when the call arrives, in the event loop, before the call waits for any thread:
    # a few local steps without a request (the kill switch's halt).
    first: Optional[Callable[[dict[str, Any]], None]] = None

    def tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            description=self.description,
            input_schema=dict(self.input_schema),
            annotations=types.ToolAnnotations(read_only_hint=self.read_only, open_world_hint=self.open_world,
                                              destructive_hint=self.destructive, idempotent_hint=self.idempotent),
        )


def make_client(base_url: str, account: Optional[HyperliquidAccount], default_slippage: Optional[float],
                perp_dexs: tuple[str, ...]) -> HyperliquidClient:
    """The client factory of the account registry: nothing comes from the environment."""
    if default_slippage is None:  # a client without a key places no orders
        return HyperliquidClient(account=account, base_url=base_url, trust_env=False, perp_dexs=list(perp_dexs))
    return HyperliquidClient(account=account, base_url=base_url, trust_env=False, perp_dexs=list(perp_dexs),
                             default_slippage=default_slippage)


def to_json(data: Any) -> str:
    """Compact JSON; Decimal as a decimal string. NaN and infinity are refused."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_json_default)


def _is_integer(checker: Any, instance: Any) -> bool:
    """An integer is an int: JSON Schema would take 2.0 too, which the tools cannot use."""
    return isinstance(instance, int) and not isinstance(instance, bool)


# Draft 2020-12, with "integer" meaning an int (no integral floats).
Validator = validators.extend(
    Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer", _is_integer))


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{value} is not a finite number")
        return format(value, "f")  # never 1E-7
    raise TypeError(f"{type(value).__name__} is not JSON data")


def _non_finite(value: Any, path: tuple[str, ...] = ()) -> Optional[tuple[str, ...]]:
    """The path of the first NaN or infinite number in `value`, or None. JSON Schema has no
    way to refuse them, and the JSON parser accepts NaN and turns 1e999 into infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        return path
    if isinstance(value, Mapping):
        items: Any = value.items()
    elif isinstance(value, list):
        items = enumerate(value)
    else:
        return None
    for key, item in items:
        found = _non_finite(item, (*path, str(key)))
        if found is not None:
            return found
    return None


def _check_arguments(spec: ToolSpec, validator: Any, arguments: Mapping[str, Any]) -> None:
    """Raise INVALID_ARGUMENT for the first problem, naming the parameter."""
    parameters = ", ".join(spec.input_schema.get("properties", {})) or "none"
    non_finite = _non_finite(arguments)
    if non_finite is not None:
        raise ToolError("INVALID_ARGUMENT", f"{'/'.join(non_finite)}: must be a finite number",
                        hint=f"parameters of {spec.name}: {parameters}")
    error = next(iter(sorted(validator.iter_errors(arguments), key=lambda e: list(map(str, e.absolute_path)))), None)
    if error is None:
        return
    where = "/".join(map(str, error.absolute_path)) or "arguments"
    raise ToolError("INVALID_ARGUMENT", f"{where}: {error.message}", hint=f"parameters of {spec.name}: {parameters}")


def _text(text: str, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


def _version() -> str:
    try:
        return importlib.metadata.version("fractrade-hl-simple")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


def build_server(tools: Sequence[ToolSpec], instructions: str = INSTRUCTIONS) -> Server[Any]:
    """The server offering `tools`, in this order. Makes no network request."""
    by_name = {spec.name: spec for spec in tools}
    if len(by_name) != len(tools):
        raise ValueError("tool names must be unique")
    checkers = {}
    for spec in tools:
        Validator.check_schema(spec.input_schema)
        checkers[spec.name] = Validator(spec.input_schema)
    listed = types.ListToolsResult(tools=[spec.tool() for spec in tools])
    limiters: dict[bool, anyio.CapacityLimiter] = {}  # by own_capacity; made in the event loop, on first use

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return listed

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        spec = by_name.get(params.name)
        if spec is None:  # a protocol error, not a tool error (MCP specification, tools)
            raise MCPError(types.INVALID_PARAMS, scrub(f"unknown tool {params.name!r}"))
        arguments = dict(params.arguments or {})
        if not limiters:
            limiters.update({False: anyio.CapacityLimiter(TOOL_THREADS), True: anyio.CapacityLimiter(OWN_THREADS)})
        try:
            _check_arguments(spec, checkers[spec.name], arguments)
            if spec.first is not None:
                spec.first(arguments)
            # Tools have their own threads: blocked calls can never take the threads the SDK
            # reads stdin with. A call whose client goes away still runs to its end.
            result = await anyio.to_thread.run_sync(spec.run, arguments, limiter=limiters[spec.own_capacity])
            return _text(to_json(result), is_error=False)
        except Exception as error:  # every failure is the error body
            return _text(error_json(error), is_error=True)

    return Server(NAME, version=_version(), instructions=instructions,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def serve_stdio(server: Server[Any]) -> None:
    """Serve one client over stdin/stdout until it closes the connection."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
