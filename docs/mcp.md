# MCP server: Hyperliquid in Claude Code, Codex and Claude Desktop

`fractrade-hl-mcp` is a local [MCP](https://modelcontextprotocol.io) server. It lets an AI
assistant read your Hyperliquid accounts and the markets: balances, positions, orders,
fills, funding and prices, for several wallets at once and for any public address, on
Hyperliquid's own perps and on the HIP-3 dexes you enable, such as `xyz:` (stocks, indices,
commodities, forex), `para:` or `io:`.

**Read only by default.** Without the trading switch (`--enable-trading`, or
`FRACTRADE_MCP_TRADING=1` in the server's environment) it loads no private key and has no
tool that places, changes or cancels an order. With the switch and an account with
`trading = true`, it also offers trade tools that trade real money, within limits you set
per account ([Trading](#trading)).

**Setting it up for the first time?** The [step-by-step guide](mcp-setup.md) walks you
through it; this page is the full reference.

- Runs on your computer, over stdio; nothing is hosted.
- Works with Claude Code, Codex (the CLI and the app) and Claude Desktop. It speaks both generations of the
  MCP protocol: the `initialize` handshake (versions 2024-11-05 to 2025-11-25; Codex CLI
  uses it) and the stateless 2026-07-28 (Claude Code uses it). The tests cover both.
- Built on this library, so it reads exactly what the library reads, with the same
  documented errors.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Connect a client](#connect-a-client)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Accounts, addresses and networks](#accounts-addresses-and-networks)
- [Tools](#tools)
- [Trading](#trading)
- [What a tool returns](#what-a-tool-returns)
- [Errors](#errors)
- [Balances and account modes](#balances-and-account-modes)
- [Symbols and markets](#symbols-and-markets)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Limits](#limits)

## Install

Python 3.12 or newer. The server is an optional extra of the library; a plain
`pip install fractrade-hl-simple` stays without it.

```bash
pipx install "fractrade-hl-simple[mcp]"     # or pip install, in a virtual environment
```

This adds the `fractrade-hl-mcp` command (also `python -m fractrade_hl_simple.mcp_server`).
pipx keeps it apart from the system's Python, which on many Linux and macOS systems refuses
a plain `pip install` ([setup guide](mcp-setup.md#2-install)).
Until a release with the server is on PyPI, install from a checkout of this repository:

```bash
pip install -e ".[mcp]"        # or: poetry install --extras mcp
```

Without the extra, `fractrade-hl-mcp` says how to install it instead of starting.

## Quick start

1. Write the config file `~/.config/fractrade-hl/mcp.toml` with the public addresses of the
   wallets you want to read:

   ```toml
   default_account = "main"

   [accounts.main]
   address = "0xYourMainAddress"

   [accounts.bot]
   address = "0xYourBotAddress"
   ```

2. Check it:

   ```bash
   fractrade-hl-mcp --check
   ```

3. Add the server to your client (next section) and ask, for example, "show my
   positions", "what is the funding on xyz:NVDA?" or "list the fills of account bot this
   week".

## Connect a client

The client starts the server itself; you do not run it in a terminal. If the client cannot
find the command, give its full path (`which fractrade-hl-mcp`).

### Claude Code

```bash
claude mcp add --scope user fractrade -- fractrade-hl-mcp
```

With a config file somewhere else:

```bash
claude mcp add --scope user fractrade -- fractrade-hl-mcp --config /path/to/mcp.toml
```

`--scope user` makes it available in every project; leave it out for the current project only.

### Codex CLI and the Codex app

```bash
codex mcp add fractrade -- fractrade-hl-mcp
```

or in `~/.codex/config.toml`, which the Codex app uses too:

```toml
[mcp_servers.fractrade]
command = "fractrade-hl-mcp"
args = ["--config", "/home/you/.config/fractrade-hl/mcp.toml"]
```

### Claude Desktop

In `claude_desktop_config.json` (Settings, Developer, Edit Config):

```json
{
  "mcpServers": {
    "fractrade": {
      "command": "/full/path/to/fractrade-hl-mcp",
      "args": ["--config", "/full/path/to/mcp.toml"]
    }
  }
}
```

### Approvals

The read tools are marked read-only for the client, and you can let it run them without
asking (`enable_perp_dexs` is not marked read-only: it changes what the server reads and
trades, [below](#enable_perp_dexs)). With the trading switch on, the trade tools are not read-only: keep the client's
approval prompt for them, so you confirm every order before it is sent. Approve them without
asking only if you accept that the agent trades within the account's limits on its own.

## Configuration

### Where it comes from

In this order:

1. `--config PATH`
2. the file named by the environment variable `FRACTRADE_MCP_CONFIG`
3. `$XDG_CONFIG_HOME/fractrade-hl/mcp.toml`, by default `~/.config/fractrade-hl/mcp.toml`,
   if it exists
4. else one account named `default` from environment variables
   ([below](#without-a-config-file))

A file named by `--config` or `FRACTRADE_MCP_CONFIG` must exist and be valid: the server
never falls back to another source.

The server never reads a `.env` file.

### The file

TOML. Every key is checked: an unknown key (a typo) stops the server with its place in the
file. A complete, commented example: [`examples/mcp.example.toml`](../examples/mcp.example.toml).

```toml
default_account = "main"           # optional: used when a tool names no account

[defaults]                         # optional, inherited by every account
base_url = "https://api.hyperliquid.xyz"
max_slippage = 0.02
perp_dexs = ["xyz", "para", "io", "mkts"]   # HIP-3 dexes; none when absent

[accounts.main]
address = "0x..."                  # the account's address (main wallet, vault or sub-account)

[accounts.copy-1]
address = "0x..."
base_url = "https://api.hyperliquid-testnet.xyz"
perp_dexs = ["xyz"]                # testnet has other dexes: para and io are not there

[accounts.whale]
address = "0x..."                  # any wallet you want to watch
```

| Key | Where | Meaning |
|---|---|---|
| `default_account` | top | The account a tool uses when it names none. Without it, the only account; with several and no default, a tool must name one. |
| `base_url` | `[defaults]` or account | The Hyperliquid API: `https://api.hyperliquid.xyz` (mainnet, the default) or `https://api.hyperliquid-testnet.xyz` (testnet); `http://localhost:3001` for a local node. Any other value is refused: the SDK would sign for the wrong network with it. |
| `address` | account | Required. `0x` and 40 hex characters. |
| `vault` | account | `true` if the address is a vault or sub-account. |
| `perp_dexs` | `[defaults]` or account | The HIP-3 dexes read and traded besides Hyperliquid's own perps, by the prefix of their markets: `["xyz", "para"]` for `xyz:NVDA` and `para:TOTAL2`. None when absent, as in the library. An account's list replaces the one in `[defaults]`. See [HIP-3 dexes](#hip-3-dexes). |
| `trading`, `key_env`, `key_file`, `max_order_usd`, `max_leverage`, `max_slippage`, `symbols`, and the risk guardrails `max_margin_pct`, `max_total_margin_pct`, `max_daily_loss_pct`, `max_orders_per_hour`, `check_order_leverage`, `isolated_only`, `max_risk_pct` | account | Trading settings, checked always (below; the guardrails in [Risk guardrails](#risk-guardrails)). Used only with the trading switch (`--enable-trading` or `FRACTRADE_MCP_TRADING=1`): then the key of each account with `trading = true` is read at startup and its trade tools are offered. Without the switch no key is read. |

At least one account is required, and `default_account` must name one of them. Account
names start with a letter or digit and use `a-z`, `0-9`, `-` and `_`, at most 32
characters. `vault` and `trading` are booleans (default `false`). The trading settings are
checked even while trading is off: `trading = true` needs `key_env` or `key_file` (not
both), `max_leverage`, and at least one size limit: `max_margin_pct`,
`max_total_margin_pct` or `max_order_usd`. `max_order_usd` above 0,
`max_leverage` a whole number from 1 to 50, `max_slippage` above 0 and at most 0.5
(default 0.02), the `_pct` keys percentages above 0 and at most 100 (5 is 5 %, not 0.05),
`check_order_leverage` and `isolated_only` booleans, `max_orders_per_hour` a whole number of
at least 1, `symbols`
a non-empty list of names, `perp_dexs` a list of dex names. A
dex the network does not have is refused when its first tool call reads that network.
Numbers must be TOML numbers, not strings. **A private key in the file stops the server**: anything that looks like one (64
or more hex characters), in a value or a comment.

### Without a config file

Without a file, one account named `default` comes from the environment of the server
process (pass them with the client: `claude mcp add -e`, `codex mcp add --env`):

| Variable | Meaning |
|---|---|
| `HYPERLIQUID_PUBLIC_ADDRESS` | Required: the account's address. |
| `HYPERLIQUID_ENV` | `mainnet` (default) or `testnet`. |
| `HYPERLIQUID_IS_VAULT` | `true` (or `1`, `yes`) for a vault or sub-account. |
| `FRACTRADE_MCP_PERP_DEXS` | HIP-3 dexes, comma-separated: `xyz,para`. |
| `HYPERLIQUID_PRIVATE_KEY`, `FRACTRADE_MCP_MAX_ORDER_USD`, `FRACTRADE_MCP_MAX_LEVERAGE` | With all three set, the account can trade: the limits are checked always (an invalid one stops the server), and the key is read and used only with the trading switch. |
| `FRACTRADE_MCP_TRADING` | `1` (or `true`, `yes`): the trading switch, as `--enable-trading`. |

### Check a config

```bash
fractrade-hl-mcp --check
```

prints the accounts (name, address, API, whether trading is configured, the dexes) and
whether trading would be on, and for which accounts, to stderr, and exits with 0, or with
1 and the reason. It makes no network request and needs no `mcp` extra. With
`--enable-trading` it also reads the trading accounts' keys (never shown) as the server
would; with `--online` it checks them with Hyperliquid ([Command line](#command-line)).

## Command line

```
fractrade-hl-mcp [--config PATH] [--log-level LEVEL] [--init | [--enable-trading] [--check [--online]]]
```

| Flag | Meaning |
|---|---|
| `--config PATH` | The config file (else the sources above). |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING` (default), `ERROR` or `CRITICAL`; also `FRACTRADE_MCP_LOG_LEVEL`. |
| `--init` | Create the config file, the folder for the key files and an empty key file (`keys/main`) to paste the API wallet's key into, where none is yet, for your user only, and print where they are; never overwrites anything, never through a symbolic link ([Switch it on](#switch-it-on)). |
| `--enable-trading` | The trading switch (also `FRACTRADE_MCP_TRADING=1`): read the keys of the accounts with `trading = true` and offer the trade tools for them ([Trading](#trading)). With `--check`: check those keys as the server would. |
| `--check` | Validate the config, print the accounts, exit. |
| `--online` | With `--check` and `--enable-trading` (without the switch it reads no key and says the check is skipped): also check, for every account with `trading = true`, that its key is an API wallet the account approved (one `userRole` request per key, after the client has loaded Hyperliquid's market data); for a vault or sub-account, that it is an API wallet. Exits with 1 if one is not: the account's own key, or an API wallet that expired, was removed or belongs to another account. |

Logs go to stderr only (stdout carries the protocol). Anything that looks like a private key
is redacted in every log line, tracebacks included. The `--check` summary shows addresses
and key sources (variable names, file paths), never key values. Command line errors are
printed by Python's argparse as they are.

## Accounts, addresses and networks

Every tool about one wallet takes one of:

| Parameter | Meaning |
|---|---|
| `account` | A configured account name (see `list_accounts`). |
| `address` | Any public address, also one that is not configured. |
| `base_url` | Only with `address`: which Hyperliquid API to read (default mainnet). A configured account's API comes from the config. |

Without `account` and `address`, the default account is used, or the only account if there
is just one. The market tools take only `base_url` (default mainnet). Trade tools act on
configured accounts only and never on the default account by default
([Trading](#which-account-a-trade-acts-on)).

In the wallet tools, a `symbol` that matches nothing is checked against the markets
(delisted ones included): a market the wallet holds nothing in gives an empty list, a
symbol that is no market is `UNKNOWN_SYMBOL`, with the market of that name on another dex
suggested (`NVDA`: did you mean `xyz:NVDA`?). Before refusing, the current markets are read
(two requests per dex), so a market listed since the server started is found; if they
cannot be read, the error is that failure (e.g. `NETWORK_ERROR`), not `UNKNOWN_SYMBOL`. A
symbol that matches records is never refused. Fills and funding payments cover every HIP-3
dex, so there a symbol of a dex that is not enabled (such as `flx:GOLD`) is accepted;
`get_positions`, `get_open_orders` and the market tools refuse it, naming the dex and
`enable_perp_dexs`.

Every answer says which wallet and API it is about: `account` (the name, or null for an
address), `address` and `base_url`.

The server reads wallets without their keys: one client per API reads every wallet,
passing its address. A watched address can never be traded through it.

## Tools

| Tool | Reads |
|---|---|
| [`list_accounts`](#list_accounts) | the configured accounts |
| [`enable_perp_dexs`](#enable_perp_dexs) | (changes server state) enables HIP-3 dexes for reading and trading until the server restarts |
| [`get_account`](#get_account) | a wallet's balances, by account mode, with positions |
| [`get_positions`](#get_positions) | open perp positions |
| [`get_open_orders`](#get_open_orders) | open orders, stop loss and take profit included |
| [`get_fills`](#get_fills) | executed trades, perp or spot |
| [`get_order_status`](#get_order_status) | one order by its id |
| [`get_order_status_by_cloid`](#get_order_status_by_cloid) | one order by its client order id |
| [`get_portfolio`](#get_portfolio) | account value, PnL and volume per period |
| [`get_funding_payments`](#get_funding_payments) | funding the wallet paid or received |
| [`get_prices`](#get_prices) | mid and mark prices, perp or spot |
| [`list_markets`](#list_markets) | the open markets, perp or spot |
| [`get_market_info`](#get_market_info) | everything about one market |
| [`get_order_book`](#get_order_book) | bids and asks of a market |
| [`get_funding_rates`](#get_funding_rates) | current funding rates |
| [`get_position_risk`](#get_position_risk) | an open position's outcomes at its stops and take profits, risk/reward |
| [`plan_trade`](#plan_trade) | a planned trade's outcomes and its size from a risk |

Arguments are checked exactly against each tool's schema: a misspelled or unknown parameter,
a number sent as a string or `true` as a number is refused with `INVALID_ARGUMENT` naming
the parameter. Nothing is converted or ignored. In particular:

- `limit`, `top`, `depth` and `order_id` are JSON integers (`123`, not `"123"` or `123.0`).
- `symbol` and `query` are non-empty; `symbols` holds 1 to 50 different symbols.
- `market` is `perp` (the default) or `spot`; with `spot`, symbols are spot symbols
  ([below](#symbols-and-markets)) and `dex` is refused (it filters perp dexes).
- `threshold` is a number of at least 0.
- Times are ISO 8601; without an offset they are UTC. A range includes both ends, and
  `end` must not be before `start`.

### `list_accounts`

The configured accounts. No parameters.

Returns `default_account` (as configured, so null if the file sets none), `trading_tools`
(true when the trade tools are offered) and `accounts`, directly, without `data` or `count`.
Each account: `name`, `address`, `base_url`, `mode` (`watch`: read only; `trade`: the trade
tools act on it),
`halted`, `limits`: null unless `trading = true`, else `max_order_usd`,
`max_leverage`, `max_slippage`, `symbols` (sorted, or null) and the risk guardrails
(`max_margin_pct`, `max_total_margin_pct`, `max_daily_loss_pct`, `max_orders_per_hour`,
`max_risk_pct`: null when not set; `check_order_leverage`, `isolated_only`), and `perp_dexs`: the HIP-3
dexes of the account, its configured ones and those enabled since the server started.

### `enable_perp_dexs`

Enables HIP-3 dexes at one network until the server restarts: from then on they are read,
and traded by the accounts that can trade, like the configured ones, for every account
there (each account's limits, its `symbols` allowlist included, still apply). The tool
changes server state, so it is not marked read-only. Use it when you ask about markets of a
dex that is not configured; to keep it, add it to `perp_dexs` in the config. It sends
nothing to Hyperliquid but reads: the markets of the dexes (about 14 requests for four
dexes, a few seconds).

| Parameter | Default | Meaning |
|---|---|---|
| `dexes` | required | 1 to 20 dex names, as the prefix of their markets: `["io"]` |
| `base_url` | mainnet | |

Returns `base_url`, `perp_dexs` (every HIP-3 dex now read there), `enabled` (those this call
added) and a `note`. A name the network does not have is `INVALID_ARGUMENT`, nothing is
enabled, and the hint lists the network's dexes. Enabling a dex again changes nothing.

### `get_account`

A wallet's balances by its account mode ([below](#balances-and-account-modes)). Parameters:
the [wallet parameters](#accounts-addresses-and-networks).

Returns:

| Field | Meaning |
|---|---|
| `mode` | `unified`, `standard`, `portfolio_margin` or `unknown` |
| `account_value` | USD, PnL of open positions included; null where not established |
| `available_to_trade` | USDC free to back new orders (unified accounts); null otherwise |
| `margin_used` | margin of the dexes read |
| `sources` | how each figure was computed, which dexes were read, which tokens are unpriced |
| `balances` | every token with a balance (zero balances are left out): `token`, `market` (`spot` or `outcome`), `total`, `hold`, `available` (total minus hold), `price`, `usd_value` (null if unpriced) |
| `dexes` | each dex read (`null` = Hyperliquid's own perps, then the enabled HIP-3 dexes): `account_value` and `withdrawable` (standard accounts only), `margin_used`, `cross_maintenance_margin_used`, `positions` (count) |
| `positions` | the open positions, as in `get_positions` |
| `open_orders` | the number of open perp orders |

### `get_positions`

Open perp positions on Hyperliquid's own perps and the enabled HIP-3 dexes. Parameters: the wallet parameters,
`symbol` (only this market).

Each position: `symbol`, `dex`, `side` (`long`/`short`), `size` (base units), `entry_price`,
`notional`, `unrealized_pnl`, `return_on_equity`, `leverage`, `margin_mode` (`cross`/
`isolated`), `liquidation_price`, `margin_used`, `funding_since_open`.

### `get_open_orders`

Open orders, triggers (stop loss, take profit) included. Parameters: the wallet parameters,
`symbol`, `market` (`perp`, the default, every dex read; or `spot`, where `symbol` is a
token such as `HYPE`).

Each order: `order_id`, `cloid` (the client order id it was placed with, `0x` and 32 hex
digits, or null), `symbol`, `side` (`buy`/`sell`), `type` (`limit`, `stop_loss`,
`take_profit`, ...), `size` (as placed), `filled_size`, `remaining_size`, `limit_price`,
`trigger_price` (null for orders without a trigger), `trigger_condition`, `reduce_only`,
`is_position_tpsl`, `time_in_force`, `created_at`.

### `get_fills`

Fills (executed trades), newest first: perp fills of every dex, or with `market: "spot"` the
spot fills, under the same symbols as spot open orders (`FRAC`, `HYPE/USDT0`); a pair that is
no longer listed keeps Hyperliquid's name (`@98`).

| Parameter | Default | Meaning |
|---|---|---|
| wallet parameters | | |
| `symbol` | | only this market |
| `market` | `perp` | or `spot` |
| `start` | | ISO 8601 time (UTC if no offset). Without it: from the 2000 most recent fills |
| `end` | now | only with `start` |
| `limit` | 50 | at most 500 |

Each fill: `time`, `symbol`, `side`, `direction` (e.g. `Open Long`), `price`, `size`,
`closed_pnl`, `fee`, `fee_token`, `taker` (true: the fill took liquidity), `order_id`,
`trade_id`, `start_position` (the position size before the fill).

`truncated` is true if more fills match than were returned, or if the list may be missing
some; `truncated_reason` says which:

| Reason | Meaning |
|---|---|
| `limit` | more fills match than `limit` |
| `recent_fills` | without `start`, only the 2000 most recent fills are read, and there were 2000: older ones may exist; pass `start` |
| `retention_window` | Hyperliquid keeps only an account's 10000 most recent fills, and it cannot be ruled out that fills of the range are gone |
| `same_millisecond_overflow` | 2000 or more fills share one millisecond (one full response); the rest of the range cannot be read |

These reasons say the list may be incomplete, not that fills are certainly missing. With
`start`, completeness is checked with extra requests (one or two, more when many fills came
after the range). The check assumes Hyperliquid answers consistently and its clock is within
30 days of yours; a fill Hyperliquid records late, behind a range already read, cannot be
ruled out by any client.

### `get_order_status`

One order by its id, open or not. Parameters: the wallet parameters, `order_id` (required,
an integer, as the other tools return it).

Returns `found`; if false (the wallet has no such order), `data` is just `{"found":false}`.
Else `status` (`open`, `filled`, `canceled`, `triggered`, `rejected`, ...), `status_time`,
and `order`: the fields and values of [`get_open_orders`](#get_open_orders) (`type` is
`limit`, `stop_loss` or `take_profit`, as there, and its `cloid`). Its fill prices are not
part of the status: [`get_fills`](#get_fills) has them.

### `get_order_status_by_cloid`

The same, by the client order id the order was placed with. Parameters: the wallet
parameters, `cloid` (required, `0x` and 32 hex digits). The tool to use after
`OUTCOME_UNKNOWN`: `found: false` means the order is not known yet, which is no permission
to place it again (it can still arrive).

### `get_portfolio`

Performance per period. Parameters: the wallet parameters.

For each of `day`, `week`, `month`, `all_time`, and `perp_day`, `perp_week`, `perp_month`,
`perp_all_time` (perps only): `account_value` at the end, `pnl` over the period, `volume`,
`from` and `to`.

### `get_funding_payments`

Funding the wallet paid or received, newest first, every dex.

| Parameter | Default | Meaning |
|---|---|---|
| wallet parameters | | |
| `symbol` | | only this market |
| `start` | 7 days before `end` | ISO 8601 time |
| `end` | now | |
| `limit` | 100 | at most 500; `total_usdc` covers the whole range |

A range may be at most 31 days: every hour adds one payment per open position. If 500 or
more payments share one timestamp (one full response), the rest cannot be read: the call
fails with `HYPERLIQUID_ERROR` instead of returning a partial list and total.

Returns `from`, `to`, `total_usdc` (positive: received, negative: paid) and each payment:
`time`, `symbol`, `position_size` (negative: short), `funding_rate` (hourly), `usdc`.

### `get_prices`

Current mid and mark prices, of perp markets or spot pairs.

| Parameter | Default | Meaning |
|---|---|---|
| `symbols` | | these markets (1 to 50) |
| `dex` | | perp only: `default` (Hyperliquid's own perps) or an enabled HIP-3 dex (`xyz`); with no `symbols`, the most traded markets of it |
| `market` | `perp` | or `spot`: the spot pairs given, or without `symbols` the most traded |
| `limit` | 50 | at most 200; only without `symbols` |
| `base_url` | mainnet | |

For perp markets, at least one of `symbols` and `dex` is required: the tool never lists
every market (see `list_markets`). `symbols` come back in the order given; with `dex` too,
only those of that dex. A dex that is not enabled is `INVALID_ARGUMENT`, with the enabled
ones in the hint. Delisted perp markets are left out. Each: `symbol`,
`mid_price`, `mark_price`; spot prices are in the pair's quote token.

### `list_markets`

The open perp markets of Hyperliquid's own perps and the enabled HIP-3 dexes, or with
`market: "spot"` the spot pairs, most traded first; delisted perp markets are left out.

| Parameter | Default | Meaning |
|---|---|---|
| `dex` | | perp only: `default` or an enabled HIP-3 dex |
| `market` | `perp` | or `spot` |
| `query` | | only symbols containing this text (case-insensitive) |
| `limit` | 50 | at most 200 |
| `base_url` | mainnet | |

Each: `symbol`, `dex`, `max_leverage`, `margin_mode` (`cross`, or `no_cross`/
`strict_isolated` for isolated-only markets, or `unknown`), `size_decimals`, `mark_price`,
`funding_hourly`, `open_interest` (base units), `day_volume_usd`, `at_open_interest_cap`.

Each spot pair: `symbol`, `base` and `quote` token, `size_decimals`, `mark_price`, `mid_price`
and `day_volume`, the last three in the quote token (USDC, USDH, USDT0 or USDE).

### `get_market_info`

Everything about one perp market. Parameters: `symbol` (required), `base_url`.

The `list_markets` fields, plus `delisted`, `mid_price`, `oracle_price`,
`previous_day_price`, `change_24h_percent`, `premium`, and for `xyz:` markets a `note` on
prices outside the oracle's schedule ([below](#symbols-and-markets)).

### `get_order_book`

| Parameter | Default | Meaning |
|---|---|---|
| `symbol` | required | |
| `depth` | 10 | levels per side, 1 to 20 |
| `market` | `perp` | or `spot` (`symbol` is then a token, e.g. `HYPE`) |
| `base_url` | mainnet | |

Returns `symbol`, `time`, `best_bid`, `best_ask`, `mid_price`, `spread`, `spread_bps`, and
`bids` and `asks`: each level's `price`, `size` (base units) and `orders`.

### `get_funding_rates`

Current funding rates, largest first by size; delisted markets are left out.

| Parameter | Default | Meaning |
|---|---|---|
| `symbols` | | these markets (1 to 50); else every open market |
| `top` | 20 | at most 200 |
| `threshold` | | only rates at least this large (hourly, absolute), e.g. `0.0001` |
| `base_url` | mainnet | |

Each: `symbol`, `funding_hourly`, `funding_annualized` (both fractions: `0.0001` = 0.01%;
positive: longs pay shorts), `mark_price`. Markets without a funding rate are left out.

### `get_position_risk`

What an open perp position makes or loses at its stop losses and take profits. Parameters:
the wallet parameters, `symbol` (required). Read only. No position: `POSITION_NOT_FOUND`.

The protections are the position's reduce-only open orders on the closing side: stop losses
and take profits at their trigger price, a resting limit order as a take profit at its
price. A TP/SL set for the entire position (in the Hyperliquid app) counts for the whole
position, as it closes all of it when it triggers. In the order the price reaches them, each
counts up to what is left of the position (Hyperliquid fills a reduce-only order no further).

Returns `side`, `size`, `entry_price`, `mid_price`, `liquidation_price`, `unrealized_pnl`,
`leverage`, `margin_mode`, and:

- `stop_losses`, `take_profits`: per level `price`, `size`, `order_id`, `executes_as`
  (`market (taker)`, `limit (maker)`, or for a stop limit the limit it fills at, at worst),
  `gross_pnl` (from the entry price, at the level's
  price), `fee`, `slippage` (estimated, what the execution costs against the level's price;
  negative if better; null if not estimable: larger than the visible order book) and
  `net_pnl` (from the executed price);
- `if_all_stops`, `if_all_take_profits`: `size`, `net_pnl` and `percent_of_account` if every
  stop, or every take profit, fills (null without any);
- `risk_reward` (the take profits' net gain over the stops' net loss),
  `break_even_win_rate_percent`, `unprotected_size` (the size no stop covers);
- `fees`: the `taker` and `maker` rates used (the wallet's, from Hyperliquid; on a HIP-3 dex
  scaled by its growth mode and fee scale);
- `warnings`: no stop loss; size without a stop, with what it loses at the liquidation
  price; a stop beyond the liquidation price; a stop or take profit the market already
  crossed; a stop limit whose limit is on the far side of its trigger (once triggered it may
  not fill); a full stop-out reaching the account's `max_daily_loss_pct`; an exit larger than
  the visible book; an order not counted.

How it estimates: a stop or market take profit executes as a market order, so it pays the
taker fee. Slippage is the average price impact on the current order book (the visible
levels) of all the stop losses together, applied to each one's trigger price (and likewise
of all the take profits): one rate for all of them, so the loss grows with the size and a
partial fill never shows more than the full size. It is an estimate, not a bound: if the
stops fire one after another into a book that does not refill, the later ones fill deeper
than this average, and a book thinner when they trigger makes every fill worse.
A stop limit counts at its limit, the worst it fills at, as a taker. A resting limit take
profit fills at its price as a maker, if it fills. Funding is not included. Requests: positions, open orders, the order
book, the fee rates and the account (about 7).

### `plan_trade`

The same for a trade before it is entered, and its size from a risk. Parameters: the wallet
parameters; `symbol` and `side` (`long` or `short`), required; `stop_loss` (the trigger
price, for the whole size) or `stop_losses` (up to 10 `{price, fraction}`; for sizing by risk
the fractions add up to 1); `take_profits` (optional, up to 10 `{price, fraction}`, the
fractions of the size adding up to at most 1); `entry_price` (optional: a limit entry; without
it a market entry); and exactly one of `size` (base units), `risk_usd`, or `risk_pct` (% of
the account value).

With a risk, the size is one at the market's lot size whose loss at the stops (the
move, the entry and stop fees, the estimated slippage of both, as the result reports it)
is at most the risk, under these estimates: today's book and fee rates. It is found by
a search; with several stops at very different distances, rounding each to the lot size
can let a somewhat larger size fit too. It never loses more than the risk. A size larger than
the visible book cannot be estimated and is not offered: the size is then limited by the
book, and a warning says so. A limit entry is planned at its price; on the passive side of
the book it counts as a maker, else a taker. A take profit whose share rounds to 0 at the
lot size is left out, with a warning. Read only: it places
nothing. Place the trade with [`open_position`](#open_position), with the size it returns:
the levels are sized the same way (each its fraction, the nearest of a kind the rest).

Returns what [`get_position_risk`](#get_position_risk) returns for the planned position (the
entry's fee counted in both totals; `entry_price` the estimated entry), and `entry` (`market
(taker)`, `limit (maker)`, `limit (taker)`), `risk_budget` (USD, or null with a given size),
`notional`, `margin` (at the leverage the account has on the market), `leverage`,
`margin_mode`; warnings also for an order below Hyperliquid's 10 USD minimum. A stop or take
profit on the wrong side of the entry, fractions above 1, or not exactly one of `size`,
`risk_usd` and `risk_pct`: `INVALID_ARGUMENT`.

## Trading

The trade tools place, change and cancel orders with real money. They exist only when you
switch trading on, and only for accounts you configure for it.

### Switch it on

Five steps, the same on Linux, macOS and Windows:

Your assistant can do all of this for you, following the
[setup guide](mcp-setup.md#9-for-assistants-how-to-run-this-setup); the key never passes
through the chat.

1. **Know the plan:** you create an API wallet in the Hyperliquid app (More → API:
   Generate, then Authorize API Wallet, confirmed with your wallet) and paste its private
   key into a key file. The key never passes through the chat. The server trades only
   with API wallets: it refuses an account's own key ([why](#why-only-api-wallets)).
2. **Create the files:**

   ```bash
   fractrade-hl-mcp --init
   ```

   It creates the config file (`~/.config/fractrade-hl/mcp.toml`, or the one `--config`
   names), the folder for the key files (`keys` next to it) and an empty key file
   `keys/main`, for you only (on Linux and macOS; on Windows this setup sets no
   permissions), and
   prints where they are. It never overwrites anything.
3. **Fill them in:**
   - in `mcp.toml`, replace `0xYOUR_ACCOUNT_ADDRESS` with your account's address: the
     wallet you trade, not the API wallet (the API wallet's address is not needed: it
     follows from its key);
   - paste the API wallet's private key into `keys/main` with a text editor, nothing else
     (64 hex characters, with or without `0x`);
   - adjust the limits: `max_leverage`, `max_slippage`, the risk guardrails the template
     switches on, optionally `max_order_usd` and `symbols` ([Limits](#limits-guards),
     [Risk guardrails](#risk-guardrails)).

   For another wallet, copy the `[accounts.main]` block with its own name, address and
   `key_file`, and paste that account's API wallet key into its own key file (on Linux and
   macOS readable by you only: `chmod 600`).
4. **Check it:**

   ```bash
   fractrade-hl-mcp --check --enable-trading --online
   ```

   It reads the keys as the server would, never showing them, and asks Hyperliquid
   whether each one is an approved API wallet of its account. Each account should end
   with `the key (0x…) is an API wallet of 0x…`; anything else names the problem (no key
   file yet, the account's own key, an API wallet not authorized yet, expired, or of
   another account).
5. **Start the server with the switch:** add `--enable-trading` to the client's command,
   or set `FRACTRADE_MCP_TRADING=1` in its environment:

   ```bash
   claude mcp add --scope user fractrade -- fractrade-hl-mcp --enable-trading
   ```

The key can come from an environment variable instead (`key_env = "HL_KEY_MAIN"` in place
of `key_file`). A key given to the client that way (`claude mcp add -e`) is stored in the
client's own config, in plain text: the key file keeps it in one place.

#### Why only API wallets

Hyperliquid lets an API wallet sign trading actions for the account that approved it:
orders, cancels, modifies, leverage changes. A withdrawal, a transfer of the account's
funds, or the approval of another API wallet needs the account's own key, so an API
wallet's signature cannot authorize them for that account. The server exposes no
withdrawal or transfer, and its tests run every trade tool through the real SDK signing to
check that only trading actions are ever signed. It signs only with a key it has verified
as an API wallet of the account (below), and refuses the account's own key. So neither the
agent nor the server can move the account's funds out; what an API wallet can still do is
trade, within the account's limits.

Use the API wallet only for this server: a separate one from any bot's (each signer has its
own nonces), never holding funds of its own, never reused for another account. An order is
routed by the key that signs it: if the key were also a funded wallet and lost its
approval, its orders would trade that wallet. The server checks the key before the
account's first signed action and again before every order that adds risk (`buy`, `sell`
or `modify_order` without `reduce_only`, `set_leverage`), so an approval removed meanwhile
is found before the next such order. Closes, cancels and stops use the last confirmed
result, so while the confirmation is kept, a failed lookup never keeps you from reducing
a position (a refused check drops it, and the next call looks the key up again); such a call can still go out with a key whose approval was removed after the last
check (Hyperliquid then rejects it, unless the key's address meanwhile became a funded
wallet of its own, which a dedicated API wallet never does).

#### The key file

The key file is plain text, protected by its permissions: on Linux and macOS it must be a
regular file of your user that no one else can read (`--init` creates it so, and an
editor that saves into it keeps that; otherwise `chmod 600`), else the server refuses it. On Windows the permissions are not
checked and not set (the server warns): keep it in your user folder, which other users
cannot open by default. The server starts on its own, so it
must read the key without you; any encryption it could undo alone, someone acting as your
user could undo as well. Full-disk encryption protects the file on a lost or stolen
machine, and an encrypted backup protects a copy. If a key leaks, revoke the API wallet in
the Hyperliquid app: it can trade, but not withdraw.

Without the switch the server reads no key and offers no trade tool, whatever the config
says. With the switch but no account with `trading = true`, it stays read only and says so
on stderr. `list_accounts` shows `trading_tools: true` and `mode: trade` for each account
the trade tools act on.

A vault or sub-account (`vault = true`) is traded with an API wallet of the account that
controls it (the sub-account's master, or the vault's leader); each order names the vault
or sub-account, so the exchange executes it for that account or rejects it.

### Which account a trade acts on

A trade tool takes `account`. Without it, the only account that can trade; with several,
`account` is required (`AMBIGUOUS_ACCOUNT`). Trade tools never use `default_account`, and
never an address that is not configured.

### What every trade call does

Every trade tool but `kill_switch` (which halts at once, [below](#kill-switch)) runs these
steps, in this order, holding a lock for the account that every server of the config shares
([Several chats, one account](#several-chats-one-account)), so two calls for one account
never run at once, from whichever chat (a bot on the same account is outside this lock):

1. **Halt:** refused with `KILL_SWITCH_ENGAGED` if the account is halted, unless the call
   reduces risk ([below](#kill-switch)).
2. **Signer:** the server checks that the key is an API wallet of this account (as
   `--check --online`); `SIGNER_NOT_AUTHORIZED` if not, also for the account's own key.
   Checked before the account's first signed action and again before every order that
   adds risk; calls that reduce risk use the last result ([why](#why-only-api-wallets)).
3. **Prepare:** the library computes the order as it will be signed: the size rounded down
   to the market's lot size, the price rounded as Hyperliquid requires (5 significant
   figures, which can round up), and for a market order the price from the current mid
   plus or minus the slippage.
4. **Limits:** that order is checked against the account's limits ([below](#limits-guards));
   `GUARD_BLOCKED` if one fails, with every check in `details.checks`. For an order that
   adds risk the server first reads what the account's guardrails need
   ([Risk guardrails](#risk-guardrails)); a failed read refuses the order, and nothing is
   sent.
5. **Send**, through the library, exactly as prepared.
6. **Log:** one line in the server's log at INFO: the tool, symbol, account and outcome.

### Limits (guards)

| Rule | Checks | Tools |
|---|---|---|
| perp only | the market is a perp (Hyperliquid's own or a HIP-3 dex's), not spot or an outcome | every trade tool with a symbol |
| `symbols` | the market is in the account's allowlist, if it has one | `buy`, `sell`, `modify_order` (unless `reduce_only`), `set_leverage` |
| `max_order_usd` (optional) | size × price of the order as signed; a sell at the current mid if that is higher | `buy`, `sell`, `modify_order` (its full new size and price; unless `reduce_only`) |
| `max_slippage` | the slippage of a market order; without `slippage` it is `max_slippage` | `buy`, `sell` without `limit_price`, `close`; `close_all_positions` always uses `max_slippage` |
| `max_leverage` | the leverage asked for; with `check_order_leverage`, also the leverage the account has on the market | `set_leverage`; with `check_order_leverage` also `buy`, `sell`, `modify_order` (unless `reduce_only`) |
| risk guardrails (optional) | the order's margin, the margin in use, margin mode, the last 24 hours, the order rate ([below](#risk-guardrails)) | `buy`, `sell`, `modify_order` (unless `reduce_only`); `isolated_only` also `set_leverage` |

**Risk-reducing calls** are never blocked by `symbols`, `max_order_usd` or the guardrails, and run while
the account is halted: `close`, `close_all_positions`, `stop_loss`, `take_profit`,
`update_stop_loss`, `update_take_profit`, the cancels, and `buy`, `sell` or `modify_order`
with `reduce_only` (Hyperliquid refuses a reduce-only order that would increase a
position).

What the limits mean exactly: `max_order_usd` is the value at the order's own price, in the
market's collateral (USDC on every dex with listed markets); a sell's price is its lowest,
so a sell is valued at the current mid when that is higher. `max_slippage` is the tolerance before Hyperliquid's
price rounding, which can add a few thousandths of a percent. Not checked by the server:
orders split into many below `max_order_usd` (`max_total_margin_pct` limits what they add
up to once filled, not while they rest), and what the exchange checks itself (the minimum order value of $10, margin, prices far
from the oracle, open interest caps): those come back as `ORDER_REJECTED` or
`INSUFFICIENT_MARGIN` with Hyperliquid's text.

### Risk guardrails

Each is optional: it is checked only when its key is set. `--init` writes those with a
value in the template column; delete a line to switch one off. Percentages are of the
account's value (`account_value` of [`get_account`](#get_account), as the account mode
defines it).

| Key | Template | Refuses an order that adds risk when |
|---|---|---|
| `max_margin_pct` | `5` | its margin is above this % of the account value |
| `max_total_margin_pct` | `30` | the margin in use plus its margin is above this % |
| `max_daily_loss_pct` | `10` | the account lost this % or more over the last 24 hours, until it recovers |
| `max_orders_per_hour` | `30` | this many orders that add risk went out in the last hour |
| `check_order_leverage` | off | the leverage the account has on the market, set anywhere (app, bots), is above `max_leverage` |
| `isolated_only` | off | the market uses cross margin (also `set_leverage` with `is_cross: true`) |
| `max_risk_pct` | off | an [`open_position`](#open_position) would lose more than this % of the account if every stop loss fills (after fees and estimated slippage, as [`plan_trade`](#plan_trade) computes it, at the prices as sent). With it, `buy`, `sell` and `modify_order` that add risk are refused: their loss is not limited by stop losses. Checked at entry: moving a stop later (`update_stop_loss`) or cancelling it is not limited by it |

Without `check_order_leverage` and `isolated_only` the account trades with Hyperliquid's
own settings: cross margin, at the leverage set for each market.

**The margin of an order** is its value divided by the leverage the account has on that
market. At 5 % of a $1000 account, $50: an order of up to $500 on a market at 10x, $150
on one at 3x, $50 at 1x. The limit follows each market's leverage. The value is size ×
price at the order's price; a sell at the current mid if that is higher, as a sell priced
below the market fills at the market.

**Margin and loss.** What a position loses is its value times the price move, whatever the
leverage: $1000 of BTC loses $10 per 1 %. Leverage sets how much collateral it ties up and
how close its liquidation is. With **isolated margin** a position is liquidated before it
loses more than its margin. With **cross margin** the account's collateral backs it (in a
unified account the whole balance, in a standard one that dex's), so a position can lose
more than its margin. A stop loss limits the loss either way, but can fill worse in a
fast market.

**What the limits cover:** `max_margin_pct` limits each order, not a position: orders that
add to one position add up, and so can their loss. `max_total_margin_pct` counts the margin
of positions, not of open orders: several resting orders, each within it, can fill beyond
it. Without `max_margin_pct`, `max_total_margin_pct` alone lets one order use up to that
share of the account.

How they are checked:

- **What is read**, before an order that adds risk and only as its guardrails need it: the
  market's leverage (`activeAssetData`) for `check_order_leverage`, `isolated_only` and the
  margins; a sell's mid price; the account ([`get_account`](#get_account): about 2
  requests plus 2 per dex) for the margins and `max_risk_pct`; the portfolio for
  `max_daily_loss_pct`; for `max_risk_pct` with `open_position` also the order book and the
  fee rates. Orders that reduce risk make none of these reads.
- **The last 24 hours** are Hyperliquid's portfolio `day` window, as
  [`get_portfolio`](#get_portfolio) shows it: the loss is the change of its PnL, against
  the account value without that PnL (the value 24 hours ago plus deposits, minus
  withdrawals), so a deposit is not a gain and an account funded today is covered.
- **What cannot be established refuses:** an account value that the account mode does not
  define (portfolio margin, unknown modes), a figure that is missing or no finite number,
  a negative margin in use, a portfolio whose histories are missing or end at different
  times, a read that fails. `GUARD_BLOCKED` names the guardrail or the read, says that the
  order was not sent, and is `retryable` when the failed read is.
- **Margin in use** is Hyperliquid's `totalMarginUsed` of the account's dexes (its own and
  the enabled HIP-3 dexes); margin on other dexes is not counted.
- **The order rate** is counted per exchange account by every server of the config
  together ([Several chats, one account](#several-chats-one-account)); orders from the app
  or bots are not in it.
  An order counts once it may have been sent: not when it was refused before (a used
  cloid, a guardrail, an error that says nothing was sent).
- **Waiting:** the reads run while the call holds the account's lock, so a close, a stop
  or a cancel on the same account waits for them (and their retries). `kill_switch` does
  not wait.

### Client order ids (`cloid`)

Every order the server places has a client order id: `0x` and 32 hex digits. `buy`,
`sell`, `stop_loss` and `take_profit` take one as `cloid`; without it the server makes one.
Every placed order in a result carries its `cloid`; an order returned by `modify_order` has
none (`cloid: null`): use its `order_id`.

A cloid you choose is known before the order is sent: if the answer is lost, the order can
still be found with `get_order_status_by_cloid`. A cloid used before for the same account
(address and network), by any server of the config in the last 30 days, passed or made (also those of orders `close` and
`update_*` placed), is refused with `INVALID_ARGUMENT`, in any case of its hex digits, so a
repeated call cannot place a second order under the same id. It stays used after a
rejection or an unknown outcome. Exceptions: an order that provably was not sent (an
invalid argument, no position for a stop loss, a rate limit or a connect timeout) leaves
its cloid free, so the same call can be made again.

### After an error

Pass the trade's `account` to every read you check with: read tools without `account` use
`default_account`, which can be another account than the one the trade acted on.

- **`OUTCOME_UNKNOWN`:** the order or action was sent, but whether the exchange executed it
  is unknown (the answer was lost and the library's lookup could not settle it). Never call
  again: follow the hint. For an order, look it up with `get_order_status_by_cloid`
  (`details.cloid`); not found yet is no permission to place it again, as it can still
  arrive. For a modify or a leverage change, check `get_order_status`, `get_open_orders`
  and `get_positions`.
- **A call that got no answer** (interrupted, or the client went away): the server still
  finishes it. Check `get_positions`, `get_open_orders` and `get_fills` before placing
  again; a repeated `buy` doubles the position.
- **Partial outcomes** carry what happened in `details`: the size still open of a close
  (`POSITION_NOT_CLOSED`), each position of `close_all_positions`
  (`CLOSE_POSITIONS_INCOMPLETE`), the new and the old stop of an `update_*`
  (`PROTECTION_REPLACEMENT_INCOMPLETE`), the orders `cancel_all_orders` could not cancel
  (`ORDERS_NOT_CANCELED`). See [Errors](#errors).

### Trade tools

| Tool | Does |
|---|---|
| [`buy`, `sell`](#buy-sell) | a market or limit order |
| [`open_position`](#open_position) | a market entry with its stop losses and take profits |
| [`modify_order`](#modify_order) | change an open limit order |
| [`cancel_order`](#cancel_order-cancel_order_by_cloid), `cancel_order_by_cloid` | cancel one order |
| [`close`](#close) | close a whole position |
| [`close_all_positions`](#close_all_positions) | close every position |
| [`stop_loss`, `take_profit`](#stop_loss-take_profit) | protect a position |
| [`update_stop_loss`, `update_take_profit`](#update_stop_loss-update_take_profit) | move the protection |
| [`cancel_all_orders`](#cancel_all_orders) | cancel every order (stop losses too) |
| [`set_leverage`](#set_leverage) | the leverage of a market |
| [`kill_switch`](#kill-switch) | halt trading |

Every trade tool takes `account` and returns `account`, `address`, `base_url` and `data`;
`kill_switch` returns `halted` and `accounts` instead ([Kill switch](#kill-switch)).
An order in `data` has the fields of [`get_open_orders`](#get_open_orders) plus `status`
(`open`, or `filled`) and `average_fill_price`. Sizes are in base units, never USD; prices
in the market's collateral.

#### `buy`, `sell`

| Parameter | Default | Meaning |
|---|---|---|
| `symbol` | required | the perp market |
| `size` | required | in base units; rounded down to the lot size |
| `limit_price` | | the limit price; without it a market order |
| `reduce_only` | `false` | only reduce a position |
| `post_only` | `false` | limit orders only: rest as maker, or be canceled |
| `slippage` | `max_slippage` | market orders only: the worst price from the mid (0.01 = 1 %), at most `max_slippage` |
| `cloid` | made by the server | the client order id |

A market order is immediate or cancel at the mid plus (buy) or minus (sell) the slippage,
so it can fill partly: `filled_size` says how much. A limit order rests until filled or
canceled. Returns the order.

#### `open_position`

| Parameter | Default | Meaning |
|---|---|---|
| `symbol` | required | the perp market |
| `side` | required | `long` or `short` |
| `size` | required | in base units; rounded down to the lot size ([`plan_trade`](#plan_trade) sizes it for a risk) |
| `stop_losses` | required | up to 10 `{price, fraction}`: each closes its share of the position; the shares add up to 1, so the whole position is protected |
| `take_profits` | none | up to 10 `{price, fraction}`, the shares adding up to at most 1 |
| `slippage` | `max_slippage` | the entry's worst price from the mid (0.01 = 1 %), at most `max_slippage` |

A market entry with its protection in one call, through the library's
`open_long_position` / `open_short_position`. The entry is immediate or cancel at the mid
plus or minus the slippage, checked against the account's limits as `buy` and `sell` are,
and sent at exactly that price; it can fill partly. Once it fills, each stop loss and take
profit is placed for its share of the **filled** size, rounded down to the lot size, the
stop loss and the take profit nearest the price taking what rounding left (so, by the
estimate of [`get_position_risk`](#get_position_risk), a partial fill never loses more at its
stops than the full size). Market entries only: a
limit entry would have no stop until it fills; for one, use `buy` or `sell` and protect it
after.

Before anything is sent (`INVALID_ARGUMENT`): stop losses not adding up to 1, prices on the
wrong side of the market (the mid: the entry executes at once), and a level worth less than
Hyperliquid's 10 USD minimum at its price (Hyperliquid may exempt a reduce-only order that
closes a whole position; this does not rely on it). If the entry filled but a level was not
placed, or executed at once because the price had already crossed it:
`PROTECTION_NOT_PLACED`, with `details.orders` (what was placed) and `details.missing`
(`leg`, `kind`, `price`, `size` of each level still missing; `outcome_unknown` with its
`cloid` for one that may still arrive: look it up with `get_order_status_by_cloid` before
placing it again). The position is then open without them: place them with `stop_loss` /
`take_profit`, or close it. Nothing is closed for you. Returns `entry`, `stop_losses`,
`take_profits` and `warnings` (a level whose share rounds to 0 for a small partial fill is
not placed: the nearest placed level of its kind covers its part, or, with none, none does). On `OUTCOME_UNKNOWN`, check
`get_positions`: an entry that filled has no stop loss yet.

#### `modify_order`

`order_id`, `symbol`, `is_buy`, `size`, `price` and `order_type` (`{"limit": {"tif":
"Gtc"}}`, or `"Alo"` to rest as maker only) are required, `reduce_only` is optional: the
whole new order, checked like a new one. Limit orders only: Hyperliquid does not modify
trigger orders (move a stop with `update_stop_loss`). Returns the order after the modify;
use its `order_id` from then on, as Hyperliquid can give it a new one.

#### `cancel_order`, `cancel_order_by_cloid`

`order_id` (or `cloid`) and `symbol`: cancel one open order, stop losses and take profits
included. Returns `{"canceled": true}`, or `false` if it was not open any more (filled,
canceled, or never placed).

#### `close`

`symbol` and optionally `slippage`: closes the whole position with a reduce-only market
order. Returns the closing order. Filled only partly, or rejected (no liquidity within the
slippage): `POSITION_NOT_CLOSED` with `details.remaining` and `details.order`; close again,
with more slippage if the account allows it, or keep the rest. To reduce only part of a
position, use `buy` or `sell` with `reduce_only`. No position: `POSITION_NOT_FOUND`. Once
the position is gone, Hyperliquid cancels its reduce-only orders, such as its stop losses
and take profits (their status: `reduceOnlyCanceled`).

#### `close_all_positions`

No parameters but `account`: closes every position on the account's dexes, each as `close`
does, at `max_slippage`. Every position is tried, also after one fails. Returns
`{"closed": {symbol: order}}`; with failures, `CLOSE_POSITIONS_INCOMPLETE` with `closed` and
`failed` (each position's error) in `details`.

#### `stop_loss`, `take_profit`

`symbol`, `size`, `trigger_price` and optionally `cloid`: a reduce-only order that triggers
when the mark price reaches `trigger_price`, then executes as a market order (Hyperliquid
allows TP/SL orders 10 % slippage). The side comes from the position: sell for a long, buy
for a short. The position's size protects all of it. Returns the order: `open`, or
`filled` if the price was already past the trigger. No position: `POSITION_NOT_FOUND`.

#### `update_stop_loss`, `update_take_profit`

`symbol` and `new_price`: places a new stop loss (take profit) for the whole live position,
then cancels the old ones, so the position is never without one; without one yet, it is
created. Returns the new order. If the new order does not cover the whole position (the
position grew, or the order executed at once, partly), if that could not be checked (the
position could not be read after placing it), or if an old one could not be canceled:
`PROTECTION_REPLACEMENT_INCOMPLETE` with `new_order`, `old_order_ids` (kept, and may still
be open) and `position` in `details`. `position` is the position read when the new order
does not cover it; null when it could not be read, or when an old order could not be
canceled. The message says which; check the position and the open orders before canceling
any old one.

#### `cancel_all_orders`

Optionally `symbol`: cancels every open perp order of the account (or of the symbol), stop
losses and take profits too, so positions are left without protection. Spot orders stay.
Returns `null`; orders the exchange did not cancel: `ORDERS_NOT_CANCELED` with
`details.failed` (symbol, order id, reason for each; the others were canceled). An answer
that does not confirm the cancels at all is `ORDER_ERROR` (empty `details`): check the open
orders. The same holds for `cancel_order` and `cancel_order_by_cloid`.

#### `set_leverage`

`symbol`, `leverage` (a whole number) and `is_cross` (default `true`; `false` for isolated
margin): the leverage for new orders of the market. Hyperliquid lets the leverage of an
open position be raised; other changes with a position open, such as switching between
cross and isolated, it can refuse (`ACTION_REJECTED` with its reason). At most
`max_leverage` and the market's own maximum; isolated-only markets, and accounts with
`isolated_only`, need `is_cross: false`.
Returns `{"symbol", "leverage", "is_cross"}`, the values set. Refused while halted.

### Several chats, one account

An assistant may start a server per chat (the Codex app does) or per session, and each one
trades the same accounts. What protects an account is shared by every server whose config
file is in the same folder, through files in the folder `state` next to it (for you only;
on Linux and macOS protected with file locks, which the system releases when a server ends,
also after a crash):

- **the account lock:** a trade call holds it for its whole run, so two calls for one
  account never run at once, from whichever chat: the second one's guardrails see what the
  first one did;
- **the kill switch:** a halt applies to every server at once;
- **`max_orders_per_hour`:** counts the orders of every server;
- **used cloids:** refused for 30 days, by every server.

A halt lasts while any server of the config that can trade runs, so opening another chat
does not lift it; a trading server that starts while no other one runs clears it (read-only
servers cannot trade, so they do not keep it; they report it in `list_accounts`). Per server, on purpose: the
confirmed signers (a cache: every order that adds risk asks Hyperliquid again) and the dexes
enabled with `enable_perp_dexs`. The margin and loss guardrails read the account from
Hyperliquid, so they see what every chat did, on the dexes the server reads: the margin of a
position on a dex that only another chat enabled is not counted (put the dexes you trade
into `perp_dexs` in the config, so every chat reads them), nor are resting orders. Not
covered: bots, and servers whose config file is in another folder. Two accounts that share
one API wallet are locked separately: signing in the same millisecond from two chats, one
action would be refused by Hyperliquid for a used nonce. Not tested on Windows.

### Kill switch

`kill_switch` with `account` (or alone, with one trading account), or `all_accounts: true`:

1. **Halts** the account at once, the moment the call arrives, before anything else. A
   trade call already running finishes; every later one is refused with
   `KILL_SWITCH_ENGAGED`, except the risk-reducing ones ([above](#limits-guards)).
2. **Reports**, per account, what is still live, read after the halt: the perp positions
   (`symbol`, `dex`, `side`, `size`) and open perp orders (`count`, `stop_losses`,
   `take_profits`), on the dexes in `dexes_read`; a failed read is in `error`, and the halt
   stands anyway.

It cancels and closes nothing, so stop losses stay in place. To flatten, call
`cancel_all_orders` and `close_all_positions` next: they act on the account's dexes
(`dexes_traded`), so a position on another dex needs `enable_perp_dexs` first.

No tool lifts a halt. It applies to every server of the config (every chat) and lasts while
any of them that can trade runs; once none does, the next one to start clears it: quit the app, or every
assistant session, to trade again. Bots, and servers whose config file is in another
folder, keep trading on the same account ([Several chats, one account](#several-chats-one-account)).

## What a tool returns

Compact JSON:

```json
{"account":"main","address":"0x...","base_url":"https://api.hyperliquid.xyz","count":1,"truncated":false,"data":[...]}
```

- Wallet tools: `account`, `address`, `base_url`; market tools: `base_url`.
- Lists add `count` (returned) and `truncated` (more exist); `data` is the list. Tools about
  one thing (`get_account`, `get_order_status`, ...) have `data` as an object.
  `list_accounts` has its fields at the top level.
- Prices, sizes, rates, USD amounts, position `leverage` and `return_on_equity` (a
  fraction) are **decimal strings** (`"0.0000001"`, never `1E-7`).
  `change_24h_percent` and `spread_bps` are numbers rounded to 2 decimals; counts,
  `max_leverage` and `size_decimals` are integers.
- Order and trade ids are integers, as Hyperliquid sends them: an id from one tool can be
  passed to another unchanged (for example from `get_fills` to `get_order_status`).
- A figure Hyperliquid does not report is `null`: for example the mid price of a market
  without orders, a position's liquidation price, or portfolio values of a period without
  history. An empty side of an order book has a null best price, and then mid and spread
  are null too.
- Times are ISO 8601 UTC (`2026-11-06T21:20:00.000Z`).
- Sides: `long`/`short` for positions, `buy`/`sell` for orders and fills.
- Symbols exactly as Hyperliquid names them.

## Errors

A failed call returns an error result (`isError: true`) with:

```json
{"error":{"code":"UNKNOWN_SYMBOL","message":"Unknown symbol 'NVDA'; did you mean 'xyz:NVDA'?","retryable":false,"hint":null,"details":{}}}
```

`retryable` is true only where the same call can succeed later; the library has already
retried what it can. The codes are the library's exceptions (see the
[README's error table](../README.md#error-handling)) and the server's own:

| Code | When | Retryable |
|---|---|---|
| `INVALID_ARGUMENT` | a parameter is missing, unknown, of the wrong type or out of range, or the values do not fit together; for a schema error `hint` lists the tool's parameters | no |
| `UNKNOWN_SYMBOL` | no market has this symbol; the market of that name on another dex, or the token's other spot pairs, are suggested (not by `get_order_book`, nor by `get_open_orders` for spot) | no |
| `ACCOUNT_NOT_FOUND` | no configured account has this name | no |
| `AMBIGUOUS_ACCOUNT` | several accounts, no default, no `account` given; for a trade tool, several trading accounts and no `account` | no |
| `TRADING_DISABLED` | a trade tool for a configured account that cannot trade (no `trading = true`, or trading is off); a name that is not configured is `ACCOUNT_NOT_FOUND` | no |
| `KILL_SWITCH_ENGAGED` | the account is halted; only risk-reducing calls run ([Kill switch](#kill-switch)) | no |
| `GUARD_BLOCKED` | the order breaks one of the account's limits; `details.checks` lists every rule with `passed` and `detail` | no |
| `SIGNER_NOT_AUTHORIZED` | the account's key does not trade it (an API wallet that expired, was removed or belongs to another account); the hint says what to do | no |
| `OUTCOME_UNKNOWN` | sent, but whether it was executed is unknown: never call again, follow the hint ([After an error](#after-an-error)) | no |
| `ORDER_REJECTED`, `INSUFFICIENT_MARGIN`, `ACTION_REJECTED` | the exchange refused it, with its reason; nothing was executed | no |
| `POSITION_NOT_FOUND` | no position in the symbol (`close`, `stop_loss`, `take_profit`, `update_*`) | no |
| `POSITION_NOT_CLOSED`, `CLOSE_POSITIONS_INCOMPLETE`, `PROTECTION_REPLACEMENT_INCOMPLETE`, `ORDERS_NOT_CANCELED` | partly done; `details` says what happened (below) | no |
| `ORDER_ERROR` | a cancel the exchange did not confirm; check the open orders | no |
| `RATE_LIMITED` | Hyperliquid's rate limit | yes |
| `NETWORK_ERROR`, `TIMEOUT` | Hyperliquid could not be reached or did not answer | yes |
| `SERVER_ERROR` | Hyperliquid answered with a server error or a body that is not JSON | yes |
| `REQUEST_REJECTED` | Hyperliquid refused the request | no |
| `HYPERLIQUID_ERROR` | a failure Hyperliquid reports that has no code of its own, e.g. no price for a pair | no |
| `INTERNAL` | anything else (a bug); the details are in the server log only | no |

`details` holds what the error knows beyond its message, so the outcome can be acted on
without parsing text: the server's own errors put their data there (e.g. the checks of
`GUARD_BLOCKED`), and a library error its documented attributes, converted like results
(orders as in `get_open_orders` plus `status` and `average_fill_price`, positions as in
`get_positions`, decimals as strings):

| Code | `details` |
|---|---|
| `ACTION_REJECTED`, `ORDER_REJECTED`, `INSUFFICIENT_MARGIN` | `action` (what was sent), `reason` (Hyperliquid's text) |
| `OUTCOME_UNKNOWN` | `action`; for an order also `symbol` and `cloid` |
| `POSITION_NOT_CLOSED` | `symbol`, `order` (the closing order, or null if it was rejected), `remaining` (still open), `reason` (the rejection, or null) |
| `CLOSE_POSITIONS_INCOMPLETE` | `closed` (the closing order by symbol), `failed` (by symbol, the error of that position: `code`, `message`, `retryable`, `hint`, `details`) |
| `PROTECTION_REPLACEMENT_INCOMPLETE` | `new_order`, `old_order_ids` (may still be open), `position` (the position when the new order does not cover it; null when it could not be read, or an old order could not be canceled) |
| `PROTECTION_NOT_PLACED` | `leg` (`stop_loss`/`take_profit`), `orders` (those placed, `entry` included) |
| `UNSUPPORTED_ACCOUNT_MODE` | `mode` |
| `ORDERS_NOT_CANCELED` | `failed`: each order still open, with `symbol`, `order_id` and `reason` |
| `SERVER_ERROR` | `status_code` |
| `REQUEST_REJECTED` | `status_code`, `error_code`, `error_message`, `error_data` (Hyperliquid's answer; not the HTTP headers) |

A value in `details` that cannot be shown is `"<unavailable>"`, a reference back to itself
or nesting too deep `"<omitted>"`; NaN and infinity are `null`. The error itself is never
lost to its details.

Anything that looks like a private key is redacted in every message and in `details`, and
texts are cut to 500 characters. Calling a tool that does not exist is a protocol error
(JSON-RPC `-32602`), not a tool result.

## Balances and account modes

Hyperliquid accounts run in one of these modes (`get_account` says which):

- **unified**: one balance per token in the spot account, shared by spot and the positions
  of every dex. The USDC already includes the PnL of open positions. `account_value` is the
  USD value of all token balances; `available_to_trade` is USDC total minus what is held. A
  dex's own account value and withdrawable are not meaningful here (they are only the part
  of the collateral backing its positions) and are null.
- **standard**: each dex has its own collateral. `account_value` adds every dex and the spot
  balances; each dex shows its own `account_value` and `withdrawable`. There is no single
  `available_to_trade` (null).
- **portfolio_margin** and **unknown**: the balances and dexes are listed, every total is null.

A token's price comes from its perp market, else from its USDC spot pair; USDC, USDT,
USDC.E and USDBC without a market count as 1 USD. HIP-4 outcome balances and tokens
without a price are listed with a null price and left out of `account_value`; `sources`
names them. Only Hyperliquid's own perps and the enabled HIP-3 dexes are read: positions on
other HIP-3 dexes are not shown, although in a unified account their PnL is in the USDC. The figures come from
separate requests a moment apart.

## Symbols and markets

| Kind | Looks like | In the tools |
|---|---|---|
| perp, Hyperliquid's own | `BTC`, `ETH`, `kPEPE` | every perp tool; `dex: "default"` |
| perp, a HIP-3 dex | `xyz:NVDA`, `para:TOTAL2`, `io:ANTH` | every perp tool once the dex is enabled; `dex: "xyz"` |
| spot | a token for its USDC pair (`HYPE`), or the pair for another quote (`HYPE/USDT0`); Hyperliquid's internal names such as `@107` are translated | `get_open_orders`, `get_fills`, `get_prices`, `list_markets` and `get_order_book` with `market: "spot"`; balances in `get_account` |
| HIP-4 outcome | `#12` (markets), `+12` or `o12` (balances) | left out of every perp tool; unpriced in `get_account` |

Use symbols exactly as Hyperliquid names them; an unknown one is `UNKNOWN_SYMBOL`, mostly
with a suggestion (`NVDA` → `xyz:NVDA`; see [Errors](#errors)). Delisted markets are left
out of the lists and prices; `get_market_info` still describes them.

`xyz:` markets trade around the clock. Outside the external oracle's schedule (for stocks
about 24/5), the oracle follows the dex's own order book, so prices then do not come from the
stock exchange.

### HIP-3 dexes

HIP-3 dexes are perp markets deployed by builders, each with its own markets, order books
and collateral; their markets carry the dex as prefix (`xyz:NVDA`). As in the library,
only Hyperliquid's own perps are read unless you enable a dex: with `perp_dexs` in the
config (kept), or with `enable_perp_dexs` (until the server restarts). **What is not
enabled is not seen**: positions and open orders on such a dex are not in `get_positions`,
`get_open_orders` or `get_account` (which names the dexes it read); fills and funding
payments include every dex. On mainnet in November 2026, `xyz`, `para`, `io` and `mkts`
have listed markets; testnet has other dexes. Each enabled dex adds requests to every read
of positions and open orders.

## Security

- Without the trading switch no private key is loaded and no tool trades. With it, the
  trade tools trade real money within each account's limits; no tool transfers or
  withdraws.
- The server signs only with keys it verified as API wallets of the account (checked at
  first use and before every order that adds risk) and refuses an account's own key; an
  API wallet's signature cannot authorize a withdrawal or transfer of the account's funds,
  and the server exposes no such action ([why](#why-only-api-wallets)). So neither the
  agent nor the server can move the account's funds out; within its limits, it can trade.
- Key files are plain text protected by their permissions; full-disk encryption protects
  them on a lost machine ([the key file](#the-key-file)). If a key leaks, revoke the API
  wallet in the Hyperliquid app.
- Keys never belong in the config file: one there stops the server.
- A key is never part of a tool result; tool errors (message, hint, details) and log lines
  are redacted on top of that.
- The server reads nothing from `.env` files.
- The server writes no files, except a server that can trade: the shared state in `state`
  next to the config file ([Several chats, one account](#several-chats-one-account)).
- MCP is not a sandbox: an assistant with shell access can read what your user can read,
  key files and environment variables included. Use API wallets, and the client's
  permission settings.

## Troubleshooting

| Symptom | What to do |
|---|---|
| The client shows no fractrade tools | Run the client's command by hand (`fractrade-hl-mcp --check`); give the command's full path in the client config. |
| `needs the mcp extra` | `pip install "fractrade-hl-simple[mcp]"` in the Python the command runs with. |
| `AMBIGUOUS_ACCOUNT` | For reads: set `default_account`, or name the account. For trades with several trading accounts: always name the account (the default is never used). |
| `UNKNOWN_SYMBOL` | Use Hyperliquid's name; HIP-3 markets need their dex prefix (`xyz:NVDA`) and an enabled dex (`perp_dexs`, `enable_perp_dexs`). |
| `RATE_LIMITED`, `NETWORK_ERROR` | Call again later; check your connection. |
| No trade tools | Start the server with `--enable-trading` (or `FRACTRADE_MCP_TRADING=1`), with an account with `trading = true`; `fractrade-hl-mcp --check --enable-trading` says whether trading would be on. |
| `SIGNER_NOT_AUTHORIZED` | Create and approve an API wallet for the account in the Hyperliquid app (an approval can expire) and configure its key; the account's own key is always refused. `--check --enable-trading --online` checks every key. |
| `GUARD_BLOCKED` | `details.checks` names the limit; change the order, or the limit in the config. |
| `KILL_SWITCH_ENGAGED` | The account was halted; close and cancel still work. Stop every server of the config (every chat; the Codex app: quit it) and start again to trade. |
| Anything else | Start with `--log-level DEBUG` (or `FRACTRADE_MCP_LOG_LEVEL=DEBUG` in the client's env) and read the client's MCP log. |

## Limits

- Read only unless trading is switched on (`--enable-trading`) for an account with
  `trading = true`.
- Trading covers perps only, on Hyperliquid's own dex and the account's enabled HIP-3
  dexes; no spot orders, transfers, isolated margin changes or trailing stops, and no
  order sized in USD.
- The halt, the account lock, the order rate and the used cloids are shared by the servers
  of one config ([Several chats, one account](#several-chats-one-account)); bots and
  servers of another config file are not stopped or serialized by them. The confirmed
  signers are per server.
- `max_order_usd` and `max_margin_pct` limit each order, not a position: several orders
  can add up. `max_total_margin_pct` limits the margin of positions, not of open orders.
- Positions, open perp orders, balances by dex and the market tools cover Hyperliquid's own
  perps and the enabled HIP-3 dexes; other dexes are not read. Fills and funding payments
  come from account-wide requests and include every dex.
- `get_market_info` and `get_funding_rates` are for perp markets only.
- Funding rates are the current ones; predicted funding is not shown.
- `get_fills` without `start` sees only the 2000 most recent fills; Hyperliquid keeps an
  account's 10000 most recent.
- `get_funding_payments` reads at most 31 days per call.
