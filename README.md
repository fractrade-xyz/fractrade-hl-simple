<img src="https://raw.githubusercontent.com/fractrade-xyz/fractrade-hl-simple/main/docs/logo.svg" alt="fractrade logo" width="64" height="64">

# fractrade-hl-simple

A Python client for Hyperliquid perpetual futures, built on the official Hyperliquid Python SDK: prices, order books, candles and funding; balances, positions, orders and fills of any wallet; market and limit orders, stop losses and take profits, trigger entries and client order ids; spot and the HIP-3 dexes (`xyz:` stocks and commodities, and others). Requests that fail on the network, a rate limit or a server error are retried; every error is an exception with a fixed `code` and `retryable` ([Error Handling](#error-handling)).

It includes **`fractrade-hl-mcp`, an MCP server** for Claude Code and Claude Desktop. It runs locally over stdio and reads your Hyperliquid accounts and the markets. Trading is off by default; switched on per account, every order that adds risk is checked against the limits you set for that account (symbols, order size, margin, daily loss, loss at the stop, order rate), and orders are signed only with an API wallet. See [MCP Server](#mcp-server-claude-code-claude-desktop).

## Installation & Updates

Using pip:
```bash
pip install fractrade-hl-simple

# Update to latest version
pip install --upgrade fractrade-hl-simple
```

Using poetry:
```bash
poetry add fractrade-hl-simple

# Update to latest version
poetry update fractrade-hl-simple
```

## Setup

1. Create a `.env` file in your project root:
```env
HYPERLIQUID_ENV=mainnet  # or testnet
HYPERLIQUID_PUBLIC_ADDRESS=your_public_address
HYPERLIQUID_PRIVATE_KEY=your_private_key
```

We recommend creating a separate **API wallet** in the Hyperliquid UI for automated trading. API wallets cannot withdraw funds, limiting risk. When using an API wallet, set `HYPERLIQUID_PUBLIC_ADDRESS` to your **main account address** and `HYPERLIQUID_PRIVATE_KEY` to the **API wallet's private key**.

2. Initialize the client:
```python
from fractrade_hl_simple import HyperliquidClient

client = HyperliquidClient()
```

## Client Configuration

The client accepts several optional parameters:

```python
from hyperliquid.utils import constants

client = HyperliquidClient(
    base_url=constants.MAINNET_API_URL,  # or TESTNET_API_URL; default: HYPERLIQUID_ENV, else mainnet
    default_slippage=0.05,      # 5% default slippage for market orders (above 0, at most 0.5)
    max_retries=3,              # Retry transient failures (0 to disable)
    retry_delay=1.0,            # Base delay between retries (exponential backoff)
    cache_market_specs=True,    # New clients reuse market specs loaded in the last 24 hours
    extended_universe=True,      # Enable stocks, commodities, indices, forex (xyz: symbols)
    request_timeout=10.0,       # Seconds to wait for each HTTP request (above 0, at most 120)
)
```

`base_url` takes the network the same way as the Hyperliquid SDK, and only its constants: `MAINNET_API_URL`, `TESTNET_API_URL` or `LOCAL_API_URL`. Any other URL, even with a trailing slash, raises `ValueError`, because the SDK would sign for testnet with it and mainnet would reject every order. `env="mainnet"` / `env="testnet"` still works but is deprecated.

**Retry logic**: Reads, cancels and leverage changes retry on network errors, timeouts, rate limits and server errors. Orders, modifies, transfers and margin changes are retried only when the request provably did not reach the exchange (rate limit, connect timeout). An order whose answer is lost is looked up by its client order id, never sent again ([Client Order IDs](#client-order-ids)). Auth errors and validation errors are never retried.

**Clock**: keep the computer's clock synchronized (NTP). As in the Hyperliquid SDK, every signed action carries the current time in milliseconds as its nonce; after a clock was set back, Hyperliquid can refuse signed actions until it has caught up.

## Authentication Modes

### 1. Environment Variables (Default)
```python
client = HyperliquidClient()  # Loads from .env automatically
```

### 2. Explicit Account
```python
from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount

account = HyperliquidAccount(
    private_key="your_private_key",
    public_address="your_public_address"
)
client = HyperliquidClient(account=account)

# The same, from a key: without public_address, the key's own address
account = HyperliquidAccount.from_key("your_private_key", public_address="your_main_account_address")
# A vault or sub-account, traded with its master's key
account = HyperliquidAccount.from_key("master_key", public_address="vault_address", is_vault=True)
# From HYPERLIQUID_PRIVATE_KEY, HYPERLIQUID_PUBLIC_ADDRESS (both required) and HYPERLIQUID_IS_VAULT
account = HyperliquidAccount.from_env()

client.is_authenticated()   # True if the client has a key (checked locally, no request)
client.get_user_role()      # Hyperliquid's userRole: {"role": "agent", "data": {"user": ...}}, "user", "vault", ...
```

With an API wallet's key, `public_address` must be the account it trades for (the key's own
address is the API wallet's); with `is_vault=True`, the vault or sub-account.

### 3. Without a Key: Market Data and Any Wallet
```python
reader = HyperliquidClient(trust_env=False)  # no key; none of the library's settings read from the environment

reader.get_price("BTC")
reader.get_positions(address="0x...")       # any wallet, passed per call
reader.get_open_orders(address="0x...")
reader.get_perp_balance("0x...")
```

`trust_env=False` works like requests' `Session.trust_env`, for this library's settings: the client reads no `.env` file, key, address or `HYPERLIQUID_ENV`, so it can never pick up the key of your `.env` wallet. HTTP settings such as proxy variables are still applied by requests itself; pass `proxy=` to set a proxy explicitly. Without `account`, it has no key; trade methods raise, and every read about a wallet takes it as `address=`. Reads without an address on such a client raise `AddressRequiredException`. `HyperliquidClient()` without credentials still falls back to reading public data only.

A key that is given must work: an `account` that cannot sign (no or malformed key or address) raises `InvalidArgumentException` when the client is created, and a `HYPERLIQUID_PRIVATE_KEY` in the environment that cannot be used (malformed, or without `HYPERLIQUID_PUBLIC_ADDRESS`) raises `ConfigurationException`. A private key is 64 hex characters, with or without `0x`.

## Basic Usage

### Get Market Prices
```python
btc_price = client.get_price("BTC")
all_prices = client.get_price()  # Returns dict of all symbols
```

### Check Account Balance
```python
balance = client.get_perp_balance()
print(f"Account balance: ${float(balance):,.2f}")

total = client.get_all_balances()        # perp + spot, never counting shared collateral twice
mode = client.get_account_mode()         # "standard", "unified", "portfolio_margin" or "unknown"
```

In a unified account (the default for accounts created in the Hyperliquid app), spot and all
perp dexes share one balance per token in the spot account. `get_perp_balance()` then returns
the spot USDC, which already includes the PnL of open positions, and `get_all_balances()` the
spot balance. For portfolio-margin accounts and unknown account modes both raise
`UnsupportedAccountModeException`; use `get_spot_balance(simple=False)` and `get_user_state()`
for the figures per token and per dex.

```python
client.get_perp_balance(simple=False)   # {"balance", "positions", "margin_used", "state"}
client.get_all_balances(simple=False)   # {"total_balance", "account_mode", "perp", "spot"}
client.get_user_state()                 # every perp dex of the client, merged
client.get_user_state(dex="xyz")        # one dex only; dex="" is Hyperliquid's own perps
```

`get_account_summary()` gives the whole picture by account mode:
```python
summary = client.get_account_summary()   # or any wallet: get_account_summary("0x...")
summary.mode                # "unified"
summary.account_value       # USD, PnL of open positions included
summary.available_to_trade  # unified: USDC total minus hold
summary.balances            # every token: total, hold, available, price, USD value
summary.dexes               # every perp dex with its positions and margin used
summary.sources             # how each figure was computed
```
A figure that is not established for the mode is `None`: per-dex account value and withdrawable in unified accounts (the dex figures are only the part of the collateral backing its positions), `available_to_trade` in standard accounts (each dex has its own collateral), and every total for portfolio-margin accounts and unknown modes. Outcome balances (HIP-4) are listed, unpriced and left out of the totals.

### View Positions
```python
positions = client.get_positions()
for pos in positions:
    direction = "LONG" if pos.is_long else "SHORT"
    print(f"{pos.symbol} {direction} {float(pos.size):+.3f} @ ${float(pos.entry_price):,.2f}")

client.has_position("BTC")             # True or False
client.get_position_size("BTC")        # signed Decimal (negative: short), None without a position
client.get_position_direction("BTC")   # "long", "short" or None
```

### Place Orders

```python
# Market buy
order = client.buy("BTC", size=0.001)

# Market buy with custom slippage (overrides default_slippage)
order = client.buy("BTC", size=0.001, slippage=0.02)  # 2% slippage

# Limit buy
order = client.buy("BTC", size=0.001, limit_price=80000.0)

# Market sell
order = client.sell("BTC", size=0.001)

# Limit sell
order = client.sell("BTC", size=0.001, limit_price=90000.0)

# The size and price an order would be sent with, without sending it: rounded as the
# exchange needs them; without limit_price the market price (mid ± slippage), read now
size, price = client.prepare_order("BTC", 0.001, is_buy=True)
if size * price <= 500:
    order = client.create_order("BTC", size, True, limit_price=price, time_in_force="Ioc")  # = client.buy("BTC", 0.001)

# Change an open limit order: side, size, price, order type and reduce_only, all given anew
order = client.modify_order(12345, "BTC", is_buy=True, size=0.002, price=79500.0,
                            order_type={"limit": {"tif": "Gtc"}})

# A size for a risk in USD: risk / |mid - stop|, rounded to the lot size (needs a key)
from decimal import Decimal
size = client.calculate_position_size("BTC", Decimal("50"), Decimal("80000"))
```

`modify_order` returns the order after the modify (`open`, or `filled` if the new price
matched at once); use its `order_id` from then on, Hyperliquid can give it a new one. A modify
whose answer is lost raises `ActionOutcomeUnknownException`: check the order with
`get_order_status()` before trying again. `calculate_position_size` leaves out fees and
slippage, and rounds to the market's lot size with the `Decimal` context's rounding
(`ROUND_HALF_EVEN` by default); it does not force rounding down.

### Stop Loss and Take Profit

```python
# The side comes from the open position: sells for a long, buys for a short
client.stop_loss("BTC", size=0.001, trigger_price=80000.0)
client.take_profit("BTC", size=0.001, trigger_price=95000.0)

# is_buy is optional; a side that would not close the position raises ValueError
client.stop_loss("BTC", size=0.001, trigger_price=90000.0, is_buy=True)  # short position

# Move the stop (take profit) of the live position: the new order first, then the old ones cancelled
# (those on the side that closes the position; a stop entry on the other side stays)
client.update_stop_loss("BTC", 81000.0)
client.update_take_profit("BTC", 96000.0)

# One-shot: the stop loss moved to 2 % from the current price (below for a long), via update_stop_loss;
# None without a position. It does not keep trailing: call it again to follow the price.
client.trailing_stop("BTC", 2.0)

client.get_stop_loss_price("BTC")     # the trigger price of the position's first stop loss, or None
client.get_take_profit_price("BTC")   # the same for take profit triggers (not limit orders)
```

### Resize Stops and Take Profits to the Position

After adding to a position or closing part of it, `resize_protection` sets its stop losses
and take profits level by level, each (price, fraction of the live position):

```python
result = client.resize_protection(
    "ICP",
    stop_losses=[(2.555, 0.5), (2.58, 0.5)],
    take_profits=[(2.42, 0.4), (2.37, 0.4), (2.31, 0.2)],
    take_profit_type="limit",   # resting reduce-only post-only limits (maker); default "trigger"
)
result.placed, result.kept, result.canceled, result.untouched
```

Pass at least one list. A list given replaces that kind's orders; a list left out (`None`)
leaves that kind alone. An empty list is refused (`InvalidArgumentException`): it does not
remove a kind. Each fraction is above 0 and at most 1, and a list's fractions add up to at
most 1. A level already open with its size is kept without a request. Sizes are rounded down
to the lot size, as in `open_long_position`.

The order of the steps follows how Hyperliquid treats reduce-only orders. Hyperliquid keeps a
position's resting reduce-only limit orders at most as large as the position, and cancels one
itself (`reduceOnlyCanceled`) when a new one would exceed it. It does the same when the position
gets smaller. Untriggered stops don't count. This was seen live; it isn't in Hyperliquid's docs.
So:

- **Stop losses:** the new ones are placed first. The old ones are cancelled only once every
  level is read open with its size, on an unchanged position.
- **Take profits, when a resting limit is involved** (one of the old take profits is a limit,
  or `take_profit_type="limit"`): the old ones that match no level are cancelled first, then
  the new ones placed; take-profit coverage may temporarily decrease (kept and untouched ones stay). None is
  placed if an old one can't be cancelled. Take profit triggers replacing triggers
  are placed first, as stops are.

Stop losses are market triggers. One call takes one take-profit type. Native position TP/SL set
in the Hyperliquid app, and orders with a TP/SL attached, are left alone (`untouched`).
Levels on the wrong side of the mid, below one lot or Hyperliquid's 10 USD minimum, or two
at one price are refused before anything is sent (`InvalidArgumentException`). If it isn't
done as asked, `ProtectionSyncIncompleteException` carries `placed`, `kept`, `canceled`,
`retained` (old orders deliberately left open: the step that would cancel them was not
reached), `not_canceled` (old orders whose cancel failed: they may still be open), `missing`
(each with `outcome_unknown` and its `cloid` when it may still arrive), and `position`.
Nothing is undone. The result, `ProtectionSync`, also has `symbol` and `position_size`
(signed: negative is short).

### Open Position with TP/SL

```python
# Long with stop loss and take profit
position = client.open_long_position(
    symbol="BTC",
    size=0.001,
    stop_loss_price=80000.0,
    take_profit_price=95000.0,
)
# Returns: {"entry": Order, "stop_loss": Order, "take_profit": Order}

# Short with TP/SL
position = client.open_short_position("BTC", 0.001, stop_loss_price=90000.0, take_profit_price=75000.0)

# Several stops and take profits, each (price, fraction of the position)
position = client.open_short_position(
    "ICP", 1300,
    stop_losses=[(2.555, 0.5), (2.58, 0.5)],
    take_profits=[(2.42, 0.4), (2.37, 0.4), (2.31, 0.2)],
)
# Returns: {"entry": Order, "stop_loss_1": Order, "stop_loss_2": Order, "take_profit_1": Order, ...}
```

Each level is placed right after the entry, if it filled at once, for its fraction of the
**filled** size (rounded down to the lot size; the stop loss and the take profit nearest the
price take what rounding left, so the levels cover exactly their share, and a partial fill
never loses more at its stops than the full size, before slippage). A level whose fraction
rounds to 0 lots for the filled size is skipped, and left out of the result.

A limit entry that rests (Gtc, the default with a price, or Alo) takes one stop loss and one
take profit (`stop_loss_price`, `take_profit_price`), sent attached to it as Hyperliquid's app
attaches them; the result is `{"entry": Order}`. Hyperliquid places them for the entry's size
once it has filled completely (or its rest is canceled for lack of margin); until then they
show in the entry's `Order.children`, a partly filled entry's filled part has none, and
canceling the entry cancels them. Several levels need an entry that executes at once. Prices on the wrong side of the entry, fractions
above 1, and levels worth less than Hyperliquid's 10 USD minimum are refused before anything
is sent. If the entry fills but a level cannot be placed, or executes at once because the
price had already crossed it, `ProtectionOrderException` carries the orders placed
(`orders`) and those missing (`missing`: leg, price, size; `outcome_unknown` and `cloid` for
one that may still arrive). `time_in_force="Ioc"` with `limit_price` makes a market entry with
a price limit; its levels are checked against the mid.

### Trigger Entries (Stop Orders)

```python
# Buy 0.001 BTC once the mark price rises to 90,000
order = client.trigger_order("BTC", True, 0.001, trigger_price=90000.0)  # "open" until it fires

# A stop limit: once triggered, a limit order at 90,450 instead of a market order
client.trigger_order("BTC", True, 0.001, trigger_price=90000.0, limit_price=90450.0)
```

A buy triggers on a rise (`trigger_price` above the price), a sell on a fall; it fires on a
touch of the mark price, not on a candle close. Without `limit_price` it executes as a market
order within Hyperliquid's 10 % tolerance, so in a gap it can fill well past the trigger; with
it, in a fast move it may fill partly or not at all. Hyperliquid attaches no stop loss or take
profit to a stop order: place them with `stop_loss()` / `take_profit()` once it fills.

### Maker Orders (Fee-Optimized)

```python
# Maker buy/sell: the timing comes from the order book spread
order = client.maker_buy("BTC", 0.001)    # Tight spread → 30s timeout, 3s reprice
order = client.maker_sell("kPEPE", 1500)  # Medium spread → 45s timeout, 5s reprice

# Equivalent verbose form
order = client.maker_order("BTC", is_buy=True, size=0.001)

# Override auto-detection with manual timing
order = client.maker_buy("DYM", 30, timeout=60, reprice_interval=8, fallback="ioc")

# fallback options: "ioc" (default), "market", "cancel"

# reduce_only: only reduce a position (the fallback order too)
order = client.maker_sell("BTC", 0.001, reduce_only=True)

# Prices from your own feed instead of an order book request per reprice, and an event your
# fill listener sets to check the order at once instead of waiting for the next reprice
import threading
order = client.maker_buy("BTC", 0.001, bbo_cache={"BTC": {"bid": 85000.0, "ask": 85001.0}},
                         fill_event=threading.Event())

order.is_maker, order.attempts, order.elapsed, order.spread_bps   # how it was filled
```

`bbo_cache` is read at every reprice (keep it updated), also to pick the timing. A reduce-only
result whose fills were read from an attempt's sizes after its cancel has
`filled_size_known=False` (Hyperliquid also shrinks those sizes to the position); a first
attempt that filled at once keeps `True`.

### Close Position
```python
from fractrade_hl_simple import PositionNotClosedException

try:
    close_order = client.close("BTC")  # the whole position closed
except PositionNotClosedException as e:
    # The closing order is immediate-or-cancel: it filled only part (e.order), or was rejected
    # (e.order is None, e.reason is Hyperliquid's text). e.remaining is still open.
    print(f"{e.remaining} BTC still open")
```

### Cancel Orders
```python
client.cancel_order(order_id=12345, symbol="BTC")  # Accepts str or int
client.cancel_all_orders("BTC")   # Cancel all BTC orders
client.cancel_all_orders()        # Cancel every perp order on the client's dexes (spot orders stay)
client.cancel_all()               # the same, an older name

closed = client.close_all_positions()   # {symbol: closing order}
```

`close_all_positions` tries every position, also after one fails, and then raises
`ClosePositionsIncompleteException` if one is not closed ([Error Handling](#error-handling)).

### Client Order IDs
Every single order (`buy`, `sell`, `stop_loss`, `take_profit`, `close`, spot and maker orders, ...) is placed with a client order id (`cloid`, `0x` and 32 hex digits): yours, or a random one. In `bulk_order()` only orders whose dict has a `cloid` carry one. `order.order_id` is Hyperliquid's id (a number), `order.cloid` the client order id; open orders have theirs too. An id you choose is known before the order is sent, so you can still find the order if its answer was lost (a crash, a timeout):
```python
import secrets

cloid = "0x" + secrets.token_hex(16)  # store it before sending
order = client.buy("BTC", 0.001, limit_price=80000, cloid=cloid)

client.get_order_status_by_cloid(cloid)
client.cancel_order_by_cloid(cloid, "BTC")
```

## Spot Trading

Trade spot tokens (e.g., PURR, HYPE, FRAC) using just the token name; the library maps it to the `/USDC` pair.

### Transfer Between Wallets
```python
# Move USDC between perp and spot wallets (requires main wallet key, not API wallet)
client.transfer_to_spot(100.0)   # $100 USDC to spot
client.transfer_to_perp(50.0)    # $50 USDC back to perp
```

### Buy and Sell
```python
# Market buy
order = client.spot_buy("FRAC", size=500)

# Limit buy
order = client.spot_buy("FRAC", size=500, limit_price=0.020)

# Market sell
order = client.spot_sell("FRAC", size=500)

# Limit sell
order = client.spot_sell("FRAC", size=500, limit_price=0.050)

# Every option: post_only, time_in_force ("Gtc", "Ioc", "Alo"), slippage, cloid
order = client.create_spot_order("HYPE", 2, True, limit_price=40.0, post_only=True,
                                 cloid="0x" + secrets.token_hex(16))
```

Without `time_in_force`, a spot order without `limit_price` is a market order (`Ioc` at the
price plus or minus the slippage), one with it `Gtc` (it can fill at once or rest). An explicit
`time_in_force` is used as given; `post_only=True` is `Alo` and refuses `Ioc`.

### Spot Price
```python
price = client.get_spot_price("FRAC")

# Every spot pair with its current figures (one request); prices and volume in the quote token
for market in client.get_spot_markets():
    print(market.symbol, market.quote, market.mark_price, market.mid_price, market.day_volume)
```

### Cancel Spot Orders
```python
client.spot_cancel_order(order_id=12345, token="FRAC")
client.spot_cancel_all_orders("FRAC")   # Cancel all FRAC spot orders
client.spot_cancel_all_orders()          # Cancel all spot orders
```

### Spot Order Book
```python
book = client.get_spot_order_book("FRAC")
print(f"Best bid: ${book['best_bid']:.6f}, Best ask: ${book['best_ask']:.6f}")
```

### Spot Fills
```python
fills = client.get_spot_fills("FRAC")       # FRAC fills only
all_spot_fills = client.get_spot_fills()     # All spot fills
for fill in fills:
    print(f"{fill.symbol} {fill.direction} {fill.size} @ {fill.price}")
```

### Spot Open Orders and Balance
```python
orders = client.get_spot_open_orders("FRAC")
balance = client.get_spot_balance()              # Total spot balance in USD
balance = client.get_spot_balance(simple=False)  # Detailed per-token balances

# For multiple wallets, fetch prices once to avoid redundant API calls:
prices = client.get_price()
for wallet in wallets:
    balance = client.get_spot_balance(wallet, prices=prices)
```

## Leverage Management

```python
# Set cross leverage
client.set_leverage("BTC", 10)

# Set isolated leverage
client.set_leverage("ETH", 5, is_cross=False)

# Add margin to an isolated position
client.add_isolated_margin("ETH", 100.0)  # Add $100
```

## Order Tracking

```python
# Get recent fills
fills = client.get_fills()                    # All symbols, the 2000 most recent fills
fills = client.get_fills("BTC")               # BTC only

for fill in fills:
    print(f"{fill.symbol} {fill.direction} {fill.size} @ {fill.price} pnl={fill.closed_pnl}")

# Get fills in a time range (timestamps in milliseconds)
import time
start = int((time.time() - 86400) * 1000)  # 24 hours ago
fills = client.get_fills_by_time(start_time=start)

# The same, and whether Hyperliquid's limits cut the range short
history = client.get_fill_history(start_time=start)
history.fills             # oldest first, each fill once (trade_id)
history.truncated         # True if fills of the range may be missing
history.truncated_reason  # "retention_window" (only the 10000 most recent fills are kept)
                          # or "same_millisecond_overflow"

# Trades and their PnL: flat -> position -> flat, with fees and funding
report = client.get_pnl(start_time=start)
for trade in report.trades:  # oldest first
    print(trade.symbol, trade.side, trade.status, trade.realized_pnl, trade.fees, trade.funding, trade.net_pnl)
report.net_pnl, report.unrealized_pnl  # totals

# Check order status
status = client.get_order_status(order_id=12345)   # Hyperliquid's orderStatus answer, any state
client.get_open_order_by_id("BTC", 12345)          # the Order if still open, else None
client.has_active_orders("BTC")                     # any open order (all symbols without one)

# Recent orders with their status (Hyperliquid's historicalOrders, at most 2000 entries, newest
# first; every dex and spot): order.status ("filled", "canceled", "reduceOnlyCanceled", ...) and
# order.status_time, when it got it
for order in client.get_historical_orders():
    print(order.symbol, order.status, order.status_time)
```

A fill that turns a long into a short splits into two trades. `opened_before_start`: the
trade was open when the range began, so its `entry_price` covers only the fills in the
range. Open trades have the position's unrealized PnL when the range ends now (positions of
the client's dexes). Each trade has its `currency`, its dex's collateral (`"USDC"`, or e.g.
`"USDH"` on some HIP-3 dexes): the report's totals are of the USDC trades, and those in
another currency are added up apart in `report.other_currencies`, never into USDC.
`report.fees_other` holds fees paid in a token other than their trade's currency.
`report.truncated` and `truncated_reason` say whether Hyperliquid's limits may have cut the
fills, as in `get_fill_history`. `spot=True` adds each spot market's closed PnL and its fees
by token.

An order of `get_historical_orders` is listed once for each status Hyperliquid kept for it
(seen: once as `open` when placed, once with its final status); the list is not a complete
log of status changes.

**An `Order`** has `order_type`, an `OrderType` with one of `limit` (e.g. `{"tif": "Gtc"}`),
`trigger` (`triggerPx`, `isMarket`, `tpsl`) or `market` set; `type` (`"limit"`,
`"stop_loss"`, `"take_profit"`); `size` (as placed), `filled_size` and `remaining_size`;
and `is_active` (open) and `is_filled`.

**Filled or shrunk.** Hyperliquid shrinks a resting reduce-only order (a take-profit limit, a
stop) to the position when the position gets smaller, and reports only the size placed and the
size still open. So for such an order with less open than placed, `filled_size` (their
difference) can be a shrink, not fills. `Order.filled_size_known` is then `False`; the order's
fills tell how much filled. An order returned by placing or modifying it has `created_at`
`None`: Hyperliquid stamps the block time, which its answer doesn't carry.

## Bulk Orders

```python
import secrets

# Place several orders in one action; a cloid per order is optional
result = client.bulk_order([
    {"symbol": "BTC", "is_buy": True,  "size": 0.001, "limit_price": 80000.0, "cloid": "0x" + secrets.token_hex(16)},
    {"symbol": "ETH", "is_buy": True,  "size": 0.01,  "limit_price": 3000.0},
    {"symbol": "BTC", "is_buy": False, "size": 0.001, "limit_price": 90000.0, "reduce_only": True},
])
# Hyperliquid's answer, one status per order: {"resting": {"oid": ...}}, {"filled": {...}},
# or {"error": "..."} for an order it rejected (the others are placed). A single error for the
# whole batch (an error of the payload itself) raises OrderRejectedException: nothing was placed.
statuses = result["response"]["data"]["statuses"]

# Cancel several orders in one action. Raises OrdersNotCanceledException naming each order the
# exchange did not cancel; one that already filled or was canceled counts as done.
client.bulk_cancel([
    {"symbol": "BTC", "order_id": 12345},
    {"symbol": "ETH", "order_id": 67890},
])
```

## Extended Universe (Stocks, Commodities, Forex)

Hyperliquid's extended perp universe includes stocks, commodities, indices, and forex, all prefixed with `xyz:`. Enable it with `extended_universe=True`. Off by default to avoid extra API overhead for users who only trade crypto.

```python
client = HyperliquidClient(extended_universe=True)

# Prices
tsla = client.get_price("xyz:TSLA")
gold = client.get_price("xyz:GOLD")
sp500 = client.get_price("xyz:SP500")

# Trading: the same API as crypto
order = client.buy("xyz:TSLA", size=1.0)
order = client.sell("xyz:GOLD", size=0.5, limit_price=4500.0)
client.set_leverage("xyz:NVDA", 10)
client.close("xyz:TSLA")

# Market info and order book
info = client.get_market_info("xyz:TSLA")
book = client.get_order_book("xyz:GOLD")
```

Available symbols include `xyz:TSLA`, `xyz:NVDA`, `xyz:GOLD`, `xyz:SILVER`, `xyz:SP500`, `xyz:EUR`, `xyz:BRENTOIL`, and many more. Use `client.get_price()` to see all available symbols.

### Other HIP-3 dexes (`perp_dexs`)

`xyz` is one of several builder-deployed perp dexes (HIP-3). Name the ones a client should read and trade with `perp_dexs`, as the SDK's `Info(perp_dexs=...)` takes them; the default dex is always included. Their markets carry the dex as prefix, and every method works with them as with `xyz:`:

```python
client = HyperliquidClient(perp_dexs=["xyz", "para", "io"])

client.get_perp_dexs()            # the network's dexes: [{"name": "xyz", "full_name": "XYZ"}, ...]
client.get_leverage("BTC")         # Leverage(type="cross", value=Decimal("20")): as set for the account
client.get_fee_rates("xyz:NVDA")   # FeeRates(taker=Decimal("0.00009"), maker=...): the wallet's, per market
client.get_price("para:TOTAL2")
order = client.buy("io:ANTH", size=0.01)
```

`extended_universe=True` is the same as `perp_dexs=["xyz"]`; pass one or the other. A dex the network does not have raises `InvalidArgumentException` (testnet has other dexes than mainnet). Each dex costs requests: creating a client reads its metadata, and positions, open orders, `get_price()` without a symbol, `get_markets()` and `cancel_all_orders()` read every dex of the client. A symbol's price and open orders read only its dex. Each HIP-3 dex has its own collateral token, named in the market specs as `collateral`: USDC on most, a USD stablecoin such as USDH, USDE or USDT0 on some (e.g. `flx`, `hyna`, `cash`, November 2026). In a standard account, move it to the dex before trading there; in a unified account the spot balance is the collateral of every dex.

## Market Data

### Candles
```python
candles = client.get_candles("BTC", "15m", lookback=96)          # the last 24 hours
daily = client.get_candles("xyz:SPCX", "1d", start_time=1793491200000)  # since 2026-11-01
last = candles[-1]   # Candle(open_time, close_time, open, high, low, close, volume, trades)
```

Every interval Hyperliquid has (`1m` to `1M`); one request. Hyperliquid keeps only the most
recent 5000 candles of each interval. The last candle may still be forming (its `close_time`
is in the future).

### Order Book
```python
order_book = client.get_order_book("BTC")
print(f"Best bid: ${order_book['best_bid']:,.2f}")
print(f"Best ask: ${order_book['best_ask']:,.2f}")
print(f"Spread: ${order_book['spread']:,.2f}")

# Prices, sizes, spread and mid as Decimal, exactly as Hyperliquid sends them (also get_spot_order_book)
order_book = client.get_order_book("BTC", decimal=True)
```

By default the figures are floats, which can lose digits of large, fine sizes.

### Optimal Limit Pricing
```python
# urgency_factor: 0.0 = patient (at best bid/ask), 1.0 = aggressive (crosses spread)
patient_price = client.get_optimal_limit_price("BTC", "buy", urgency_factor=0.1)
aggressive_price = client.get_optimal_limit_price("BTC", "buy", urgency_factor=0.9)
```

### Funding Rates
```python
# Current predicted funding rate
btc_rate = client.get_funding_rates("BTC")  # Returns float

# All rates sorted by value
all_rates = client.get_funding_rates()  # Returns List[{"symbol": str, "funding_rate": float}]

# Filter by threshold
high_rates = client.get_funding_rates(threshold=0.0001)  # Only |rate| >= threshold

# Historical funding rates of a market
history = client.get_funding_history("BTC", start_time=1700000000000)
# Returns List[{"time": int, "funding_rate": float, "premium": float}]

# Funding your account paid or received (every dex), oldest first
for payment in client.get_user_funding(start_time=1700000000000):
    print(payment.time, payment.symbol, payment.usdc)  # usdc: positive received, negative paid
```

### Portfolio Performance
```python
portfolio = client.get_portfolio()
# or for any address:
portfolio = client.get_portfolio("0x...")
```

### Market Kinds
Hyperliquid names markets differently by kind. `coin_market()` tells them apart, and every `Order` and `Fill` carries it as `market`:
```python
from fractrade_hl_simple import coin_market

coin_market("BTC")        # "perp"
coin_market("xyz:NVDA")   # "hip3_perp"
coin_market("@107")       # "spot" (also "PURR/USDC")
coin_market("#12")        # "outcome" (HIP-4)

perp_fills = [f for f in client.get_fills() if f.market in ("perp", "hip3_perp")]
```

### Market Info
```python
markets = client.get_market_info()       # All markets
btc_info = client.get_market_info("BTC") # Specific market

# The markets are loaded once, when the client is created (if that fails, the client is not
# created, as in the SDK); no call reloads them on its own. In a long-running program, reload
# them when markets were listed (they are then tradeable):
client.refresh_market_specs()

# Every perp market with its current figures (two requests per dex)
for market in client.get_markets():
    if not market.delisted:
        print(market.symbol, market.mark_price, market.funding, market.open_interest, market.margin_mode)
```

`get_markets()` returns `Market` objects: symbol, dex, size decimals, max leverage, margin mode (`cross`, or `no_cross`/`strict_isolated` for isolated-only markets), whether the market is delisted or at its open interest cap, and the mark, mid, oracle and previous-day price, hourly funding, premium, open interest and 24h volume as `Decimal` (None where Hyperliquid has no figure, e.g. no mid price without orders).

## Error Handling

The library's own exceptions are `HyperliquidException`s, each with a fixed `code` and `retryable`. `retryable` means the same call can succeed later; the library has already retried what it can. Where an exception replaces a type raised before, it also is that type, so existing `except` blocks keep working.

| Exception | `code` | Retryable | Also a | When |
|---|---|---|---|---|
| `InvalidArgumentException` | `INVALID_ARGUMENT` | no | `ValueError` | an argument is invalid |
| `UnknownSymbolException` | `UNKNOWN_SYMBOL` | no | `ValueError` | no market has this symbol |
| `AddressRequiredException` | `ADDRESS_REQUIRED` | no | `ValueError`, `RuntimeError` | a read about a wallet got no address, and the client has no account |
| `RateLimitException` | `RATE_LIMITED` | yes | | Hyperliquid's rate limit, after the retries |
| `NetworkException` | `NETWORK_ERROR` | yes | `requests.ConnectionError` | Hyperliquid could not be reached, after the retries |
| `TimeoutException` | `TIMEOUT` | yes | `requests.Timeout` | Hyperliquid did not answer in time, after the retries |
| `ServerErrorException` | `SERVER_ERROR` | yes | the SDK's `ServerError` | a server error or a response that is not JSON, after the retries |
| `ClientErrorException` | `REQUEST_REJECTED` | no | the SDK's `ClientError` | Hyperliquid rejected the request (an HTTP 4xx other than the rate limit) |
| `UnsupportedAccountModeException` | `UNSUPPORTED_ACCOUNT_MODE` | no | | no single balance for portfolio-margin accounts and unknown account modes |
| `PositionNotFoundException` | `POSITION_NOT_FOUND` | no | | no position for the symbol |
| `PositionNotClosedException` | `POSITION_NOT_CLOSED` | no | | after `close()` the position is still (partly) open: its order filled only part (`order`), or was rejected (`order` is None, `reason`); `remaining` is still open |
| `MakerOrderIncompleteException` | `MAKER_ORDER_INCOMPLETE` | no | | `maker_order()` stopped on an error after part of the size filled; `filled_size`, `average_price` and `remaining` say how much, the error is `__cause__` |
| `ClosePositionsIncompleteException` | `CLOSE_POSITIONS_INCOMPLETE` | no | | `close_all_positions()` did not close every position; `closed` and `failed` per symbol |
| `AuthenticationRequiredException` | `AUTHENTICATION_REQUIRED` | no | `RuntimeError` | the method signs an action, but the client has no account (private key) |
| `ActionRejectedException` | `ACTION_REJECTED` | no | `ValueError` | the exchange refused the action (leverage, margin, transfer); `.reason` is Hyperliquid's text |
| `OrderException` | `ORDER_ERROR` | no | | base for order errors, e.g. a cancel the exchange did not confirm |
| `OrderRejectedException` | `ORDER_REJECTED` | no | `ValueError` | the exchange refused the order or the modify; `.reason` is Hyperliquid's text (API docs, "Error responses") |
| `InsufficientMarginException` | `INSUFFICIENT_MARGIN` | no | `ValueError` | the exchange refused the order for lack of margin ("Insufficient margin to place order.") |
| `OrderNotFoundException` | `ORDER_NOT_FOUND` | no | | not raised by the library, kept so existing imports work: `cancel_order()` returns `False` for an order no longer open, `get_open_order_by_id()` returns `None`, `get_order_status()` returns Hyperliquid's answer |
| `OrdersNotCanceledException` | `ORDERS_NOT_CANCELED` | no | | `cancel_all_orders()`/`spot_cancel_all_orders()`/`bulk_cancel()`: the exchange did not cancel some orders; `.failed` names each (`symbol`, `order_id`, `reason`) |
| `ActionOutcomeUnknownException` | `OUTCOME_UNKNOWN` | no | | sent, but unknown whether it was executed (no answer, or one in no documented format): check the account before acting again |
| `OrderOutcomeUnknownException` | `OUTCOME_UNKNOWN` | no | | the same for an order; look it up with `get_order_status_by_cloid(e.cloid)` |
| `ProtectionOrderException` | `PROTECTION_NOT_PLACED` | no | | `open_*_position`: entry placed, stop loss/take profit not (`.orders`) |
| `ProtectionReplacementIncompleteException` | `PROTECTION_REPLACEMENT_INCOMPLETE` | no | | `update_stop_loss`/`update_take_profit`: an old order is still open, or the new one does not cover the whole position (`.position`) |
| `ProtectionSyncIncompleteException` | `PROTECTION_SYNC_INCOMPLETE` | no | | `resize_protection`: the protection asked for could not be placed or confirmed (also when every level was kept but the check afterwards failed); `.placed`, `.kept`, `.canceled`, `.retained`, `.not_canceled`, `.missing`, `.position` say what happened |
| `UnauthorizedException` | `UNAUTHORIZED` | no | | not raised by the library, kept so existing imports work (a missing key is `AuthenticationRequiredException`) |
| `ConfigurationException` | `CONFIGURATION_ERROR` | no | | invalid configuration |
| `HyperliquidException` | `HYPERLIQUID_ERROR` | no | | base class; also a failure Hyperliquid reports that has no exception of its own |

Every exception is also its base class: `TimeoutException` is a `NetworkException` (so also a `requests.ConnectionError`), `UnknownSymbolException` an `InvalidArgumentException`, and every order exception an `OrderException`; `OrderRejectedException` is also an `ActionRejectedException`, `InsufficientMarginException` an `OrderRejectedException`. Exceptions can be copied and pickled (multiprocessing, task queues) with their message and attributes.

```python
from fractrade_hl_simple import HyperliquidException, PositionNotFoundException

try:
    client.close("BTC")
except PositionNotFoundException:
    print("No BTC position to close")
except HyperliquidException as e:
    print(f"{e.code}: {e}" + (" (try again later)" if e.retryable else ""))
```

Some failures keep a plain type: `maker_order` raises `RuntimeError` when it placed no order, a market list without any market raises `RuntimeError`, and `HyperliquidAccount.from_key()` / `from_env()` raise `ValueError` for a missing key (`from_env` also for a missing address). An answer from Hyperliquid in a shape the library does not know can also end in an ordinary Python exception.

A read never hides a failure: `get_open_orders()` raises on an API failure instead of returning an empty list.

## Logging

```python
import logging

# Show info-level logs
logging.basicConfig(level=logging.INFO)

# For detailed debugging (API calls, order details, retry attempts)
logging.basicConfig(level=logging.DEBUG)
```

All logs are under the `fractrade_hl_simple` logger.

## Using the API Module

The common client methods are also available as standalone functions, with the same parameters as the method (pass `client=` to reuse a client, else each call creates one for itself):

```python
from fractrade_hl_simple import buy, get_price, get_fills, set_leverage

price = get_price("BTC")
order = buy("BTC", 0.001)
fills = get_fills("BTC")
set_leverage("BTC", 10)
```

## Complete Example

```python
from fractrade_hl_simple import HyperliquidClient

client = HyperliquidClient(default_slippage=0.03, max_retries=3)

# Check balance
balance = client.get_perp_balance()
print(f"Balance: ${float(balance):,.2f}")

# Set leverage
client.set_leverage("BTC", 10)

# Open long with TP/SL
price = client.get_price("BTC")
position = client.open_long_position(
    "BTC", size=0.001,
    stop_loss_price=price * 0.95,
    take_profit_price=price * 1.10,
)
print(f"Entry: {position['entry'].order_id}")

# Monitor fills
fills = client.get_fills("BTC")
for fill in fills[:5]:
    print(f"  {fill.direction} {fill.size} @ {fill.price}")

# Close when done
client.close("BTC")
```

## MCP Server (Claude Code, Claude Desktop)

`fractrade-hl-mcp` lets an AI assistant read your Hyperliquid accounts and the markets:
balances, positions, orders, fills, funding and prices, for several wallets and any public
address. It runs locally over stdio and is read only by default: it loads no private key
and has no tool that trades. With trading switched on (`--enable-trading`) for an account,
it also trades perps, within limits you set per account.

**New to this? Follow the step-by-step guide: [docs/mcp-setup.md](https://github.com/fractrade-xyz/fractrade-hl-simple/blob/main/docs/mcp-setup.md).**

```bash
pipx install "fractrade-hl-simple[mcp]"                        # or pip, in a virtual environment
fractrade-hl-mcp --init                                        # create your config file
claude mcp add --scope user fractrade -- fractrade-hl-mcp     # Claude Code
```

Configuration (example: [examples/mcp.example.toml](https://github.com/fractrade-xyz/fractrade-hl-simple/blob/main/examples/mcp.example.toml)), the tools, trading and its limits, errors and security: [docs/mcp.md](https://github.com/fractrade-xyz/fractrade-hl-simple/blob/main/docs/mcp.md).

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT

## Disclaimer

This software is provided as-is. Use at your own risk. The authors take no responsibility for any financial losses incurred while using this software. Always test thoroughly on testnet before trading with real funds.
