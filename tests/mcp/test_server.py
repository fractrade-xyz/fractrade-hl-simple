"""The MCP server: both protocol eras, strict arguments, results and error bodies,
list_accounts, and a real stdio connection."""

import json
import queue
import subprocess
import sys
import threading
from decimal import Decimal

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
from mcp import Client  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

from fractrade_hl_simple.exceptions import UnknownSymbolException  # noqa: E402
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.errors import ToolError  # noqa: E402
from fractrade_hl_simple.mcp_server.server import INSTRUCTIONS, ToolSpec, _check_arguments, build_server, to_json  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import read_tools  # noqa: E402

MODES = ["legacy", "2026-07-28"]  # the handshake era and the stateless one
ECHO_SCHEMA = {
    "type": "object",
    "properties": {"symbol": {"type": "string"}, "size": {"type": "number"}, "reduce_only": {"type": "boolean"},
                   "cloid": {"type": "string", "pattern": "^0x[0-9a-f]{4}$"}},
    "required": ["symbol", "size"],
    "additionalProperties": False,
}


def _raise(error):
    raise error


def probe_server(ran=None):
    def echo(arguments):
        if ran is not None:
            ran.append(arguments)
        return {"got": arguments, "price": Decimal("1.50")}

    return build_server([
        ToolSpec("echo", "Echo the arguments.", ECHO_SCHEMA, echo, open_world=False),
        ToolSpec("unknown_symbol", "Raise a library exception.", {"type": "object", "additionalProperties": False},
                 lambda arguments: _raise(UnknownSymbolException("Unknown symbol 'BTCC'"))),
        ToolSpec("broken", "Raise a bug.", {"type": "object", "additionalProperties": False},
                 lambda arguments: _raise(KeyError("internal detail"))),
        ToolSpec("not_a_number", "Return NaN.", {"type": "object", "additionalProperties": False},
                 lambda arguments: {"price": Decimal("NaN")}),
    ])


def call(server, name, arguments, mode="2026-07-28"):
    async def run():
        async with Client(server, mode=mode) as client:
            result = await client.call_tool(name, arguments)
            return result.is_error, result.content[0].text

    return anyio.run(run)


class TestProtocol:
    @pytest.mark.parametrize("mode", MODES)
    def test_both_protocol_eras_list_and_call_tools(self, mode):
        async def run():
            async with Client(probe_server(), mode=mode) as client:
                tools = (await client.list_tools()).tools
                result = await client.call_tool("echo", {"symbol": "BTC", "size": 0.01})
                return client.protocol_version, client.instructions, tools, result

        version, instructions, tools, result = anyio.run(run)
        assert (version == "2026-07-28") is (mode == "2026-07-28")
        assert [tool.name for tool in tools] == ["echo", "unknown_symbol", "broken", "not_a_number"]
        assert tools[0].input_schema == ECHO_SCHEMA and tools[0].annotations.read_only_hint
        assert not result.is_error

    def test_instructions_are_short(self):
        assert len(INSTRUCTIONS) < 1500

    def test_an_unknown_tool_name_is_redacted_in_the_error(self):
        key = "ab" * 32

        async def run():
            async with Client(probe_server(), mode="2026-07-28") as client:
                with pytest.raises(MCPError) as error:
                    await client.call_tool(key, {})
                return error.value

        error = anyio.run(run)
        assert error.code == -32602 and key not in error.message

    def test_tools_never_take_the_threads_the_sdk_reads_stdin_with(self):
        started, release = threading.Event(), threading.Event()
        borrowed = []

        def block(arguments):
            started.set()
            release.wait(5)
            return {}

        server = build_server([ToolSpec("block", "Block.", {"type": "object"}, block)])

        async def run():
            async with Client(server, mode="2026-07-28") as client, anyio.create_task_group() as tasks:
                tasks.start_soon(client.call_tool, "block", {})
                await anyio.to_thread.run_sync(started.wait, 5, limiter=anyio.CapacityLimiter(1))
                borrowed.append(anyio.to_thread.current_default_thread_limiter().borrowed_tokens)
                release.set()

        anyio.run(run)
        assert borrowed == [0]  # the blocked tool holds a token of its own limiter only


class TestArguments:

    @pytest.mark.parametrize("value, text", [
        (Decimal("1E-7"), '"0.0000001"'), (Decimal("1E+2"), '"100"'), (Decimal("0"), '"0"'),
        (Decimal("1.50"), '"1.50"'), (Decimal("-0.00012"), '"-0.00012"'),
    ])
    def test_decimals_are_plain_decimal_strings_at_any_depth(self, value, text):
        assert to_json({"a": [{"b": value}]}) == '{"a":[{"b":' + text + '}]}'

    @pytest.mark.parametrize("arguments, where", [
        ({"symbol": "BTC", "size": 0.01, "reduceOnly": True}, "arguments"),  # misspelled: never ignored
        ({"symbol": "BTC", "size": "0.01"}, "size"),                         # no type coercion
        ({"symbol": "BTC", "size": True}, "size"),                           # a boolean is no number
        ({"symbol": "BTC", "size": 0.01, "reduce_only": "true"}, "reduce_only"),
        ({"size": 0.01}, "arguments"),                                       # missing
        ({"symbol": "BTC", "size": 0.01, "cloid": "0x00ab\n"}, "cloid"),     # a pattern takes the whole string
    ])
    def test_wrong_arguments_are_refused_with_the_parameter_named(self, arguments, where):
        is_error, text = call(probe_server(), "echo", arguments)
        error = json.loads(text)["error"]
        assert is_error and error["code"] == "INVALID_ARGUMENT" and error["message"].startswith(f"{where}: ")
        assert "symbol, size, reduce_only, cloid" in error["hint"]

    @pytest.mark.parametrize("arguments, where", [
        ({"symbol": "BTC", "size": float("nan")}, "size"),
        ({"symbol": "BTC", "size": float("inf")}, "size"),           # also what 1e999 becomes
        ({"symbol": "BTC", "size": 1.0, "nested": [{"x": float("-inf")}]}, "nested/0/x"),
    ])
    def test_non_finite_numbers_are_refused(self, arguments, where):
        # JSON Schema cannot refuse them; the SDK's client sends NaN as null, so the wire test
        # below sends them as raw JSON text.
        spec = ToolSpec("echo", "Echo.", ECHO_SCHEMA, lambda arguments: None)
        with pytest.raises(ToolError) as error:
            _check_arguments(spec, Draft202012Validator(ECHO_SCHEMA), arguments)
        assert (error.value.code, str(error.value)) == ("INVALID_ARGUMENT", f"{where}: must be a finite number")

    @pytest.mark.parametrize("mode", MODES)
    def test_the_check_is_the_same_in_both_eras(self, mode):
        is_error, text = call(probe_server(), "echo", {"symbol": "BTC", "size": 0.01, "reduceOnly": True}, mode)
        assert is_error and json.loads(text)["error"]["code"] == "INVALID_ARGUMENT"


class TestErrors:

    def test_a_result_that_is_not_a_finite_number_is_internal(self):
        is_error, text = call(probe_server(), "not_a_number", {})
        assert is_error and json.loads(text)["error"]["code"] == "INTERNAL"

    def test_anything_else_is_internal_without_details(self):
        is_error, text = call(probe_server(), "broken", {})
        error = json.loads(text)["error"]
        assert is_error and error["code"] == "INTERNAL" and "internal detail" not in text

    def test_tool_names_are_unique(self):
        spec = ToolSpec("echo", "Echo.", ECHO_SCHEMA, lambda arguments: None)
        with pytest.raises(ValueError, match="unique"):
            build_server([spec, spec])


MAIN = "0x" + "1" * 40
WHALE = "0x" + "3" * 40
CONFIG = f'''
default_account = "whale"

[accounts.main]
address = "{MAIN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5
symbols = ["ETH", "BTC"]
perp_dexs = ["xyz"]

[accounts.whale]
address = "{WHALE}"
base_url = "https://api.hyperliquid-testnet.xyz"
'''


class TestCapacity:
    def test_a_tool_with_capacity_of_its_own_runs_while_every_other_thread_is_busy(self):
        # A kill switch must never queue behind trade calls hanging on the network.
        from fractrade_hl_simple.mcp_server.server import TOOL_THREADS
        release, waiting = threading.Event(), []
        empty = {"type": "object", "additionalProperties": False}

        def hang(arguments):
            waiting.append(1)
            release.wait(10)
            return {}

        server = build_server([ToolSpec("hang", "Hang.", empty, hang),
                               ToolSpec("urgent", "Answer.", empty, lambda arguments: {"ok": True}, own_capacity=True)])

        async def run():
            async with Client(server, mode="2026-07-28") as client:
                async with anyio.create_task_group() as group:
                    for _ in range(TOOL_THREADS + 1):  # every thread busy, and one more call queued
                        group.start_soon(client.call_tool, "hang", {})
                    while len(waiting) < TOOL_THREADS:
                        await anyio.sleep(0.01)
                    with anyio.fail_after(5):
                        result = await client.call_tool("urgent", {})
                    answered_while_busy = len(waiting) == TOOL_THREADS
                    release.set()
                return result, answered_while_busy

        result, answered_while_busy = anyio.run(run)
        assert json.loads(result.content[0].text) == {"ok": True} and answered_while_busy


class TestClientFactory:
    def test_a_trading_client_uses_the_slippage_cap_as_its_default(self, monkeypatch):
        # Without it a market order without slippage, and close_all_positions, would use the
        # library's 5 %.
        from fractrade_hl_simple.mcp_server import server
        made = []
        monkeypatch.setattr(server, "HyperliquidClient", lambda **options: made.append(options))
        server.make_client("https://api.hyperliquid.xyz", None, None, ())
        server.make_client("https://api.hyperliquid.xyz", "account", 0.01, ("xyz", "para"))
        assert "default_slippage" not in made[0] and made[1]["default_slippage"] == 0.01
        # The dexes as the library takes them; none means Hyperliquid's own perps only.
        assert [options["perp_dexs"] for options in made] == [[], ["xyz", "para"]]
        assert all(options["trust_env"] is False and "extended_universe" not in options for options in made)

    def test_metadata_refresh_can_bypass_the_library_cache(self, monkeypatch):
        from fractrade_hl_simple.mcp_server import server
        made = []
        monkeypatch.setattr(server, "HyperliquidClient", lambda **options: made.append(options))
        server.make_client("https://api.hyperliquid.xyz", None, None, (), cache_market_specs=False)
        server.make_client("https://api.hyperliquid.xyz", "account", 0.01, ("xyz",), cache_market_specs=False)
        assert all(options["cache_market_specs"] is False and options["trust_env"] is False for options in made)


def no_clients(base_url, account, default_slippage, perp_dexs):
    raise AssertionError("list_accounts must not create a client")


class TestListAccounts:
    def test_the_configured_accounts_with_their_mode_and_limits(self):
        config = parse_config(CONFIG, "test.toml")
        registry = AccountRegistry(config, {}, no_clients)  # read only: no keys loaded
        is_error, text = call(build_server(read_tools(config, registry)), "list_accounts", {})
        assert not is_error and json.loads(text) == {
            "default_account": "whale",
            "trading_tools": False,
            "accounts": [
                {"name": "main", "address": MAIN, "base_url": "https://api.hyperliquid.xyz", "mode": "watch",
                 "halted": False, "limits": {"max_order_usd": "100", "max_leverage": 5,
                                             "max_slippage": "0.02", "symbols": ["BTC", "ETH"],
                                             "max_margin_pct": None, "max_total_margin_pct": None,
                                             "max_daily_loss_pct": None, "isolated_only": False,
                                             "max_orders_per_hour": None, "check_order_leverage": False,
                                             "max_risk_pct": None, "max_day_loss_pct": None,
                                             "max_day_loss_mode": None, "max_total_notional_x": None,
                                             "max_total_notional_mode": None, "day_start": "00:00", "tz": "UTC"},
                 "perp_dexs": ["xyz"]},
                {"name": "whale", "address": WHALE, "base_url": "https://api.hyperliquid-testnet.xyz",
                 "mode": "watch", "halted": False, "limits": None, "perp_dexs": []},
            ],
        }

TOOL_NAMES = ["list_accounts", "enable_perp_dexs", "refresh_market_specs", "get_account", "get_positions", "get_open_orders", "get_fills", "get_changes", "get_order_status",
              "get_order_status_by_cloid", "get_portfolio",
              "get_funding_payments", "get_pnl", "get_prices", "list_markets", "get_market_info", "get_order_book", "get_candles", "get_funding_rates",
              "get_position_risk", "get_account_risk", "plan_trade"]

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
}


class StdioServer:
    """The server as an MCP client starts it, spoken to in raw JSON-RPC lines: every line it
    writes to stdout, to the end, must be one. Use it as a context manager: the
    process is always ended and reaped, its pipes drained and closed."""

    def __init__(self, tmp_path, args=(), env=None, config=CONFIG):
        config_file = tmp_path / "mcp.toml"
        config_file.write_text(config)
        config_file.chmod(0o600)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "fractrade_hl_simple.mcp_server", "--config", str(config_file), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **(env or {})})  # nothing from the real environment
        self.lines: queue.Queue = queue.Queue()
        self.stderr: list = []
        self._readers = [threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=lambda: self.stderr.extend(self.process.stderr), daemon=True)]
        for reader in self._readers:
            reader.start()

    def _read_stdout(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)  # end of stdout

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=30)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            for reader in self._readers:
                reader.join(timeout=10)
            self.process.stdout.close()
            self.process.stderr.close()
        rest = []
        while (line := self.lines.get(timeout=5)) is not None:  # everything up to the end
            rest.append(line)
        assert all(json.loads(line)["jsonrpc"] == "2.0" for line in rest), rest

    def send(self, message):
        self.send_text(json.dumps(message))

    def send_text(self, text):
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()

    def request(self, id, method, params, text=None):
        self.send_text(text or json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))
        return self.response(id)

    def response(self, id):
        while True:
            line = self.lines.get(timeout=30)
            if line is None:
                self.lines.put(None)  # keep the end marker for __exit__, so this failure shows
                raise AssertionError(f"the server closed stdout; stderr: {''.join(self.stderr)[-2000:]}")
            message = json.loads(line)  # a line that is no JSON fails here
            assert message["jsonrpc"] == "2.0"
            if message.get("id") == id:
                return message


@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])  # 2025-06-18: the previous protocol version, still sent by some clients
def test_a_real_stdio_connection_in_the_handshake_era(tmp_path, version):
    with StdioServer(tmp_path) as server:
        init = server.request(1, "initialize", {
            "protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        assert init["result"]["protocolVersion"] == version
        assert init["result"]["instructions"] == INSTRUCTIONS and "tools" in init["result"]["capabilities"]
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert [tool["name"] for tool in server.request(2, "tools/list", {})["result"]["tools"]] == TOOL_NAMES
        result = server.request(3, "tools/call", {"name": "list_accounts", "arguments": {}})["result"]
        assert not result["isError"] and json.loads(result["content"][0]["text"])["default_account"] == "whale"


def test_a_real_stdio_connection_in_the_2026_07_28_era(tmp_path):
    with StdioServer(tmp_path) as server:
        discover = server.request(1, "server/discover", {"_meta": MODERN_META})["result"]
        assert "2026-07-28" in discover["supportedVersions"] and discover["instructions"] == INSTRUCTIONS
        listed = server.request(2, "tools/list", {"_meta": MODERN_META})["result"]
        assert listed["resultType"] == "complete" and "ttlMs" in listed and listed["cacheScope"] in ("private", "public")
        assert [tool["name"] for tool in listed["tools"]] == TOOL_NAMES
        result = server.request(3, "tools/call", {"_meta": MODERN_META, "name": "list_accounts", "arguments": {}})["result"]
        assert result["resultType"] == "complete" and not result["isError"]
        for id, raw in ((4, "NaN"), (5, "1e999")):  # JSON text a model could send; json.dumps cannot
            text = json.dumps({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {
                "_meta": MODERN_META, "name": "list_accounts", "arguments": {"x": "RAW"}}}).replace('"RAW"', raw)
            refused = server.request(id, None, None, text=text)["result"]
            error = json.loads(refused["content"][0]["text"])["error"]
            assert refused["isError"] and (error["code"], error["message"]) == ("INVALID_ARGUMENT", "x: must be a finite number")


TRADE_TOOL_NAMES = ["buy", "sell", "place_orders", "place_ladder", "open_position", "place_trigger_entry",
                    "modify_order", "cancel_order",
                    "cancel_order_by_cloid", "cancel_orders", "close", "close_all_positions",
                    "stop_loss", "take_profit", "update_stop_loss", "update_take_profit", "sync_protection", "cancel_all_orders",
                    "set_leverage", "kill_switch"]


@pytest.mark.parametrize("args, env", [(["--enable-trading"], {}), ([], {"FRACTRADE_MCP_TRADING": "yes"})])
def test_with_the_trading_switch_the_trade_tools_are_offered(tmp_path, args, env):
    from fractrade_hl_simple.mcp_server.server import TRADING_INSTRUCTIONS
    with StdioServer(tmp_path, args, {"HL_KEY_MAIN": "0x" + "ab" * 32, **env}) as server:
        discover = server.request(1, "server/discover", {"_meta": MODERN_META})["result"]
        assert discover["instructions"] == TRADING_INSTRUCTIONS and len(TRADING_INSTRUCTIONS) < 1500
        listed = server.request(2, "tools/list", {"_meta": MODERN_META})["result"]
        assert [tool["name"] for tool in listed["tools"]] == TOOL_NAMES + TRADE_TOOL_NAMES
        result = server.request(3, "tools/call", {"_meta": MODERN_META, "name": "list_accounts", "arguments": {}})["result"]
        accounts = json.loads(result["content"][0]["text"])
        assert accounts["trading_tools"] is True
        assert {a["name"]: a["mode"] for a in accounts["accounts"]} == {"main": "trade", "whale": "watch"}


def test_without_the_switch_no_trade_tool_is_offered_and_no_key_is_read(tmp_path):
    # trading = true is not enough; the key is not read (reading this one would stop the server).
    with StdioServer(tmp_path, [], {"HL_KEY_MAIN": "not a key", "FRACTRADE_MCP_TRADING": "0"}) as server:
        listed = server.request(1, "tools/list", {"_meta": MODERN_META})["result"]
        assert [tool["name"] for tool in listed["tools"]] == TOOL_NAMES


def test_the_switch_without_a_trading_account_serves_read_tools_only(tmp_path):
    config = f'[accounts.whale]\naddress = "{WHALE}"\n'
    with StdioServer(tmp_path, ["--enable-trading"], config=config) as server:
        listed = server.request(1, "tools/list", {"_meta": MODERN_META})["result"]
        assert [tool["name"] for tool in listed["tools"]] == TOOL_NAMES
    assert "no account has trading = true: read only" in "".join(server.stderr)


def test_the_trading_instructions_say_which_account_a_trade_uses():
    # Reads default to default_account, trades never: an agent that read the default account
    # must not take an order without account for one on it.
    from fractrade_hl_simple.mcp_server.server import TRADING_INSTRUCTIONS
    assert "read tools use the default account" in INSTRUCTIONS
    assert "the only account that can trade (never the default account)" in " ".join(TRADING_INSTRUCTIONS.split())
