"""MCP server configuration: where it comes from, validation, resolved accounts.

No network access. Private keys are never part of the config; see keys.py.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..hyperliquid import _NETWORK_URLS, _NETWORKS
from . import keys

BASE_URLS = tuple(_NETWORKS)  # the API URLs the library accepts: in the config
# The API URLs a tool call may name: mainnet and testnet, never a local node over plain HTTP.
TOOL_BASE_URLS = tuple(_NETWORK_URLS.values())
DEFAULT_MAX_SLIPPAGE = Decimal("0.02")
ENV_ACCOUNT = "default"  # name of the account configured from environment variables
ENV_KEY_VAR = "HYPERLIQUID_PRIVATE_KEY"

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_TOP_LEVEL_KEYS = {"default_account", "defaults", "accounts"}
_DEFAULTS_KEYS = {"base_url", "max_slippage", "perp_dexs", "day_start", "tz"}
_ACCOUNT_KEYS = {
    "address", "key_env", "key_file", "base_url", "vault", "trading", "max_order_usd",
    "max_leverage", "max_slippage", "symbols", "perp_dexs",
    # Risk guardrails, each optional: a check runs only when its key is set.
    "max_margin_pct", "max_total_margin_pct", "max_daily_loss_pct", "isolated_only", "max_orders_per_hour",
    "check_order_leverage", "max_risk_pct",
    # Limits with a mode (off, warn: the order goes through with a warning, block): warn by default.
    "day_start", "tz", "max_day_loss_pct", "max_day_loss_mode", "max_total_notional_x", "max_total_notional_mode",
}
MODES = ("off", "warn", "block")
SIZE_LIMITS = ("max_order_usd", "max_margin_pct", "max_total_margin_pct")  # a trading account needs one


class ConfigError(ValueError):
    """The configuration is invalid. The message says where and why; anything in it that
    looks like a private key is redacted."""

    def __init__(self, message: str) -> None:
        super().__init__(keys.redact(message))


@dataclass(frozen=True)
class AccountConfig:
    name: str
    address: str
    base_url: str  # one of BASE_URLS, as HyperliquidClient(base_url=...) takes it
    vault: bool = False
    trading: bool = False
    key_env: Optional[str] = None
    key_file: Optional[Path] = None
    max_order_usd: Optional[Decimal] = None
    max_leverage: Optional[int] = None
    max_slippage: Decimal = DEFAULT_MAX_SLIPPAGE
    symbols: Optional[frozenset[str]] = None  # allowlist; None allows every symbol
    # The HIP-3 dexes read and traded besides Hyperliquid's own perps, as the library's
    # perp_dexs= takes them; none by default, as in the library.
    perp_dexs: tuple[str, ...] = ()
    # Risk guardrails (None / False: not checked). Percentages of the account value.
    max_margin_pct: Optional[Decimal] = None         # one new order's margin
    max_total_margin_pct: Optional[Decimal] = None   # used margin plus pending entries and the new order
    max_daily_loss_pct: Optional[Decimal] = None     # loss over the last 24 h, then no new risk
    isolated_only: bool = False                      # new risk only with isolated margin
    max_orders_per_hour: Optional[int] = None        # orders that add risk, per hour
    check_order_leverage: bool = False               # the market's leverage at most max_leverage, every order
    max_risk_pct: Optional[Decimal] = None           # a trade's loss at its stop losses (open_position)
    # The trading day, for max_day_loss_pct: it starts at day_start ("HH:MM") in the IANA time zone tz.
    day_start: str = "00:00"
    tz: str = "UTC"
    # Limits with a mode: off, warn (the order goes through, with a warning) or block.
    max_day_loss_pct: Optional[Decimal] = None       # loss since the trading day started, % of the value then
    max_day_loss_mode: str = "warn"
    max_total_notional_x: Optional[Decimal] = None   # positions and open orders that add risk, x the account value
    max_total_notional_mode: str = "warn"

    @property
    def has_key(self) -> bool:
        return self.key_env is not None or self.key_file is not None


@dataclass(frozen=True)
class Config:
    accounts: Mapping[str, AccountConfig]
    default_account: Optional[str]
    source: str  # the config file, or "environment"


def default_config_path(environ: Mapping[str, str]) -> Path:
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "fractrade-hl" / "mcp.toml"


def load_config(path: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None) -> Config:
    """Load the configuration. Sources, first match wins: `path` (``--config``), the file in
    ``FRACTRADE_MCP_CONFIG``, the default file (``$XDG_CONFIG_HOME/fractrade-hl/mcp.toml``),
    then a single account from environment variables."""
    environ = os.environ if environ is None else environ
    if path is None and environ.get("FRACTRADE_MCP_CONFIG"):
        path = Path(environ["FRACTRADE_MCP_CONFIG"])
    if path is None and _is_file(default_config_path(environ)):
        path = default_config_path(environ)
    if path is None:
        return config_from_environment(environ)
    path = path.expanduser().absolute()
    text = _decode(_read_config_file(path))
    if text is None:
        raise ConfigError(f"config file {path} is not valid UTF-8 text")
    return parse_config(text, source=str(path), folder=path.parent)


def _read_config_file(path: Path) -> bytes:
    """The config file's bytes, after checking the opened file itself: a regular file of the
    current user that no one else can write. Whoever can write it can point a key_file at
    another key or lift a limit. The owner and permissions are not checked on Windows, which
    has no such bits."""
    try:
        fd, problem = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)), None  # never block on a FIFO
    except OSError as e:
        fd, problem = -1, e.strerror
    if problem is not None:  # raised outside the except block: the OSError is not attached
        raise ConfigError(f"cannot read config file {path}: {problem}")
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"config file {path} is not a regular file")
        if sys.platform != "win32" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ConfigError(f"config file {path} can be changed by other users: it must be your own file, "
                              f"writable by you only (chmod 600 {path})")
        return f.read()


def parse_config(text: str, source: str, folder: Optional[Path] = None) -> Config:
    """Validate the text of a TOML config file. A relative key_file is read from `folder`
    (the config file's), not from the directory the server happens to start in."""
    # Checked on the raw text so that keys in comments are found too.
    if keys.contains_private_key(text):
        raise ConfigError(
            f"{source}: private keys must not be stored in the config file; use key_env or key_file"
        )
    try:
        data, parse_error = tomllib.loads(text), None
    except tomllib.TOMLDecodeError as e:
        data, parse_error = {}, str(e)
    if parse_error is not None:
        # Raised here, not in the except block, so the parser's exception is not attached
        # as __context__: its message can quote decoded names (e.g. a table declared
        # twice). ConfigError redacts the copy in its own message.
        raise ConfigError(f"{source}: {parse_error}")
    # Again after decoding: a TOML escape (\u0061) can split a key in the raw text.
    if _holds_private_key(data):
        raise ConfigError(
            f"{source}: private keys must not be stored in the config file; use key_env or key_file"
        )
    _check_keys(data, _TOP_LEVEL_KEYS, source)

    defaults = data.get("defaults", {})
    _check_table(defaults, f"{source} [defaults]")
    _check_keys(defaults, _DEFAULTS_KEYS, f"{source} [defaults]")

    tables = data.get("accounts", {})
    _check_table(tables, f"{source} [accounts]")
    if not tables:
        raise ConfigError(f"{source}: no accounts configured")
    accounts = {name: _account(name, table, defaults, f"{source} [accounts.{name}]", folder)
                for name, table in tables.items()}

    default_account = data.get("default_account")
    if default_account is not None and not isinstance(default_account, str):
        raise ConfigError(f"{source}: default_account must be an account name")
    if default_account is not None and default_account not in accounts:
        raise ConfigError(f"{source}: default_account {default_account!r} is not a configured account")
    return Config(MappingProxyType(accounts), default_account, source)


def config_from_environment(environ: Mapping[str, str]) -> Config:
    """One account named "default" from HYPERLIQUID_PUBLIC_ADDRESS (required),
    HYPERLIQUID_PRIVATE_KEY (optional; without it the account is watch-only), HYPERLIQUID_ENV
    ("mainnet" or "testnet", as for the library), HYPERLIQUID_IS_VAULT and
    FRACTRADE_MCP_PERP_DEXS (HIP-3 dexes, comma-separated). It can trade only if
    FRACTRADE_MCP_MAX_ORDER_USD and FRACTRADE_MCP_MAX_LEVERAGE are set too."""
    address = environ.get("HYPERLIQUID_PUBLIC_ADDRESS")
    if not address:
        raise ConfigError(
            f"no config file ({default_config_path(environ)}, --config or FRACTRADE_MCP_CONFIG) "
            "and HYPERLIQUID_PUBLIC_ADDRESS is not set"
        )
    network = (environ.get("HYPERLIQUID_ENV") or "mainnet").strip().lower()
    if network not in _NETWORK_URLS:
        raise ConfigError("environment: HYPERLIQUID_ENV must be mainnet or testnet")
    table: dict[str, Any] = {
        "address": address,
        "base_url": _NETWORK_URLS[network],
        "vault": environ.get("HYPERLIQUID_IS_VAULT", "").strip().lower() in ("1", "true", "yes"),
    }
    if environ.get(ENV_KEY_VAR):
        table["key_env"] = ENV_KEY_VAR
    if environ.get("FRACTRADE_MCP_PERP_DEXS", "").strip():  # comma-separated: xyz,para
        table["perp_dexs"] = [dex.strip() for dex in environ["FRACTRADE_MCP_PERP_DEXS"].split(",") if dex.strip()]
    max_order_usd = environ.get("FRACTRADE_MCP_MAX_ORDER_USD")
    max_leverage = environ.get("FRACTRADE_MCP_MAX_LEVERAGE")
    if "key_env" in table and max_order_usd and max_leverage:
        table["trading"] = True
        table["max_order_usd"] = _env_number("FRACTRADE_MCP_MAX_ORDER_USD", max_order_usd)
        table["max_leverage"] = _env_number("FRACTRADE_MCP_MAX_LEVERAGE", max_leverage)
    account = _account(ENV_ACCOUNT, table, {}, "environment")
    return Config(MappingProxyType({ENV_ACCOUNT: account}), ENV_ACCOUNT, "environment")


def load_trading_keys(config: Config, environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The private key of every trading account, read at startup so that a missing variable
    or an unsafe key file fails before the server runs."""
    environ = os.environ if environ is None else environ
    loaded = {}
    for account in config.accounts.values():
        if not account.trading:
            continue
        try:
            loaded[account.name] = keys.read_key(account.key_env, account.key_file, environ)
            continue
        except ValueError as e:
            problem = str(e)
        # Raised outside the except block, so the original error is not attached to it.
        raise ConfigError(f"account {account.name!r}: {problem}")
    return loaded


# ── validation helpers ──────────────────────────────────────────────


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:  # e.g. a name too long for the file system: no usable file there
        return False


def _decode(data: bytes) -> Optional[str]:
    """`data` as UTF-8 text, or None. The UnicodeDecodeError is not passed on: it holds the
    raw bytes."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _holds_private_key(value: Any) -> bool:
    """True if any key or value in the decoded TOML looks like a private key."""
    if isinstance(value, str):
        return keys.contains_private_key(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return keys.contains_private_key(str(value))  # e.g. an all-digit key typed as a number
    if isinstance(value, dict):
        return any(_holds_private_key(k) or _holds_private_key(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_holds_private_key(v) for v in value)
    return False


def _account(name: str, table: Any, defaults: Mapping[str, Any], where: str,
             folder: Optional[Path] = None) -> AccountConfig:
    if not _NAME.fullmatch(name):
        raise ConfigError(f"{where}: account names use a-z, 0-9, '-' and '_' (at most 32 characters)")
    _check_table(table, where)
    _check_keys(table, _ACCOUNT_KEYS, where)

    address = table.get("address")
    if not isinstance(address, str) or not ADDRESS.fullmatch(address):
        raise ConfigError(f"{where}: address must be 0x followed by 40 hex characters")
    if "key_env" in table and "key_file" in table:
        raise ConfigError(f"{where}: set key_env or key_file, not both")
    key_env = _optional(table, "key_env", str, where)
    key_file_text = _optional(table, "key_file", str, where)
    key_file = Path(key_file_text).expanduser() if key_file_text else None
    if key_file is not None and folder is not None and not key_file.is_absolute():
        key_file = folder / key_file

    base_url = table.get("base_url", defaults.get("base_url", _NETWORK_URLS["mainnet"]))
    if base_url not in BASE_URLS:
        raise ConfigError(f"{where}: base_url must be one of {', '.join(BASE_URLS)}")

    max_slippage = _decimal(table.get("max_slippage", defaults.get("max_slippage", DEFAULT_MAX_SLIPPAGE)),
                            "max_slippage", where)
    if not 0 < max_slippage <= Decimal("0.5"):
        raise ConfigError(f"{where}: max_slippage must be above 0 and at most 0.5")

    account = AccountConfig(
        name=name,
        address=address,
        base_url=base_url,
        vault=_optional(table, "vault", bool, where) or False,
        trading=_optional(table, "trading", bool, where) or False,
        key_env=key_env,
        key_file=key_file,
        max_order_usd=_positive(table, "max_order_usd", where),
        max_leverage=_leverage(table, where),
        max_slippage=max_slippage,
        symbols=_symbols(table, where),
        perp_dexs=_perp_dexs(table.get("perp_dexs", defaults.get("perp_dexs", [])), where),
        max_margin_pct=_percent(table, "max_margin_pct", where),
        max_total_margin_pct=_percent(table, "max_total_margin_pct", where),
        max_daily_loss_pct=_percent(table, "max_daily_loss_pct", where),
        isolated_only=_optional(table, "isolated_only", bool, where) or False,
        max_orders_per_hour=_whole(table, "max_orders_per_hour", where),
        check_order_leverage=_optional(table, "check_order_leverage", bool, where) or False,
        max_risk_pct=_percent(table, "max_risk_pct", where),
        day_start=_day_start(table.get("day_start", defaults.get("day_start", "00:00")), where),
        tz=_tz(table.get("tz", defaults.get("tz", "UTC")), where),
        max_day_loss_pct=_percent(table, "max_day_loss_pct", where),
        max_day_loss_mode=_mode(table, "max_day_loss_mode", "max_day_loss_pct", where),
        max_total_notional_x=_positive(table, "max_total_notional_x", where),
        max_total_notional_mode=_mode(table, "max_total_notional_mode", "max_total_notional_x", where),
    )
    if account.trading:
        missing = [field for field, value in (
            ("key_env or key_file", account.has_key),
            ("max_leverage", account.max_leverage is not None),
            (" or ".join(SIZE_LIMITS), any(getattr(account, limit) is not None for limit in SIZE_LIMITS)),
        ) if not value]
        if missing:
            raise ConfigError(f"{where}: trading = true needs {', '.join(missing)}")
    return account


def _check_table(value: Any, where: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: must be a table")


def _check_keys(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}")


def _optional(table: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
    value = table.get(key)
    if value is not None and not isinstance(value, kind):
        raise ConfigError(f"{where}: {key} must be a {kind.__name__}")
    return value


def _decimal(value: Any, key: str, where: str) -> Decimal:
    # A TOML number only: bool is an int in Python ("trading = true" in a number field must
    # not count), and a quoted string is not a number.
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ConfigError(f"{where}: {key} must be a number")
    number = Decimal(str(value))
    if not number.is_finite():
        raise ConfigError(f"{where}: {key} must be a finite number")
    return number


def _positive(table: Mapping[str, Any], key: str, where: str) -> Optional[Decimal]:
    if key not in table:
        return None
    number = _decimal(table[key], key, where)
    if number <= 0:
        raise ConfigError(f"{where}: {key} must be above 0")
    return number


def _percent(table: Mapping[str, Any], key: str, where: str) -> Optional[Decimal]:
    if key not in table:
        return None
    number = _decimal(table[key], key, where)
    if not 0 < number <= 100:
        raise ConfigError(f"{where}: {key} must be a percentage above 0 and at most 100")
    return number


def _day_start(value: Any, where: str) -> str:
    """'HH:MM', 00:00 to 23:59."""
    parts = value.split(":") if isinstance(value, str) else []
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts) \
            or int(parts[0]) > 23 or int(parts[1]) > 59:
        raise ConfigError(f"{where}: day_start must be a time of day as HH:MM, e.g. \"06:00\"")
    return str(value)


def _tz(value: Any, where: str) -> str:
    """An IANA time zone name, e.g. Europe/Athens."""
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ConfigError(f"{where}: tz must be an IANA time zone name, e.g. \"Europe/Athens\" or \"UTC\"") from None
    return str(value)


def _mode(table: Mapping[str, Any], key: str, limit: str, where: str) -> str:
    if key not in table:
        return "warn"
    if limit not in table:
        raise ConfigError(f"{where}: {key} needs {limit}")
    if table[key] not in MODES:
        raise ConfigError(f"{where}: {key} must be one of {', '.join(MODES)}")
    return str(table[key])


def _whole(table: Mapping[str, Any], key: str, where: str) -> Optional[int]:
    if key not in table:
        return None
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: {key} must be a whole number of at least 1")
    return value


def _leverage(table: Mapping[str, Any], where: str) -> Optional[int]:
    if "max_leverage" not in table:
        return None
    value = table["max_leverage"]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 50:
        raise ConfigError(f"{where}: max_leverage must be a whole number from 1 to 50")
    return value


def _symbols(table: Mapping[str, Any], where: str) -> Optional[frozenset[str]]:
    if "symbols" not in table:
        return None
    value = table["symbols"]
    if not isinstance(value, list) or not value or not all(isinstance(s, str) and s for s in value):
        raise ConfigError(f"{where}: symbols must be a non-empty list of symbol names")
    return frozenset(value)


def _perp_dexs(value: Any, where: str) -> tuple[str, ...]:
    """HIP-3 dex names, each once. Hyperliquid's own perps are always read, so they are not
    named here. Whether a dex exists on the network is checked when its client is made."""
    if not isinstance(value, list) or not all(isinstance(dex, str) and dex.strip() == dex and dex
                                              for dex in value):
        raise ConfigError(f"{where}: perp_dexs must be a list of HIP-3 dex names such as [\"xyz\", \"para\"]")
    return tuple(dict.fromkeys(value))


def _env_number(var: str, value: str) -> Any:
    """An environment variable as a TOML-like number, so it is validated like the file."""
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise ConfigError(f"environment: {var} must be a number") from None
    if not number.is_finite():
        raise ConfigError(f"environment: {var} must be a finite number")
    return int(number) if number == number.to_integral_value() else number
