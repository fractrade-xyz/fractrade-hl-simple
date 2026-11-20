# MCP server: Hyperliquid in Claude Code, Codex and Claude Desktop

`fractrade-hl-mcp` is a local [MCP](https://modelcontextprotocol.io) server. It lets an AI
assistant read your Hyperliquid accounts and the markets: balances, positions, orders,
fills, funding and prices, for several wallets and for any public address, on Hyperliquid's
own perps and on the HIP-3 dexes you enable (`xyz:`, `para:`, ...).

**Read only by default.** Without the trading switch (`--enable-trading`) it loads no
private key and has no tool that trades. With the switch and an account with
`trading = true`, it also trades real money, within limits you set per account
([Trading](#trading)).

**This page is the reference, for looking things up. To set the server up, follow the
[step-by-step guide](mcp-setup.md).**

The server runs on your computer, over stdio; nothing is hosted.

## Contents

[Install](#install), [Connect a client](#connect-a-client),
[Configuration](#configuration), [Command line](#command-line),
[Accounts, addresses and networks](#accounts-addresses-and-networks), [Tools](#tools),
[Trading](#trading), [What a tool returns](#what-a-tool-returns), [Errors](#errors),
[Balances and account modes](#balances-and-account-modes),
[Symbols and markets](#symbols-and-markets), [Security](#security),
[Troubleshooting](#troubleshooting)

## Install

Python 3.12 or newer. The server is an optional extra of the library; a plain
`pip install fractrade-hl-simple` stays without it.

```bash
pipx install "fractrade-hl-simple[mcp]"     # or pip install, in a virtual environment
```

This adds the `fractrade-hl-mcp` command (also `python -m fractrade_hl_simple.mcp_server`);
the [setup guide](mcp-setup.md#2-install) explains pipx.
Until a release with the server is on PyPI, install from a checkout of this repository:

```bash
pip install -e ".[mcp]"        # or: poetry install --extras mcp
```

## Connect a client

The [setup guide](mcp-setup.md#4-connect-your-assistant) has the command or config snippet
for each client; the client starts the server itself. For a config file somewhere else, add
`--config /path/to/mcp.toml` to the command.

**Approvals.** The read tools are marked read-only for the client: you can let it run them
without asking (not `enable_perp_dexs`, which changes what the server reads and trades).
Keep the client's approval prompt for the trade tools, so you confirm every order before it
is sent. Without it, the agent trades within the account's limits on its own.

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
never falls back to another source. It never reads a `.env` file.

### The file

TOML: an optional `default_account`, an optional table `[defaults]`, and one table
`[accounts.NAME]` per account. `--init` writes one; a complete, commented example is
[`examples/mcp.example.toml`](../examples/mcp.example.toml).

| Key | Where | Meaning |
|---|---|---|
| `default_account` | top | The account a read tool uses when a call names none. Without it, the only account; with several, a tool must name one. |
| `address` | account | Required. A main wallet, a vault, a sub-account, or any wallet you want to watch. |
| `vault` | account | `true` if the address is a vault or sub-account. |
| `base_url` | `[defaults]` or account | `https://api.hyperliquid.xyz` (mainnet, the default), `https://api.hyperliquid-testnet.xyz` (testnet) or `http://localhost:3001` (a local node). Any other value is refused. |
| `perp_dexs` | `[defaults]` or account | The [HIP-3 dexes](#hip-3-dexes) the account trades besides Hyperliquid's own perps: `["xyz", "para"]`. None when absent. |
| `max_slippage` | `[defaults]` or account | The most slippage a market order or close may ask for, as a fraction (default 0.02), [before the price is rounded](#limits-guards). |
| `day_start`, `tz` | `[defaults]` or account | When the trading day of `max_day_loss_pct` starts: `"HH:MM"` in the IANA time zone `tz` (default `"00:00"`, `"UTC"`). |
| `trading`, `key_file` or `key_env`, `max_leverage`, `max_order_usd`, `symbols`, the risk guardrails | account | Trading settings ([Switch it on](#switch-it-on), [Limits](#limits-guards), [Risk guardrails](#risk-guardrails)). |

An account's value for a key replaces the one in `[defaults]`. Keys, types and ranges are
checked when the server starts, the trading settings also while trading is off: an unknown
key (a typo) or an invalid value stops the server, and the message names it. Whether a dex
of `perp_dexs` exists is checked at the first call that reads its network.

- **A private key in the file stops the server:** anything that looks like one (64 or more
  hex characters), in a value or a comment. Keys belong in [key files](#the-key-file).
- On Linux and macOS the config file must be your own and writable by you only
  (`chmod 600`; `--init` creates it so): whoever can change it can point `key_file` at
  another key or lift a limit.

### Without a config file

One account named `default` then comes from the environment of the server process (pass the
variables with the client: `claude mcp add -e`, `codex mcp add --env`):
`HYPERLIQUID_PUBLIC_ADDRESS` (required), `HYPERLIQUID_ENV` (`mainnet`, the default, or
`testnet`), `HYPERLIQUID_IS_VAULT` (`true` for a vault or sub-account) and
`FRACTRADE_MCP_PERP_DEXS` (`xyz,para`). With `HYPERLIQUID_PRIVATE_KEY`,
`FRACTRADE_MCP_MAX_ORDER_USD` and `FRACTRADE_MCP_MAX_LEVERAGE` all set, the account can
trade; the key is read only with the trading switch (`FRACTRADE_MCP_TRADING=1`, as
`--enable-trading`).

## Command line

```
fractrade-hl-mcp [--config PATH] [--log-level LEVEL] [--init | [--enable-trading] [--check [--online]]]
```

| Flag | Meaning |
|---|---|
| `--config PATH` | The config file (else the [sources above](#where-it-comes-from)). |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING` (default), `ERROR` or `CRITICAL`; also `FRACTRADE_MCP_LOG_LEVEL`. Logs go to stderr only, with keys redacted. |
| `--init` | Create the config file and an empty key file `keys/main`, for your user only. It never overwrites anything. |
| `--enable-trading` | The trading switch (also `FRACTRADE_MCP_TRADING=1`): read the keys of the accounts with `trading = true` and offer the trade tools for them ([Trading](#trading)). |
| `--check` | Validate the config, print the accounts and whether trading would be on; exit with 0, or with 1 and the reason. No network request. With `--enable-trading` it also reads the keys. |
| `--online` | With `--check --enable-trading`: also ask Hyperliquid whether each trading account's key is an API wallet the account approved ([a vault or sub-account differs](#why-only-api-wallets)). |

The `--check` summary shows addresses and key sources (variable names, file paths), never
key values. Command line errors are printed as they are, not redacted: never put a key on
the command line.

## Accounts, addresses and networks

A tool about one wallet takes `account` (a configured name, see `list_accounts`) or
`address` (any public address, also one that is not configured; `base_url` then chooses
mainnet, the default, or testnet). Without either, it uses the default account, or the only
one. The market tools take only `base_url`. Trade tools
[choose their account differently](#which-account-a-trade-acts-on). Reading needs no key:
a wallet you only watch can never be traded through the server.

Reads cover the HIP-3 dexes of every configured account on that network. A trade and its
limits cover the dexes of the account that trades. Both include the dexes enabled with
`enable_perp_dexs`. So a read can show a position that the account's limits do not count.

## Tools

| Tool | Reads |
|---|---|
| `list_accounts` | the configured accounts, their limits and dexes; `mode` is `watch` (read only) or `trade` |
| [`enable_perp_dexs`](#enable_perp_dexs) | nothing: it enables HIP-3 dexes until the server restarts |
| [`get_account`](#balances-and-account-modes) | a wallet's balances by its account mode, with its positions |
| `get_positions` | open perp positions on the dexes read |
| [`get_open_orders`](#get_open_orders) | open orders, stop losses and take profits included |
| [`get_fills`](#get_fills) | executed trades, perp or spot |
| [`get_changes`](#get_changes) | what changed since the last look |
| `get_order_status`, `get_order_status_by_cloid` | one order, open or not (fill prices are in `get_fills`) |
| `get_portfolio` | account value, PnL and volume per day, week, month and all time |
| [`get_funding_payments`](#get_funding_payments) | funding the wallet paid or received |
| [`get_pnl`](#get_pnl) | trades of a time range with their PnL |
| [`get_prices`, `list_markets`, `get_market_info`, `get_order_book`, `get_candles`, `get_funding_rates`](#market-tools) | the markets |
| [`get_position_risk`](#get_position_risk) | an open position's outcome at its stops and take profits |
| [`get_account_risk`](#get_account_risk) | all positions together, the margin and the guardrails |
| [`plan_trade`](#plan_trade) | a planned trade's outcomes, and its size from a risk |

Each tool's own description and schema, which your client shows, say what its arguments
are and what it returns; this page explains what their names do not say. Arguments are
checked exactly: nothing is converted or ignored (`INVALID_ARGUMENT`). Times are ISO 8601
(without an offset they are UTC), and a range includes both ends.

### `enable_perp_dexs`

Enables HIP-3 dexes (`dexes`, such as `["io"]`) at one network until the server restarts.
From then on they are read, and every account of that network that can trade trades them,
within its limits. So `perp_dexs` does not limit what is traded: to keep an account to
certain markets, set `symbols`. To keep a dex, add it to `perp_dexs` in the config.

### `get_open_orders`

Open orders on every dex read, or with `market: "spot"` the spot orders. `type` is `limit`,
`stop_loss`, `take_profit`, or `trigger_entry` for a trigger order without `reduce_only`, such
as a [`place_trigger_entry`](#place_trigger_entry). The label says only that: one on the side
that closes a position (a stop placed in the app without reduce-only) can still close or
reverse it, and the stop replacement and the kill switch treat it as protection. Check its
side and the position before canceling one.

**Filled or shrunk.** Hyperliquid shrinks a resting reduce-only order (a stop, a take profit
limit) to the position when the position gets smaller, and reports only the size placed and
the size still open. For such an order with less open than placed, `filled_size` is
therefore null and `reduced_size` is the size no longer open: filled, shrunk, or both
([`get_fills`](#get_fills) shows its fills by `order_id`).

**How an order executes.** For a `Limit` or `Market` order, `limit_price` is its worst
price. A `Stop Market` or `Take Profit Market` order (what `stop_loss`, `take_profit`,
`update_*` and `place_trigger_entry` place) executes once triggered as a market order with
Hyperliquid's 10 % slippage tolerance for TP/SL market orders; Hyperliquid does not document
from which price the 10 % is measured, nor whether the limit price bounds the fill, so a
stop's and a take profit's limit price is sent beyond the trigger by the 10 % (a sell's below,
a buy's above), and `limit_price` shows that price.
For a `Stop Limit` or `Take Profit Limit` order, `slippage_cap_pct` is how far its
`limit_price` lets it fill beyond `trigger_price`, in %.

**Attached orders.** `children` lists the orders attached to an order: the stop loss and
take profit of a resting [`open_position`](#open_position) entry, which Hyperliquid places
only once the entry has filled. Until then they are no open orders of their own, and no
tool counts them as protection.

### `get_fills`

Fills (executed trades), newest first: perp fills of every dex, or with `market: "spot"`
the spot fills. Without `start`, only the 2000 most recent fills are read. `truncated` is
true if more fills match than were returned, or if the list may be missing some;
`truncated_reason` says which: `limit` (more than the `limit` asked for), or `recent_fills`,
`retention_window` (Hyperliquid keeps only an account's 10000 most recent fills) and
`same_millisecond_overflow`, which say fills may be missing, not that they are.

### `get_changes`

What changed in a wallet's perp trading between two looks, read from Hyperliquid's history
on each call: nothing runs between calls. One call covers the interval from `since` to 5
seconds before the call, and its `cursor` is where the next one starts, so each event is
reported once. Without `since`, the interval starts where this server's last call ended
(remembered per wallet, network and `symbol` filter); on the first call it is the last hour.

- `more`: the answer stopped at about 500 events (those of one millisecond stay together,
  so it can hold more): call again with `cursor`.
- `positions.now`, `protection.reduced` and `protection.coverage` are read during the call:
  they show now, not the end of the interval.

**What it cannot see:** an event Hyperliquid records more than 5 seconds late; an order
status change Hyperliquid did not keep (it lists at most 2000 recent orders, not a complete
log); spot and outcome fills and orders. `gaps` names where the history may be cut.

### `get_funding_payments`

Funding the wallet paid or received on every dex, newest first, by default of the last 7
days (a range: at most 31). `total_usdc` covers the whole range, whatever `limit` is:
positive is received, negative is paid. Other currencies are added up per currency in
`other_currencies`, never into USDC: on a HIP-3 dex that is not read the currency is
`unknown`, and such payments are not in `total_usdc`. `funding_rate` is hourly.

### `get_pnl`

A wallet's perp trades of a time range (`start` is required; at most 31 days) with their
PnL, from its fills and funding; `totals` cover the whole range, whatever `limit` leaves
out. A fill that turns a long into a short (or back) closes one trade and opens the next:
its closed PnL belongs to the closing trade, its fee is split by size. Funding counts for
the trade that was open when it was paid.

`net_pnl` is `realized_pnl` (Hyperliquid's closed PnL, before fees) minus `fees` plus
`funding`, in the trade's `currency`: its dex's [collateral](#hip-3-dexes). It leaves out
unrealized PnL, and fees paid in another token (`fees_other`). `totals` adds up the USDC
trades; those in another currency are in `other_currencies`, never added to USDC. For a
trade with `opened_before_start`, `entry_price` covers only the fills in the range. An open
trade has `unrealized_pnl` only when `end` is left out, its dex is read, and the live
position has the size its fills add up to. `truncated_reason: limit` only shortens the
rows returned: the totals still cover them. Any other [`get_fills`](#get_fills) reason means
Hyperliquid's limits may have cut the range's fills, and the totals then miss them too.

### Market tools

`get_prices`, `list_markets`, `get_market_info`, `get_order_book`, `get_candles` and
`get_funding_rates` take `base_url` (mainnet by default) and no account; `dex` is `default`
(Hyperliquid's own perps) or an enabled HIP-3 dex. In `get_funding_rates`,
`funding_hourly` and `funding_annualized` are fractions (`0.0001` = 0.01 %; positive: longs
pay shorts); predicted funding is not shown.

### `get_position_risk`

What an open perp position makes or loses at its stop losses and take profits: its
reduce-only open orders on the closing side (a resting limit counts as a take profit),
each up to what is left of the position, in the order the price reaches them.
`unprotected_size` is the size no stop covers.

**These are estimates, not bounds.** Each level's PnL is after its fee and estimated
slippage. A stop or market take profit executes as a market order and pays the taker fee.
Slippage is the average price impact, on the visible order book now, of all the stop losses
together. If they fire one after another into a book that does not refill, or the book is
thinner when they trigger, the fills are worse. Funding is not included. A stop limit or
take profit limit is valued at its limit price, with the taker fee, and may not fill; a
resting limit pays the maker fee. When the slippage cannot be estimated (the exit is larger
than the visible book), `slippage` is null and the PnL leaves that cost out.

### `get_account_risk`

The open perp positions of an account on the dexes read, together, also those opened
outside this server, with the margin and the losses as the
[loss limits](#risk-guardrails) measure them. Notional is at the mark price, in each
market's collateral counted as USD.

`warnings` collect each position's, low free margin, and these guardrails when a position
or the account breaks them, however a position was opened: `symbols`, `max_leverage`,
`max_risk_pct`, `max_total_margin_pct`, the loss limits and `max_total_notional_x`. Free
margin is low below 10 % of the account value in a unified account; in a standard account,
below 10 % of a dex's own value, for each dex with margin in use. The guardrails themselves
act only on this server's trade calls.

### `plan_trade`

What `get_position_risk` shows, for a trade before it is entered, and its size from a risk.
It places nothing.
Give exactly one of `size`, `risk_usd` (a loss in USD) and `risk_pct` (a loss in % of the
account value); to size by risk, the stop losses' fractions add up to 1. With `entry_price`
the entry is a limit order, without it a market order.

With a risk, the size is one whose loss at the stops (the move, the entry and stop fees,
the estimated slippage of both) is at most the risk, under today's book and fee rates. It
is limited to what the visible book lets estimate; a warning then says so.

The result adds the `notional`, and the `margin` at the leverage the account has on the
market; its totals also deduct the entry fee, for the share the levels close. Place a
market-entry plan with [`open_position`](#open_position), with the size it returns. A plan
with `entry_price` needs a limit order, and its stop losses once that has filled:
`open_position` enters at market, where the planned loss no longer holds.

## Trading

The trade tools place, change and cancel orders with real money. They exist only when you
switch trading on, and only for accounts you configure for it. They trade perps only, on
Hyperliquid's own dex and the account's enabled HIP-3 dexes: no spot orders, transfers,
isolated margin changes or trailing stops.

### Switch it on

The [setup guide](mcp-setup.md#6-trading-optional) has the steps. The switch needs:

- an account with `trading = true`, a `key_file` (or `key_env`: the name of an environment
  variable), `max_leverage` (1 to 50) and at least one of `max_margin_pct`,
  `max_total_margin_pct` and `max_order_usd`;
- in the key file, the private key of an API wallet that the account approved in the
  Hyperliquid app;
- `--enable-trading` on the server's command, or `FRACTRADE_MCP_TRADING=1` in its
  environment.

`fractrade-hl-mcp --check --enable-trading --online` checks all three. With the switch but
no account with `trading = true`, the server stays read only.

#### Why only API wallets

Hyperliquid lets an API wallet sign trading actions for the account that approved it:
orders, cancels, modifies, leverage changes. A withdrawal, a transfer, or the approval of
another API wallet needs the account's own key. The server signs only with a key it has
verified as an API wallet of the account, refuses the account's own key, and has no tool
that withdraws or transfers. So neither the agent nor the server can move the account's
funds out; an API wallet can still trade, within the account's limits.

A vault or sub-account (`vault = true`) is traded with an API wallet of the account that
controls it: the sub-account's master, or the vault's leader. The server can verify only
that the key is an API wallet of some account; each order names the vault or sub-account,
and the exchange executes it for that account or rejects it.

Use the API wallet only for this server: not a bot's (each signer has its own nonces),
never holding funds, never reused for another account. An order is routed by the key that
signs it: a key that is also a funded wallet and lost its approval would trade that wallet.

The key is checked with Hyperliquid once, at the start of every trade call. If the check
itself fails with a retryable error (Hyperliquid down, rate limited), a call that reduces
risk goes ahead when this server's last completed check found the key approved, so an
outage does not keep you from closing a position. Every other call fails with the error.
An approval removed or moved to another account after a call's check, or during such an
outage, is noticed only at the next check. Until then Hyperliquid rejects the action, or,
for an account that is no vault or sub-account, executes it for whichever account the key
signs for by then (above).

#### The key file

The key file is plain text, protected by its permissions. On Linux and macOS it must be a
regular file of your user with no permission for anyone else (`--init` creates it so;
otherwise `chmod 600`), else the server refuses it. On Windows the permissions are not
checked (the server warns): keep it in your user folder. A relative `key_file` is read from
the config file's folder. With `key_env`, a key passed through the client
(`claude mcp add -e`) is stored in the client's config in plain text. Full-disk encryption
protects the file on a lost or stolen machine; encrypt backups of key files too. If a key
leaks, revoke the API wallet in the Hyperliquid app.

### Which account a trade acts on

A trade tool takes `account`. Without it, the only account that can trade; with several,
`account` is required (`AMBIGUOUS_ACCOUNT`). Trade tools never use `default_account`, and
never an address that is not configured.

### Limits (guards)

A trade call is refused while the account is [halted](#kill-switch), unless it reduces
risk, and when its key no longer trades the account
([checked at the start of each call](#why-only-api-wallets)). Then the order is computed as
it will be signed: the size rounded down to the lot size, the price rounded as Hyperliquid
requires (it can round up), a market order's price from the mid plus or minus the
slippage. That order is checked and sent exactly as checked, or refused with
`GUARD_BLOCKED`.

**An order that adds risk** is a `buy`, `sell` or `modify_order`, the entry of
`open_position` or `place_trigger_entry`, or an order of `place_orders` or `place_ladder`,
without `reduce_only`.

| Rule | Checks | Applies to |
|---|---|---|
| perp only | the market is a perp, not spot or an outcome | every trade tool with a symbol |
| `symbols` | the market is in the account's allowlist, if it has one | orders that add risk, `set_leverage` |
| `max_order_usd` (optional) | size × price of the order as signed (`modify_order`: its full new size and price) | orders that add risk |
| `max_slippage` | the slippage asked for a market order | `buy` and `sell` without `limit_price`, `open_position`, `close`; `close_all_positions` always uses `max_slippage` |
| `max_leverage` | the leverage asked for | `set_leverage` |
| [risk guardrails](#risk-guardrails) (optional) | see below | orders that add risk; `isolated_only` also `set_leverage` |

**Risk-reducing calls** are never blocked by `symbols`, `max_order_usd` or the guardrails,
and run while the account is halted: `close`, `close_all_positions`, `stop_loss`,
`take_profit`, `update_stop_loss`, `update_take_profit`, `sync_protection`, the cancels,
and `buy`, `sell`, `modify_order`, `place_orders` or `place_ladder` whose orders are all
`reduce_only` (Hyperliquid refuses a reduce-only order that would increase a position).

**Batches** (`place_orders`, `place_ladder`): each order is checked as a limit `buy` or
`sell`. The guardrails about the account see the batch's orders that add risk together, as
if all of them fill. One failing check stops the whole batch (nothing is sent), unless it
is a limit in `warn` mode: that only adds a warning.

**`max_order_usd`** is the value at the order's own price, in the market's
[collateral](#hip-3-dexes), counted as USD. A sell is valued at the current mid when that
is higher; a market buy at its worst price, the mid plus the slippage. Orders split into
many below `max_order_usd` pass.

**`max_slippage`** caps the slippage asked for. The price is then rounded as Hyperliquid
requires, which can move the signed price further from the mid, most on markets with very
low prices. Market stop losses and take profits, and trigger entries without a
`limit_price`, execute with Hyperliquid's own 10 % tolerance instead.

What the exchange checks itself (the minimum order value of $10, margin, open interest
caps) comes back as `ORDER_REJECTED` or `INSUFFICIENT_MARGIN`. Hyperliquid may exempt a
reduce-only order that closes a whole position from the $10; `open_position` and
`sync_protection` still require it for every planned stop loss and take profit before
anything is sent. After a partial entry fill the levels are smaller, and Hyperliquid can
still reject one.

### Risk guardrails

Each is checked only when its key is set; `--init` writes the first four
([with these values](mcp-setup.md#62-set-your-limits)). Percentages are percent (`5` is 5 %) of the account's value: the
`account_value` of `get_account` over the dexes the account trades. The loss limits have
their own baselines (below).

| Key | Refuses an order that adds risk when |
|---|---|
| `max_margin_pct` | its margin is above this % of the account value |
| `max_total_margin_pct` | the margin in use plus its margin is above this % |
| `max_daily_loss_pct` | the account lost this % or more over the last 24 hours, until it recovers |
| `max_orders_per_hour` | this many orders that add risk went out in the last hour (one counts once it may have been sent, also when rejected or with an unknown outcome; not when it provably was not sent) |
| `check_order_leverage` | the leverage the account has on the market, set anywhere (app, bots), is above `max_leverage` |
| `isolated_only` | the market uses cross margin (also `set_leverage` with `is_cross: true`) |
| `max_risk_pct` | an [`open_position`](#open_position) would lose more than this % of the account if every stop loss fills, after fees and estimated slippage. With it, every other order that adds risk is refused: no stop loss limits its loss |

Without `check_order_leverage` and `isolated_only`, an order uses the leverage and margin
mode the account has on the market, cross or isolated, wherever they were set.

**Limits with a mode.** These two have a mode key: `off`, `warn` (the default: the order
goes through, and the answer's `warnings` name the limit) or `block` (`GUARD_BLOCKED`).

| Key | Mode key | Applies to an order that adds risk when |
|---|---|---|
| `max_day_loss_pct` | `max_day_loss_mode` | the account lost this % or more since the trading day started (`day_start` in the time zone `tz`) |
| `max_total_notional_x` | `max_total_notional_mode` | the notional of the open positions, plus the open orders that add risk, plus this order (a batch: all of them) is more than this many times the account value |

For the total notional, a `modify_order` replaces the old order's share, and an open order
counts as a new one is valued: at its limit price, a trigger order at the higher of its
trigger and limit price, a sell at the current mid when that is higher. The orders are read before the positions, so
an order that fills in between counts twice, never not at all.

**When a limit cannot be checked** (a read that fails, a missing figure, an account value
the account mode does not define: portfolio margin, unknown modes), one of these two warns
in `warn` mode and refuses in `block` mode; every other guardrail refuses the order.

**The two losses.** The last 24 hours (`max_daily_loss_pct`) are Hyperliquid's portfolio
`day` window: the change of its PnL, against the account value without that PnL. Today's
loss (`max_day_loss_pct`) is how much the PnL fell since Hyperliquid's last record at or
before the day's start, against the account value at that record; Hyperliquid keeps one
about every 2 h 20 min, so the day can begin up to that long before `day_start`. For both,
a deposit is no gain and a withdrawal no loss.

**Margin and loss.** An order's margin is its value divided by the leverage the account has
on that market ([an example](mcp-setup.md#62-set-your-limits)). It is not the most you can
lose: a position loses its value times the price move, whatever the leverage. With isolated
margin it is liquidated before it loses more than its margin. With cross margin the
account's collateral backs it (a unified account's whole balance, a standard account's on
that dex), so it can lose more. A stop loss limits the loss either way, but can fill worse
in a fast market.

**What the limits do not cover:**

- `max_margin_pct` limits each order, not a position: orders that add to one position add
  up, and so can their loss. Without it, one order can use the whole of
  `max_total_margin_pct`.
- `max_total_margin_pct` counts the margin of positions on the account's dexes, not of
  open orders: several resting orders, each within it, can fill beyond it.
- `max_orders_per_hour` counts this config's servers, not the app or bots; and
  `max_total_notional_x` cannot see orders placed elsewhere at the same moment.
- `max_risk_pct` is checked at entry: moving or cancelling a stop later is not limited.

### Client order ids (`cloid`)

Every order the server places has a client order id: `0x` and 32 hex digits. `buy`, `sell`,
`stop_loss`, `take_profit` and each order of `place_orders` take one as `cloid`; without it
the server makes one. A placed order in a result carries its `cloid`, except one returned
by `modify_order`: use its `order_id`.

A cloid you choose is known before the order is sent: if the answer is lost, the order can
still be found with `get_order_status_by_cloid`. A cloid that the servers sharing this
state folder recorded for the same address and network in the last 30 days is refused with
`INVALID_ARGUMENT`, so a repeated call cannot place a second order under the same id. Only
an order that provably was not sent leaves its cloid free. This is the servers' own record,
not a check by Hyperliquid: a bot, or a server with another state folder, does not see it.

### After an error

Pass the trade's `account` to every read you check with: without it, read tools use
`default_account`, which can be another account.

- **`OUTCOME_UNKNOWN`:** the order or action was sent, but whether the exchange executed it
  is unknown. Never call again: follow the hint. For an order, look it up with
  `get_order_status_by_cloid` (`details.cloid`; for a batch each of `details.cloids`). Not
  found yet is no permission to place it again: it can still arrive. For a modify or a
  leverage change, check `get_order_status`, `get_open_orders` and `get_positions`.
- **A call that got no answer** (interrupted, or the client went away): the server still
  finishes it. Check `get_positions`, `get_open_orders` and `get_fills` before placing
  again; a repeated `buy` doubles the position.
- **Partial outcomes** carry what happened in `details` ([Errors](#errors)).

### Trade tools

| Tool | Does |
|---|---|
| [`buy`, `sell`](#buy-sell) | a market or limit order |
| [`place_orders`, `place_ladder`](#place_orders-place_ladder) | up to 40 limit orders in one request |
| [`open_position`](#open_position) | an entry with its stop losses and take profits: at market, or a resting limit with them attached |
| [`place_trigger_entry`](#place_trigger_entry) | a stop entry, on a rise or a fall (no stop loss comes with it) |
| [`modify_order`](#modify_order) | change an open limit order |
| [`cancel_order`, `cancel_order_by_cloid`, `cancel_orders`, `cancel_all_orders`](#cancels) | cancel one order, several, or all |
| [`close`, `close_all_positions`](#close-close_all_positions) | close a position, or every position |
| [`stop_loss`, `take_profit`, `update_stop_loss`, `update_take_profit`](#stop-losses-and-take-profits) | protect a position, and move the protection |
| [`sync_protection`](#sync_protection) | resize stop losses and take profits to the position |
| [`set_leverage`](#set_leverage) | the leverage of a market |
| [`kill_switch`](#kill-switch) | halt trading |

When a limit in `warn` mode was broken or could not be checked, the answer also has
`warnings`: the call went through. A placed order has the fields of
[`get_open_orders`](#get_open_orders) plus `status` (`open` or `filled`) and
`average_fill_price`. Sizes are in base units, prices in the market's collateral.

#### `buy`, `sell`

Without `limit_price` a market order: immediate or cancel at the mid plus (buy) or minus
(sell) the `slippage` (0.01 = 1 %, before rounding; by default and at most `max_slippage`),
so it can fill partly: `filled_size` says how much. With `limit_price` it rests until
filled or canceled; with `post_only` as maker, or it is rejected if it would match at once.

`size` is in base units; with `notional_usd` instead, it is that value divided by the
price (the limit price as Hyperliquid rounds it, or the current mid). Either is rounded
down to the lot size. Hyperliquid's minimum order value of $10 applies after rounding, so
pass a little more than 10.

#### `place_orders`, `place_ladder`

`place_orders` sends 1 to 40 limit orders in one request, on one or several perps.
`place_ladder` builds them: 2 to 40 orders of one side at prices evenly spaced from
`from_price` to `to_price`. `size` or `notional_usd` is per step, not for the whole ladder:
`notional_usd: 1000` with 40 steps asks for $40,000. Its prices are not checked against the
market: steps on the
wrong side of the mid fill at once as taker, or with `post_only` are rejected.

Both are checked as a [batch](#limits-guards). Hyperliquid places or rejects each order on
its own, so one rejected order does not stop the others: the result has one row per order,
`open`, `filled` or `rejected` with its `reason`. A ladder's `submitted_size` and
`submitted_value` add up every step sent, rejected ones included: they are not what filled.

#### `open_position`

An entry with its protection in one call ([`plan_trade`](#plan_trade) sizes it for a
risk). `stop_losses` are required: up to 10 `{price, fraction}` whose fractions add up to
1, so the whole entry is protected. `take_profits` are optional, the same way, adding up to
at most 1.

**With `limit_price`** the entry rests as a limit order, with one stop loss and at most one
take profit (fraction 1 each) attached, as Hyperliquid's own app attaches them: Hyperliquid
places them once the entry has filled completely, or when the rest of a partly filled entry
is canceled for lack of margin; until then they show in the entry's
[`children`](#get_open_orders). A partly filled entry's filled part has none until then, and
canceling the entry cancels them. A limit the price has already reached fills at once, and
the attached ones are then placed at once. The answer has the `entry` and the `attached`
orders (kind, trigger price, size). `max_risk_pct` measures the loss from the limit price,
with the taker fee.

**At market** the entry can fill partly. Once it fills, each stop loss and take profit is placed for its
share of the **filled** size
([how the shares are rounded](../README.md#open-position-with-tpsl)). They cover this
entry only: a position you already had in the market needs its own stop.

If the entry filled but a level was not placed, or executed at once because the price had
already crossed it, the answer is `PROTECTION_NOT_PLACED`, with those levels in
`details.missing`. The position is then open without them, and nothing is closed for you:
place them with `stop_loss` or `take_profit`, or close the position. A missing level with
`outcome_unknown` may still arrive: look it up by its cloid first. `warnings` name a level
not placed because its share of a small partial fill rounds to 0. On `OUTCOME_UNKNOWN`,
check `get_positions`: a market entry that filled has no stop loss yet, while a limit entry
may be placed with its stop loss and take profit attached (check `get_open_orders` and the
entry's `children` before placing anything).

#### `place_trigger_entry`

A stop order that opens or adds to a position once the price gets there: a long buys when
the mark price rises to `trigger_price`, a short sells when it falls to it. It fires on a
touch, not on a candle close. `size`, or `notional_usd` at the entry price.

- **Execution.** Without `limit_price` it executes as a market order within Hyperliquid's
  10 % tolerance, so in a gap it can fill well past the trigger; `max_slippage` does not
  apply. With `limit_price` it becomes a limit order at that price, which in a fast move may
  fill partly or not at all.
- **No protection is attached**: Hyperliquid attaches no stop loss or take profit to a stop
  order. Once it fills, place them with the `stop_loss` and `take_profit` tools; until then
  the position has none ([`get_changes`](#get_changes) shows the fill and the position's
  `unprotected_size`). Open orders show it as type `trigger_entry`. With `max_risk_pct` it is
  refused.
- **Limits** are checked now, as for an order that adds risk, at the most it can enter at:
  a long stop market at its trigger plus the 10 %, a short at the higher of its trigger and
  the mid, a stop limit at its limit. Nothing is checked again when it fires. The
  [kill switch](#kill-switch) cancels it, unless it is on the side that closes a position the
  account holds.

#### `modify_order`

Takes the whole new order, which is checked like a new one. Limit orders only: a trigger
order is refused before anything is sent (move a stop with `update_stop_loss`). Use the
`order_id` and `cloid` of the order it returns from then on: Hyperliquid can give it a new
id, and it gets the `cloid` given or a new one.

#### Cancels

- `cancel_order` and `cancel_order_by_cloid` cancel one open order, stop losses and take
  profits included. `{"canceled": false}` means it was not open any more.
- `cancel_orders` cancels several in one request: `orders` by `order_id` and `symbol`, or
  the open limit orders of a `symbol`, optionally of one side and a price range. The second
  form never cancels stop losses or take profits, and skips a limit order with one attached
  to it, since cancelling it cancels them too: cancel such an order by its `order_id`.
- `cancel_all_orders` cancels every open perp order of the account, or of a `symbol`: stop
  losses and take profits too, so positions are left without protection. Spot orders stay.

Orders the exchange did not cancel are `ORDERS_NOT_CANCELED`; the others were canceled.

#### `close`, `close_all_positions`

`close` closes the whole position with a reduce-only market order. Filled only partly, or
rejected (no liquidity within the slippage): `POSITION_NOT_CLOSED` with
`details.remaining`; close again, with more slippage if the account allows it, or keep the
rest. To reduce only part of a position, use `buy` or `sell` with `reduce_only`. Once the
position is gone, Hyperliquid cancels its reduce-only orders, its stop losses and take
profits among them.

`close_all_positions` closes every position on the account's dexes that way, at
`max_slippage`. Every position is tried, also after one fails
(`CLOSE_POSITIONS_INCOMPLETE`, with `closed` and `failed` in `details`).

#### Stop losses and take profits

`stop_loss` and `take_profit` place a reduce-only order that triggers when the mark price
reaches `trigger_price`, then executes as a market order
([with Hyperliquid's 10 % tolerance](#get_open_orders)). The position's size protects all
of it. The order comes back `open`, or `filled` if the price was already past the trigger.

`update_stop_loss` and `update_take_profit` place a new one for the whole live position,
then cancel the old ones on the side that closes it (a stop entry on the other side stays).
Several old ones become this one, and `data.warnings` says so; to move one level of several,
use `sync_protection`.
While the position is open, the old ones are canceled only once the new one is read open and
covers it; once the position is gone, they are cleaned up. Without one yet, it is created.
If the new order does not cover the whole position, if that could not be checked, or if an
old one could not be canceled, the answer is `PROTECTION_REPLACEMENT_INCOMPLETE`: check the
position and the open orders before canceling any old one.

#### `sync_protection`

Resizes the stop losses and take profits of an open position to its live size, level by
level. Each list has 1 to 10 levels of `price` and `fraction` of the live position, adding
up to at most 1. `take_profit_type` is `trigger` (the default: market take profits) or
`limit` (resting reduce-only post-only limit orders, filled as maker).

A list given replaces that kind's orders on the closing side: a level already open with
the same execution, price and size is kept, the others are placed, and old orders that
match no level are cancelled. So an old stop limit is replaced by a market stop. A kind
left out stays as it is; an empty list is refused. A TP/SL set for the entire position in
the Hyperliquid app, and orders with a TP/SL attached, are not changed: they come back as
`untouched`.

New stop losses are placed before the old ones are cancelled, so the position is never
without a stop. Where a take-profit limit is involved, the old ones are cancelled first:
take-profit coverage may temporarily decrease, and if an old one cannot be cancelled, none
is placed ([why](../README.md#resize-stops-and-take-profits-to-the-position)).

Afterwards the open orders and the position are read again: every level must be open with
its size, on an unchanged position. If not, or if a step failed, the answer is
`PROTECTION_SYNC_INCOMPLETE`: nothing is undone, and `details` say what is where.
`warnings` say when the stop losses of this call cover less than the whole position:
untouched stops are not counted, and a call with take profits only says nothing about
stops.

#### `set_leverage`

The leverage for new orders of the market, at most `max_leverage` and the market's own
maximum. Isolated-only markets, and accounts with `isolated_only`, need `is_cross: false`
(isolated margin).
Hyperliquid lets the leverage of an open position be raised; other changes with a position
open it can refuse (`ACTION_REJECTED`). Refused while halted.

### Several chats, one account

An assistant may start a server per chat (the Codex app does), and each one trades the same
accounts. Servers whose config file is in the same folder share, through files in the
folder `state` next to it: the account lock (a trade call holds it for its whole run, so
two calls for one account never run at once), the kill switch, the count of
`max_orders_per_hour` and the used cloids. The margin and loss guardrails read the
account's dexes from Hyperliquid, so they see what every chat did. Dexes enabled with
`enable_perp_dexs` are per server: put the dexes you trade into `perp_dexs`. Not covered:
bots, and servers whose config file is in another folder. Not tested on Windows.

### Kill switch

`kill_switch` with `account` (or alone, with one trading account), or `all_accounts: true`:

1. **Halts** the account at once, the moment the call arrives. A trade call already running
   finishes; every later one is refused with `KILL_SWITCH_ENGAGED`, except the
   [risk-reducing ones](#limits-guards).
2. **Cancels the entries**: the account's open perp orders that would open or add to a
   position (limit and trigger entries, not reduce-only and not on the side that closes the
   position in their market). It waits for a trade call still running, so that call's
   order is seen. Orders that are reduce-only or on the closing side at the position read
   then stay: stop losses and take profits, except those attached to a canceled entry, which
   go with it. If the orders cannot be read, nothing is
   canceled; `entries` says what was canceled, what was no longer open and what was not
   canceled.
3. **Reports**, per account, the positions and open orders still live, read after that. If
   a read fails, the halt stands anyway.

It closes nothing. To flatten, call `cancel_all_orders` and `close_all_positions` next:
they act on the account's dexes, so a position on another dex needs `enable_perp_dexs`
first.

No tool lifts a halt. It applies to every server sharing the state folder (every chat of
the config) and lasts while any of them that can trade runs. Once all of them have stopped,
the next trading server to start there clears it: quit the app, or every assistant session,
to trade again. A read-only server neither keeps nor clears a halt.
If the halt cannot be written to the shared state, the error (`INTERNAL`) says that the
account is halted in this server only: quit the other chats on it.

## What a tool returns

Compact JSON:

```json
{"account":"main","address":"0x...","base_url":"https://api.hyperliquid.xyz","count":1,"truncated":false,"data":[...]}
```

- Wallet tools return `account` (the name, or null for an address), `address` and
  `base_url`; market tools `base_url`. Lists add `count` (returned) and `truncated`: rows
  were left out, or the list could not be shown to be complete (the tool says which).
- Prices, sizes, rates and USD amounts are **decimal strings** (`"0.0000001"`, never
  `1E-7`); order and trade ids are integers: pass them on unchanged. `return_on_equity`
  is a fraction: `0.05` is 5 %.
- A figure Hyperliquid does not report is `null`: for example the mid price of a market
  without orders, or a position's liquidation price.

## Errors

A failed call returns an error result (`isError: true`) with:

```json
{"error":{"code":"UNKNOWN_SYMBOL","message":"Unknown symbol 'NVDA'; did you mean 'xyz:NVDA'?","retryable":false,"hint":null,"details":{}}}
```

Repeat an unchanged call automatically only when `retryable` is true; the library has
already retried what it can. After a partial or unknown outcome, read `details` and follow
the hint if there is one; after a partial close, the next step can be the same `close`
again. The codes are those of the library's exceptions
(the [README's error table](../README.md#error-handling) has them all, with what is
retryable) and the server's own. The ones to know here:

| Code | When |
|---|---|
| `INVALID_ARGUMENT`, `UNKNOWN_SYMBOL`, `ACCOUNT_NOT_FOUND` | an argument is wrong, or names no market or no configured account (for a symbol, the message may suggest a market of that name on another dex) |
| `AMBIGUOUS_ACCOUNT` | no `account` given and no default; for a trade tool, several trading accounts |
| `TRADING_DISABLED` | a trade tool for a configured account that cannot trade |
| `KILL_SWITCH_ENGAGED` | the account is halted; only risk-reducing calls run ([Kill switch](#kill-switch)) |
| `GUARD_BLOCKED` | the order breaks a limit: `details.checks` lists every rule with `passed` and `detail`. Or a read a limit needs failed: `details.cause` is the library's error (`details` can be empty for an answer that could not be parsed), and the call is retryable when the read is |
| `SIGNER_NOT_AUTHORIZED` | the key does not trade the account: an API wallet that expired, was removed or belongs to another account |
| `OUTCOME_UNKNOWN` | sent, but whether it was executed is unknown: never call again ([After an error](#after-an-error)) |
| `ORDER_REJECTED`, `INSUFFICIENT_MARGIN`, `ACTION_REJECTED` | the exchange refused it, with its reason; nothing was executed |
| `POSITION_NOT_CLOSED`, `CLOSE_POSITIONS_INCOMPLETE`, `PROTECTION_NOT_PLACED`, `PROTECTION_REPLACEMENT_INCOMPLETE`, `PROTECTION_SYNC_INCOMPLETE`, `ORDERS_NOT_CANCELED` | partly done; `details` says what happened |
| `ORDER_ERROR` | a cancel the exchange did not confirm; check the open orders |
| `BUSY` | `get_changes` is still running for the same wallet and `symbol` (retryable) |
| `INTERNAL` | a halt that reached this server only (the message says so); anything else is a bug, with the details in the server log only |

`details` holds what a partial outcome did, so it can be acted on without parsing text:
the orders placed or canceled, the size `remaining`, the levels `missing` (each with
`outcome_unknown` and its `cloid` when it may still arrive), and old orders that may still
be open (`old_order_ids`, `not_canceled`, `failed`). Keys are redacted from every text.

## Balances and account modes

`get_account` returns a wallet's balances by its account mode, with the figures of the
library's [account summary](../README.md#check-account-balance). In a **unified** account
the spot balances back the positions of every dex: the USDC already includes the PnL of
open positions, `account_value` is the USD value of all token balances,
`available_to_trade` the USDC total minus what is held, and a dex's own account value and
withdrawable are null. In a **standard** account each dex has its own collateral:
`account_value` adds every dex and the spot balances, and there is no single
`available_to_trade` (null). For **portfolio_margin** and **unknown** every total is null.

A token's price comes from its perp market, else from its USDC spot pair; USDC, USDT,
USDC.E and USDBC without a market count as 1 USD. Tokens without a price are left out of
`account_value`; `sources` names them. Positions on
a dex that is not enabled are not shown, although in a unified account their PnL is in the
USDC.

## Symbols and markets

| Kind | Looks like | In the tools |
|---|---|---|
| perp, Hyperliquid's own | `BTC`, `ETH`, `kPEPE` | every perp tool; `dex: "default"` |
| perp, a HIP-3 dex | `xyz:NVDA`, `para:TOTAL2`, `io:ANTH` | every perp tool once the dex is enabled; `dex: "xyz"` |
| spot | a token for its USDC pair (`HYPE`), or the pair for another quote (`HYPE/USDT0`) | the tools that take `market: "spot"`; balances in `get_account` |
| HIP-4 outcome | `#12` (markets), `+12` or `o12` (balances) | left out of every perp tool; unpriced in `get_account` |

Use symbols exactly as Hyperliquid names them. `xyz:` markets trade around the clock.
Outside the external oracle's schedule (for stocks about 24/5), the oracle follows the
dex's own order book, so prices then do not come from the stock exchange.

### HIP-3 dexes

HIP-3 dexes are perp markets deployed by builders, each with its own markets, order books
and collateral token: USDC on Hyperliquid's own perps and most dexes, a USD stablecoin such
as USDH, USDE or USDT0 on some. Only Hyperliquid's own perps are read unless you enable a
dex: with `perp_dexs` in the config (kept), or with `enable_perp_dexs` (until the server
restarts). **What is not enabled is not seen**: positions and open orders on such a dex are
not in `get_positions`, `get_open_orders` or `get_account`, and the position, open-order
and market tools refuse a symbol of it, naming the dex. Fills and funding payments include
every dex, and take such a symbol.

## Security

- Without the trading switch no key is loaded and no tool trades. With it, the server
  signs only with [API wallets](#why-only-api-wallets), which cannot withdraw or transfer,
  and only [`symbols`](#limits-guards) limits the markets an account trades.
- Keys are in [key files](#the-key-file) or environment variables, never in the config
  file or a tool result; errors and log lines are redacted on top of that.
- MCP is not a sandbox: an assistant with shell access can read what your user can read,
  key files and environment variables included. Use API wallets, and the client's
  permission settings.

## Troubleshooting

For an error code, see [Errors](#errors); for no tools, no trade tools, the messages of
the setup and the debug log, the [setup guide](mcp-setup.md#8-when-something-does-not-work).

- **`needs the mcp extra`:** install it again with the extra:
  `pipx install --force "fractrade-hl-simple[mcp]"`, or without pipx
  `pip install "fractrade-hl-simple[mcp]"` in the Python the command runs with.
- **Signed actions are refused, or `get_changes` skips events:** the computer's clock must
  be right (synchronized, as by NTP): a signed action carries the time as its nonce, and
  `get_changes` ends its interval at the local time minus 5 seconds.
