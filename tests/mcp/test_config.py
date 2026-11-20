"""MCP server configuration and key handling."""

from decimal import Decimal
import sys
from pathlib import Path

import pytest

from hyperliquid.utils import constants

from fractrade_hl_simple.mcp_server.config import ConfigError, load_config, load_trading_keys, parse_config

MAIN = "0x" + "1" * 40
COPY = "0x" + "2" * 40
WHALE = "0x" + "3" * 40
KEY = "ab" * 32  # 64 hex characters


def spec_example(key_file: Path) -> str:
    """A config file with every kind of account."""
    return f'''
default_account = "main"

[defaults]
base_url = "https://api.hyperliquid.xyz"
max_slippage = 0.02

[accounts.main]
address = "{MAIN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 5000
max_leverage = 10
symbols = ["BTC", "ETH", "xyz:NVDA"]

[accounts.copy-1]
address = "{COPY}"
key_file = '{key_file}'
base_url = "https://api.hyperliquid-testnet.xyz"
trading = true
max_order_usd = 500
max_leverage = 5

[accounts.whale]
address = "{WHALE}"
'''


def key_file(tmp_path: Path, mode: int) -> Path:
    path = tmp_path / "copy-1"
    path.write_text(KEY + "\n")
    path.chmod(mode)
    return path


def test_spec_example_loads(tmp_path):
    config = parse_config(spec_example(key_file(tmp_path, 0o600)), "mcp.toml")
    main, copy, whale = (config.accounts[name] for name in ("main", "copy-1", "whale"))
    assert config.default_account == "main"
    assert (main.trading, main.max_order_usd, main.max_leverage) == (True, Decimal("5000"), 10)
    assert main.symbols == {"BTC", "ETH", "xyz:NVDA"}
    assert (main.base_url, main.max_slippage) == (constants.MAINNET_API_URL, Decimal("0.02"))  # from [defaults]
    assert copy.base_url == constants.TESTNET_API_URL
    assert not whale.trading and not whale.has_key  # watch-only


def test_trading_keys_are_read_at_startup(tmp_path):
    config = parse_config(spec_example(key_file(tmp_path, 0o600)), "mcp.toml")
    loaded = load_trading_keys(config, {"HL_KEY_MAIN": "0x" + KEY})
    assert loaded == {"main": "0x" + KEY, "copy-1": "0x" + KEY}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX permission bits")
def test_key_file_readable_by_others_is_refused(tmp_path):
    config = parse_config(spec_example(key_file(tmp_path, 0o644)), "mcp.toml")
    with pytest.raises(ConfigError, match="copy-1.*chmod 600"):
        load_trading_keys(config, {"HL_KEY_MAIN": "0x" + KEY})


@pytest.mark.parametrize("where", [
    pytest.param(f"# key: 0x{KEY}", id="comment"),
    # A TOML escape splits the key in the raw text; it is whole again after parsing.
    pytest.param(f'key_env = "{KEY[:32]}\\u00{ord(KEY[32]):02x}{KEY[33:]}"', id="escaped"),
])
def test_private_key_in_the_file_is_refused(where):
    text = f'[accounts.main]\naddress = "{MAIN}"\n{where}\n'
    with pytest.raises(ConfigError, match="private keys must not be stored in the config file"):
        parse_config(text, "mcp.toml")


def test_parse_error_does_not_show_an_escaped_key():
    # Declaring a table twice is a TOML error whose message quotes the decoded table name.
    escaped = f"{KEY[:32]}\\u00{ord(KEY[32]):02x}{KEY[33:]}"
    text = f'[accounts."{escaped}"]\n[accounts."{escaped}"]\n'
    with pytest.raises(ConfigError) as excinfo:
        parse_config(text, "mcp.toml")
    assert KEY not in str(excinfo.value) and excinfo.value.__context__ is None


@pytest.mark.parametrize(("account", "message"), [
    pytest.param(f'address = "{MAIN}"\nmax_order_usdd = 100', "unknown key max_order_usdd", id="typo"),
    pytest.param(f'address = "{MAIN[:-1]}"', "address must be 0x followed by 40 hex", id="short-address"),
    pytest.param(f'address = "{MAIN}"\nbase_url = "https://api.hyperliquid.xyz/"', "base_url must be one of",
                 id="base-url-trailing-slash"),
    pytest.param(f'address = "{MAIN}"\nnetwork = "mainnet"', "unknown key network", id="network-is-not-a-key"),
    pytest.param(f'address = "{MAIN}"\nkey_env = "A"\nkey_file = "/k"', "set key_env or key_file, not both",
                 id="two-key-sources"),
    pytest.param(f'address = "{MAIN}"\nkey_env = "A"\ntrading = true',
                 "trading = true needs max_leverage, max_order_usd or max_margin_pct or max_total_margin_pct",
                 id="trading-without-limits"),
    pytest.param(f'address = "{MAIN}"\nkey_env = "A"\ntrading = true\nmax_order_usd = 100\nmax_leverage = 100',
                 "max_leverage must be a whole number from 1 to 50", id="leverage-too-high"),
    # A bare string would otherwise be read as the dexes "x", "y", "z".
    pytest.param(f'address = "{MAIN}"\nperp_dexs = "xyz"', "perp_dexs must be a list", id="dexes-not-a-list"),
    pytest.param(f'address = "{MAIN}"\nperp_dexs = [""]', "perp_dexs must be a list", id="empty-dex-name"),
    # 0.05 for 5 % would allow 20 times less than meant; 500 is no limit.
    pytest.param(f'address = "{MAIN}"\nmax_margin_pct = 500', "max_margin_pct must be a percentage above 0 and at "
                 "most 100", id="percent-above-100"),
    pytest.param(f'address = "{MAIN}"\nmax_daily_loss_pct = 0', "max_daily_loss_pct must be a percentage",
                 id="percent-zero"),
    pytest.param(f'address = "{MAIN}"\nisolated_only = "yes"', "isolated_only must be", id="isolated-not-a-bool"),
    pytest.param(f'address = "{MAIN}"\nmax_orders_per_hour = 2.5', "max_orders_per_hour must be a whole number",
                 id="rate-not-whole"),
    pytest.param(f'address = "{MAIN}"\nmax_day_loss_mode = "block"', "max_day_loss_mode needs max_day_loss_pct",
                 id="mode-without-limit"),
    pytest.param(f'address = "{MAIN}"\nmax_total_notional_x = 3\nmax_total_notional_mode = "stop"',
                 "max_total_notional_mode must be one of off, warn, block", id="unknown-mode"),
    pytest.param(f'address = "{MAIN}"\nday_start = "6:00"', "day_start must be a time of day as HH:MM", id="day-start"),
    pytest.param(f'address = "{MAIN}"\ntz = "Athens"', "tz must be an IANA time zone name", id="time-zone"),
])
def test_invalid_account_is_refused_with_its_location(account, message):
    with pytest.raises(ConfigError, match=rf"mcp\.toml \[accounts\.main\]: {message}"):
        parse_config(f"[accounts.main]\n{account}\n", "mcp.toml")


def config_file(path: Path, text: str) -> Path:
    """A config file as --init makes it: only its owner can read and write it."""
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_config_file_that_is_not_text_is_refused_without_its_bytes(tmp_path):
    path = tmp_path / "mcp.toml"
    path.write_bytes(b"\xff\xfe" + KEY.encode())  # e.g. saved as UTF-16 by an editor
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="not valid UTF-8") as excinfo:
        load_config(path, {})
    assert KEY not in repr(excinfo.value) and excinfo.value.__context__ is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX permission bits")
def test_a_config_file_others_can_change_is_refused(tmp_path):
    # Whoever can write it can point key_file at another key or lift a limit.
    path = config_file(tmp_path / "mcp.toml", f'[accounts.main]\naddress = "{MAIN}"\n')
    path.chmod(0o664)
    with pytest.raises(ConfigError, match="can be changed by other users.*chmod 600"):
        load_config(path, {})


def test_a_relative_key_file_is_read_from_the_config_files_folder(tmp_path, monkeypatch):
    # The assistant starts the server in a directory of its own choosing.
    key_file(tmp_path, 0o600)
    path = config_file(tmp_path / "mcp.toml", f'''
[accounts.copy]
address = "{COPY}"
key_file = "copy-1"
trading = true
max_order_usd = 100
max_leverage = 5
''')
    monkeypatch.chdir(tmp_path / "..")
    assert load_trading_keys(load_config(path, {}), {}) == {"copy": "0x" + KEY}


def test_default_account_must_be_a_name():
    with pytest.raises(ConfigError, match="default_account must be an account name"):
        parse_config(f'default_account = ["main"]\n[accounts.main]\naddress = "{MAIN}"\n', "mcp.toml")


def test_without_a_file_one_account_comes_from_the_environment():
    environ = {
        "HYPERLIQUID_PUBLIC_ADDRESS": MAIN, "HYPERLIQUID_PRIVATE_KEY": "0x" + KEY,
        "FRACTRADE_MCP_MAX_ORDER_USD": "250", "FRACTRADE_MCP_MAX_LEVERAGE": "3",
        "XDG_CONFIG_HOME": "/nonexistent",
    }
    account = load_config(environ=environ).accounts["default"]
    assert (account.address, account.trading, account.max_order_usd, account.max_leverage) == (
        MAIN, True, Decimal("250"), 3)

    assert account.base_url == constants.MAINNET_API_URL

    del environ["HYPERLIQUID_PRIVATE_KEY"]
    watch_only = load_config(environ=environ).accounts["default"]
    assert not watch_only.trading and not watch_only.has_key

    environ["HYPERLIQUID_ENV"] = "testnet"  # as for the library
    assert load_config(environ=environ).accounts["default"].base_url == constants.TESTNET_API_URL
    environ["HYPERLIQUID_ENV"] = "devnet"
    with pytest.raises(ConfigError, match="HYPERLIQUID_ENV must be mainnet or testnet"):
        load_config(environ=environ)


def test_perp_dexs_from_the_environment_are_comma_separated():
    environ = {"HYPERLIQUID_PUBLIC_ADDRESS": MAIN, "FRACTRADE_MCP_PERP_DEXS": " xyz, para,", "XDG_CONFIG_HOME": "/nonexistent"}
    assert load_config(environ=environ).accounts["default"].perp_dexs == ("xyz", "para")


def test_config_sources_first_match_wins(tmp_path):
    def write(name: str, address: str) -> Path:
        return config_file(tmp_path / name, f'[accounts.main]\naddress = "{address}"\n')

    xdg = tmp_path / "xdg"
    (xdg / "fractrade-hl").mkdir(parents=True)
    write("xdg/fractrade-hl/mcp.toml", WHALE)
    environ = {"XDG_CONFIG_HOME": str(xdg), "FRACTRADE_MCP_CONFIG": str(write("env.toml", COPY))}
    explicit = write("explicit.toml", MAIN)

    assert load_config(explicit, environ).accounts["main"].address == MAIN  # --config
    assert load_config(None, environ).accounts["main"].address == COPY  # FRACTRADE_MCP_CONFIG
    del environ["FRACTRADE_MCP_CONFIG"]
    assert load_config(None, environ).accounts["main"].address == WHALE  # default file


def test_the_example_config_loads():
    # The example users copy must stay valid as the validation changes.
    example = Path(__file__).parents[2] / "examples" / "mcp.example.toml"
    config = parse_config(example.read_text(), str(example))  # its permissions are the checkout's
    assert config.accounts["main"].trading and config.accounts["vault"].vault
    assert config.accounts["test"].perp_dexs == () and config.accounts["whale"].perp_dexs == ("xyz",)


@pytest.mark.parametrize("limit", ["max_order_usd = 100", "max_margin_pct = 5", "max_total_margin_pct = 80"])
def test_any_one_size_limit_lets_an_account_trade(limit):
    account = parse_config(f'[accounts.main]\naddress = "{MAIN}"\nkey_env = "A"\ntrading = true\n'
                           f'max_leverage = 20\n{limit}\n', "mcp.toml").accounts["main"]
    assert account.trading


