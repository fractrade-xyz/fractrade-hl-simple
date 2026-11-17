"""fractrade-hl-mcp: the MCP server for Hyperliquid.

Speaks MCP on stdin/stdout; logs go to stderr only, with anything that looks like a private
key redacted. ``--check`` validates the config and prints the accounts without starting the
server; it needs no ``mcp`` extra. ``--check --online`` also checks that each trading
account's key trades that account, with one request per account.
``--enable-trading`` (or ``FRACTRADE_MCP_TRADING=1``) registers the trade tools for the
accounts with ``trading = true``; without it no key is loaded.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..exceptions import HyperliquidException
from . import keys
from .config import AccountConfig, Config, ConfigError, load_config, load_trading_keys

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
EXTRA = ("mcp", "jsonschema")  # the packages of the [mcp] extra


class RedactingFormatter(logging.Formatter):
    """Formats a record, traceback included, and redacts private-key-like text."""

    def format(self, record: logging.LogRecord) -> str:
        return keys.redact(super().format(record))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fractrade-hl-mcp", description="MCP server for Hyperliquid (stdio).")
    parser.add_argument("--config", type=Path, default=None,
                        help="config file (default: FRACTRADE_MCP_CONFIG, then ~/.config/fractrade-hl/mcp.toml)")
    parser.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS,
                        default=(os.environ.get("FRACTRADE_MCP_LOG_LEVEL") or "WARNING").upper(),
                        help="log level on stderr (default: FRACTRADE_MCP_LOG_LEVEL, else WARNING)")
    parser.add_argument("--init", action="store_true",
                        help="create the config file and an empty key file to paste the API wallet's key "
                             "into (at --config, else the default place); never overwrites anything")
    parser.add_argument("--check", action="store_true",
                        help="validate the config, print the accounts to stderr and exit")
    parser.add_argument("--enable-trading", action="store_true",
                        help="register the trade tools for the accounts with trading = true (also "
                             "FRACTRADE_MCP_TRADING=1); without it the server reads only and loads no key")
    parser.add_argument("--online", action="store_true",
                        help="with --check and the trading switch: also check that each trading "
                             "account's key trades that account (one request per account)")
    return parser


def _setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def _say(text: str) -> None:
    sys.stderr.write(text + "\n")  # stdout is the protocol channel


def _setup(args: argparse.Namespace) -> int:
    """``--init``: the files to set the server up (setup.py)."""
    from .setup import SetupError, init

    try:
        lines = init(args.config, os.environ)
    except (SetupError, ConfigError) as error:
        _say(f"fractrade-hl-mcp: {error}")
        return 1
    except OSError as error:  # e.g. a folder that cannot be created; never a key in it
        _say(keys.redact(f"fractrade-hl-mcp: {error.strerror or error}: {error.filename or ''}"))
        return 1
    for line in lines:
        _say(line)
    return 0


def _trading_switch(args: argparse.Namespace) -> bool:
    """The global trading switch: --enable-trading or FRACTRADE_MCP_TRADING."""
    value = os.environ.get("FRACTRADE_MCP_TRADING", "").strip().lower()
    return bool(args.enable_trading) or value in ("1", "true", "yes")


def _trading_line(config: Config, switch: bool) -> str:
    accounts = [account.name for account in config.accounts.values() if account.trading]
    if not switch:
        return ("trading tools: off (start with --enable-trading or FRACTRADE_MCP_TRADING=1 to trade "
                f"{', '.join(accounts) if accounts else 'the accounts with trading = true'})")
    if not accounts:
        return "trading tools: off: the switch is on, but no account has trading = true"
    return f"trading tools: on for {', '.join(accounts)}"


def _limits(account: AccountConfig) -> list[tuple[str, object]]:
    """The account's limits that are set, guardrails included, in the order of the docs."""
    pairs = [("max_order_usd", account.max_order_usd), ("max_leverage", account.max_leverage),
             ("max_slippage", account.max_slippage), ("max_margin_pct", account.max_margin_pct),
             ("max_total_margin_pct", account.max_total_margin_pct), ("max_daily_loss_pct", account.max_daily_loss_pct),
             ("max_orders_per_hour", account.max_orders_per_hour), ("max_risk_pct", account.max_risk_pct)]
    shown: list[tuple[str, object]] = [(name, value) for name, value in pairs if value is not None]
    shown.extend((name, "true") for name, on in (("isolated_only", account.isolated_only),
                                                  ("check_order_leverage", account.check_order_leverage)) if on)
    return shown


def _number(value: object) -> str:
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def _state_folder(config: Config) -> Path:
    """``state`` next to the config file (the default place for a config from the environment)."""
    from .config import default_config_path

    source = Path(config.source) if config.source != "environment" else default_config_path(os.environ)
    if not source.is_absolute():  # not normalized: the system resolves '..' after links
        source = Path.cwd() / source
    return source.parent / "state"


def _summary(config: Config, switch: bool = False) -> str:
    lines = [f"config: {config.source}", f"default account: {config.default_account or '(none)'}"]
    for account in config.accounts.values():
        key = (f"key from ${account.key_env}" if account.key_env else
               f"key from {account.key_file}" if account.key_file else "no key")
        limits = "".join(f", {name} {_number(value)}" for name, value in _limits(account)) if account.trading else ""
        dexes = ", ".join(["default", *account.perp_dexs])
        lines.append(f"  {account.name}: {account.address} at {account.base_url}, "
                     f"{'trading configured' if account.trading else 'watch only'}, {key}{limits}, dexes: {dexes}")
    lines.append(_trading_line(config, switch))
    return "\n".join(lines)


def _check_signers(config: Config, trading_keys: dict[str, str]) -> int:
    """``--check --online``: the signer check of every trading account; 1 if any fails."""
    from ..hyperliquid import HyperliquidClient
    from .accounts import signer_address, signer_problem

    if not trading_keys:
        _say("signer check: no account has trading = true")
        return 0
    _say("signer check:")
    clients: dict[str, HyperliquidClient] = {}
    failed = False
    for name, key in trading_keys.items():
        account = config.accounts[name]
        signer = None

        def reader(base_url: str = account.base_url) -> HyperliquidClient:
            if base_url not in clients:  # made only for a lookup; it loads the market data first
                clients[base_url] = HyperliquidClient(base_url=base_url, trust_env=False)
            return clients[base_url]

        try:
            signer = signer_address(key)
            problem = signer_problem(account, signer, reader)
        except HyperliquidException as error:
            problem = f"the check could not be made: {error}"
        if problem is not None:
            failed = True
            _say(keys.redact(f"  {name}: {problem}"))
        elif account.vault:
            _say(f"  {name}: the key ({signer}) is an API wallet; each order names the vault or sub-account "
                 f"{account.address}, and the exchange accepts or rejects it")
        else:
            _say(f"  {name}: the key ({signer}) is an API wallet of {account.address}")
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    _setup_logging(args.log_level)
    if args.init:  # before the config is read: there may be none yet
        return _setup(args)
    try:
        config = load_config(args.config)
    except ConfigError as error:
        _say(f"fractrade-hl-mcp: {error}")
        return 1
    if args.online and not args.check:
        _say("fractrade-hl-mcp: --online works only with --check")
        return 1
    switch = _trading_switch(args)
    if args.check:
        _say(_summary(config, switch))
        if not switch:  # no key is read without the switch, here neither
            if args.online:
                _say("signer check: skipped, trading is off (add --enable-trading to check the keys)")
            return 0
        try:  # the keys the server would load: there, readable, safe (never shown)
            trading_keys = load_trading_keys(config)
        except ConfigError as error:
            _say(f"fractrade-hl-mcp: {error}")
            return 1
        return _check_signers(config, trading_keys) if args.online else 0

    if any(importlib.util.find_spec(package) is None for package in EXTRA):
        _say("fractrade-hl-mcp needs the mcp extra: pip install 'fractrade-hl-simple[mcp]'")
        return 1
    # With the extra installed, any import error here is a real one and shows as it is.
    import anyio

    from .accounts import AccountRegistry
    from .server import INSTRUCTIONS, TRADING_INSTRUCTIONS, build_server, make_client, serve_stdio
    from .tools.read import read_tools
    from .tools.trade import trade_tools

    # Without the switch no key is loaded: every client is one without a key.
    # With it, the keys of the trading accounts are read now, so a missing one stops the server.
    try:
        trading_keys = load_trading_keys(config) if switch else {}
    except ConfigError as error:
        _say(f"fractrade-hl-mcp: {error}")
        return 1
    if switch and not trading_keys:
        _say("fractrade-hl-mcp: the trading switch is on, but no account has trading = true: read only")
    # What every server of this config shares: account locks, halts, order times, cloids. A
    # read-only server only reads the halts (list_accounts), and creates nothing.
    from .state import SharedState
    state = SharedState(_state_folder(config))
    if trading_keys:
        from .setup import SetupError
        try:
            state.start()
        except (SetupError, OSError) as error:
            _say(keys.redact(f"fractrade-hl-mcp: the shared state in {state.folder} cannot be used: {error}"))
            return 1
    registry = AccountRegistry(config, trading_keys, make_client, state)
    tools = read_tools(config, registry) + (trade_tools(registry) if trading_keys else [])
    server = build_server(tools, TRADING_INSTRUCTIONS if trading_keys else INSTRUCTIONS)
    try:
        anyio.run(serve_stdio, server)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
