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
client = HyperliquidClient(
    env="mainnet",              # "mainnet" or "testnet"
    default_slippage=0.05,      # 5% default slippage for market orders (0.0-0.5)
    max_retries=3,              # Retry transient failures (0 to disable)
    retry_delay=1.0,            # Base delay between retries (exponential backoff)
    cache_market_specs=True,    # Cache market specs across instances (24h TTL)
    extended_universe=True,      # Enable stocks, commodities, indices, forex (xyz: symbols)
)
```

**Retry logic**: All API calls automatically retry on network errors, timeouts, rate limits, and server errors. Auth errors and validation errors are never retried.

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

### 3. Unauthenticated (Public Endpoints Only)
```python
client = HyperliquidClient()  # Falls back if no credentials found
```

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
```

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
```

### Stop Loss and Take Profit

```python
# For long positions (is_buy=False by default — sells when triggered)
client.stop_loss("BTC", size=0.001, trigger_price=80000.0)
client.take_profit("BTC", size=0.001, trigger_price=95000.0)

# For short positions (must set is_buy=True — buys when triggered)
client.stop_loss("BTC", size=0.001, trigger_price=90000.0, is_buy=True)
client.take_profit("BTC", size=0.001, trigger_price=75000.0, is_buy=True)
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
```

### Close Position
```python
close_order = client.close("BTC")

# close() warns if the IOC order doesn't fill (position may still be open)
if close_order.status != "filled":
    print("Warning: close order was not filled!")
```

### Cancel Orders
```python
client.cancel_order(order_id=12345, symbol="BTC")  # Accepts str or int
client.cancel_all_orders("BTC")   # Cancel all BTC orders
client.cancel_all_orders()        # Cancel all orders across all symbols
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
fills = client.get_fills()                    # All symbols
fills = client.get_fills("BTC")               # BTC only

for fill in fills:
    print(f"{fill.symbol} {fill.direction} {fill.size} @ {fill.price} pnl={fill.closed_pnl}")

# Get fills in a time range (timestamps in milliseconds)
import time
start = int((time.time() - 86400) * 1000)  # 24 hours ago
fills = client.get_fills_by_time(start_time=start)

# Check order status
status = client.get_order_status(order_id=12345)
```

## Bulk Orders

```python
# Place multiple orders atomically
result = client.bulk_order([
    {"symbol": "BTC", "is_buy": True,  "size": 0.001, "limit_price": 80000.0},
    {"symbol": "ETH", "is_buy": True,  "size": 0.01,  "limit_price": 3000.0},
    {"symbol": "BTC", "is_buy": False, "size": 0.001, "limit_price": 90000.0, "reduce_only": True},
])

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

# Historical funding rates
history = client.get_funding_history("BTC", start_time=1700000000000)
# Returns List[{"time": int, "funding_rate": float, "premium": float}]
```

### Portfolio Performance
```python
portfolio = client.get_portfolio()
# or for any address:
portfolio = client.get_portfolio("0x...")
```

### Market Info
```python
markets = client.get_market_info()       # All markets
btc_info = client.get_market_info("BTC") # Specific market

# Refresh market specs (cached for 24 hours, but can be forced)
client.refresh_market_specs()
```

## Error Handling

The library provides specific exceptions for different failure modes:

```python
from fractrade_hl_simple import (
    HyperliquidException,        # Base — catch all library errors
    PositionNotFoundException,   # No position found for symbol
    OrderNotFoundException,      # Order not found (cancel/query)
    InsufficientMarginException, # Not enough margin
    OrderException,              # Base for order-related errors
)

try:
    client.close("BTC")
except PositionNotFoundException:
    print("No BTC position to close")
except HyperliquidException as e:
    print(f"Hyperliquid error: {e}")
```

Key error behaviors:
- `get_open_orders()` raises on API failure (never silently returns empty list)
- `cancel_all_orders()` raises if any cancellation fails, reporting which orders failed
- `cancel_order()` returns `False` if order not found, raises on real errors
- `close()` logs a warning if the IOC order doesn't fill

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

All client methods are also available as standalone functions:

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

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT

## Disclaimer

This software is provided as-is. Use at your own risk. The authors take no responsibility for any financial losses incurred while using this software. Always test thoroughly on testnet before trading with real funds.
