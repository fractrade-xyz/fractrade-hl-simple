# fractrade-hl-simple

A simple Python wrapper for the Hyperliquid DEX API, focused on perpetual futures trading. Built for reliability with automatic retries, configurable slippage, and proper error handling — because this is real money.

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
    default_slippage=0.05,      # 5% default slippage for market orders (0.0-0.5)
    max_retries=3,              # Retry transient failures (0 to disable)
    retry_delay=1.0,            # Base delay between retries (exponential backoff)
    cache_market_specs=True,    # New clients reuse market specs loaded in the last 24 hours
    extended_universe=True,      # Enable stocks, commodities, indices, forex (xyz: symbols)
    request_timeout=10.0,       # Seconds to wait for each HTTP request
)
```

`base_url` takes the network the same way as the Hyperliquid SDK, and only its constants: `MAINNET_API_URL`, `TESTNET_API_URL` or `LOCAL_API_URL`. Any other URL, even with a trailing slash, raises `ValueError`, because the SDK would sign for testnet with it and mainnet would reject every order. `env="mainnet"` / `env="testnet"` still works but is deprecated.

**Retry logic**: Reads, cancels and leverage changes retry on network errors, timeouts, rate limits and server errors. Orders, modifies, transfers and margin changes are retried only when the request provably did not reach the exchange (rate limit, connect timeout). If an order's response is lost, the library looks the order up by its client order id instead of sending it again; if it cannot settle the outcome, it raises `OrderOutcomeUnknownException`. Auth errors and validation errors are never retried.

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
```

### 3. Without a Key: Market Data and Any Wallet
```python
reader = HyperliquidClient(trust_env=False)  # no key; no .env, key, address or HYPERLIQUID_ENV read

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
spot balance. For portfolio-margin accounts both raise `UnsupportedAccountModeException`; use
`get_spot_balance(simple=False)` and `get_user_state()` for the figures per token and per dex.

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
A figure that is not established for the mode is `None`: per-dex account value and withdrawable in unified accounts (the dex figures are only the part of the collateral backing its positions), `available_to_trade` in standard accounts (each dex has its own collateral), and every total for portfolio-margin accounts. Outcome balances (HIP-4) are listed, unpriced and left out of the totals.

### View Positions
```python
positions = client.get_positions()
for pos in positions:
    direction = "LONG" if pos.is_long else "SHORT"
    print(f"{pos.symbol} {direction} {float(pos.size):+.3f} @ ${float(pos.entry_price):,.2f}")
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
```

### Stop Loss and Take Profit

```python
# The side comes from the open position: sells for a long, buys for a short
client.stop_loss("BTC", size=0.001, trigger_price=80000.0)
client.take_profit("BTC", size=0.001, trigger_price=95000.0)

# is_buy is optional; a side that would not close the position raises ValueError
client.stop_loss("BTC", size=0.001, trigger_price=90000.0, is_buy=True)  # short position
```

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

Each level is placed once the entry fills, for its fraction of the **filled** size (rounded
down to the lot size; the stop loss and the take profit nearest the price take what rounding
left, so the levels cover exactly their share, and a partial fill never loses more at
its stops than the full size, before slippage). Prices on the wrong side of the entry, fractions
above 1, and levels worth less than Hyperliquid's 10 USD minimum are refused before anything
is sent. If the entry fills but a level cannot be placed, or executes at once because the
price had already crossed it, `ProtectionOrderException` carries the orders placed
(`orders`) and those missing (`missing`: leg, price, size; `outcome_unknown` and `cloid` for
one that may still arrive). `time_in_force="Ioc"` with `limit_price` makes a market entry with
a price limit; its levels are checked against the mid. So are those of a limit beyond the mid
(a long's above it, a short's below), as it can fill at once; those of a limit that rests are
checked against its price.

### Maker Orders (Fee-Optimized)

```python
# Simple maker buy/sell — auto-detects optimal timing from order book spread
order = client.maker_buy("BTC", 0.001)    # Tight spread → 30s timeout, 3s reprice
order = client.maker_sell("kPEPE", 1500)  # Medium spread → 45s timeout, 5s reprice

# Equivalent verbose form
order = client.maker_order("BTC", is_buy=True, size=0.001)

# Override auto-detection with manual timing
order = client.maker_buy("DYM", 30, timeout=60, reprice_interval=8, fallback="ioc")

# fallback options: "ioc" (default), "market", "cancel"
```

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
client.cancel_all_orders()        # Cancel all orders across all symbols
```

### Client Order IDs
Every single order (`buy`, `sell`, `stop_loss`, `take_profit`, `close`, spot and maker orders, ...) is placed with a client order id (`cloid`, `0x` and 32 hex digits): yours, or a random one. In `bulk_order()` only orders whose dict has a `cloid` carry one. `order.order_id` is Hyperliquid's id (a number), `order.cloid` the client order id; open orders have theirs too. An id you choose is known before the order is sent, so you can still find the order if its answer was lost (a crash, a timeout):
```python
import secrets

cloid = "0x" + secrets.token_hex(16)  # store it before sending
order = client.buy("BTC", 0.001, limit_price=80000, cloid=cloid)

client.get_order_status_by_cloid(cloid)   # also works when the answer to buy() never arrived
client.cancel_order_by_cloid(cloid, "BTC")
```

## Spot Trading

Trade spot tokens (e.g., PURR, HYPE, FRAC) using just the token name — the library handles the `/USDC` pair mapping internally.

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
```

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

# Check order status
status = client.get_order_status(order_id=12345)
```

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
# or {"error": "..."} for an order it rejected (the others are placed)
statuses = result["response"]["data"]["statuses"]

# Cancel multiple orders atomically
client.bulk_cancel([
    {"symbol": "BTC", "order_id": 12345},
    {"symbol": "ETH", "order_id": 67890},
])
```

## Extended Universe (Stocks, Commodities, Forex)

Hyperliquid's extended perp universe includes stocks, commodities, indices, and forex — all prefixed with `xyz:`. Enable it with `extended_universe=True`. Off by default to avoid extra API overhead for users who only trade crypto.

```python
# Enable extended universe (crypto + stocks/commodities/forex)
client = HyperliquidClient(extended_universe=True)

# Prices
tsla = client.get_price("xyz:TSLA")
gold = client.get_price("xyz:GOLD")
sp500 = client.get_price("xyz:SP500")

# Trading — same API as crypto
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
client.get_price("para:TOTAL2")   # one request: only the para dex's mids
order = client.buy("io:ANTH", size=0.01)
```

`extended_universe=True` is the same as `perp_dexs=["xyz"]`; pass one or the other. A dex the network does not have raises `InvalidArgumentException` (testnet has other dexes than mainnet). Each dex costs requests: creating a client reads its metadata, and positions, open orders, `get_price()` without a symbol, `get_markets()` and `cancel_all_orders()` read every dex of the client. A symbol's price and open orders read only its dex. Each HIP-3 dex has its own collateral (USDC on every dex with listed markets, November 2026): in a standard account, move it to the dex before trading there; in a unified account the spot balance is the collateral of every dex.

## Market Data

### Order Book
```python
order_book = client.get_order_book("BTC")
print(f"Best bid: ${order_book['best_bid']:,.2f}")
print(f"Best ask: ${order_book['best_ask']:,.2f}")
print(f"Spread: ${order_book['spread']:,.2f}")
```

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

Every failure is a `HyperliquidException` with a fixed `code` and `retryable`. `retryable` means the same call can succeed later; the library has already retried what it can. Where an exception replaces a type raised before, it also is that type, so existing `except` blocks keep working.

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
| `UnsupportedAccountModeException` | `UNSUPPORTED_ACCOUNT_MODE` | no | | no single balance for portfolio-margin accounts |
| `PositionNotFoundException` | `POSITION_NOT_FOUND` | no | | no position for the symbol |
| `PositionNotClosedException` | `POSITION_NOT_CLOSED` | no | | `close()` filled only part of the position; `order` and `remaining` say how much |
| `MakerOrderIncompleteException` | `MAKER_ORDER_INCOMPLETE` | no | | `maker_order()` stopped on an error after part of the size filled; `filled_size`, `average_price` and `remaining` say how much, the error is `__cause__` |
| `ClosePositionsIncompleteException` | `CLOSE_POSITIONS_INCOMPLETE` | no | | `close_all_positions()` did not close every position; `closed` and `failed` per symbol |
| `AuthenticationRequiredException` | `AUTHENTICATION_REQUIRED` | no | `RuntimeError` | the method signs an action, but the client has no account (private key) |
| `ActionRejectedException` | `ACTION_REJECTED` | no | `ValueError` | the exchange refused the action (leverage, margin, transfer); `.reason` is Hyperliquid's text |
| `OrderException` | `ORDER_ERROR` | no | | base for order errors, e.g. a cancel the exchange did not confirm |
| `OrderRejectedException` | `ORDER_REJECTED` | no | `ValueError` | the exchange refused the order or the modify; `.reason` is Hyperliquid's text (API docs, "Error responses") |
| `InsufficientMarginException` | `INSUFFICIENT_MARGIN` | no | `ValueError` | the exchange refused the order for lack of margin ("Insufficient margin to place order.") |
| `OrderNotFoundException` | `ORDER_NOT_FOUND` | no | | order not found (cancel/query) |
| `OrdersNotCanceledException` | `ORDERS_NOT_CANCELED` | no | | `cancel_all_orders()`/`spot_cancel_all_orders()`: the exchange did not cancel some orders; `.failed` names each (`symbol`, `order_id`, `reason`) |
| `ActionOutcomeUnknownException` | `OUTCOME_UNKNOWN` | no | | sent, but unknown whether it was executed (no answer, or one in no documented format): check the account before acting again |
| `OrderOutcomeUnknownException` | `OUTCOME_UNKNOWN` | no | | the same for an order; look it up with `get_order_status_by_cloid(e.cloid)` |
| `ProtectionOrderException` | `PROTECTION_NOT_PLACED` | no | | `open_*_position`: entry placed, stop loss/take profit not (`.orders`) |
| `ProtectionReplacementIncompleteException` | `PROTECTION_REPLACEMENT_INCOMPLETE` | no | | `update_stop_loss`/`update_take_profit`: an old order is still open, or the new one does not cover the whole position (`.position`) |
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

Two failures keep their plain type for existing handlers: `maker_order` raises `RuntimeError` when it placed no order, and a market list without any market raises `RuntimeError`.

Key error behaviors:
- `get_open_orders()` raises on API failure (never silently returns empty list)
- `cancel_all_orders()` raises if the exchange does not confirm the cancel of every order, reporting which orders failed; orders that already filled or were canceled count as done
- `cancel_order()` returns `False` if the order was never placed, already canceled or filled, raises on real errors
- `close()` raises `PositionNotClosedException` if the position is still (partly) open afterwards; `close_all_positions()` tries every position and raises `ClosePositionsIncompleteException` for the ones not closed

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

## MCP Server (Claude Code, Codex, Claude Desktop)

`fractrade-hl-mcp` lets an AI assistant read your Hyperliquid accounts and the markets:
balances, positions, orders, fills, funding and prices, the risk/reward of a position at its
stops and take profits and the size for a planned trade's risk, for several wallets and any public
address, on Hyperliquid's own perps and the HIP-3 dexes you enable (`xyz:`, `para:`, ...).
It runs locally over stdio and is read only by default: it loads no private key and has no
tool that trades unless trading is switched on (`--enable-trading`) for an account with
`trading = true`. Then it also places, changes and cancels perp orders, closes positions,
sets stop losses and take profits, and has a kill switch, within the account's limits:
for orders that add risk, optional risk guardrails (margin per order and in total as a
share of the account, a daily loss stop, an order rate, the market's leverage, isolated
margin only), an optional order value cap and market allowlist; slippage for market
orders and closes, leverage for `set_leverage`. Closing, stop losses and cancels are
exempt from the guardrails, the value cap and the allowlist.

**New to this? Follow the step-by-step guide: [docs/mcp-setup.md](docs/mcp-setup.md).**

```bash
pipx install "fractrade-hl-simple[mcp]"                        # or pip, in a virtual environment
fractrade-hl-mcp --init                                        # create your config file
claude mcp add --scope user fractrade -- fractrade-hl-mcp     # Claude Code
codex mcp add fractrade -- fractrade-hl-mcp                    # Codex CLI
```

Configuration (example: [examples/mcp.example.toml](examples/mcp.example.toml)), every tool and its fields, trading and its limits, errors and security: [docs/mcp.md](docs/mcp.md).

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT

## Disclaimer

This software is provided as-is. Use at your own risk. The authors take no responsibility for any financial losses incurred while using this software. Always test thoroughly on testnet before trading with real funds.
