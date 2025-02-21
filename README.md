# fractrade-hl-simple

A simple Python wrapper for the Hyperliquid DEX API, focusing on perpetual futures trading. This library is yet in idea stage and not all features are available yet. We are using it for our own trading platform on fractrade.xyz and will add features as we need them.

⚠️ **Warning**: This is an early version of the library. Use with caution and test thoroughly before trading with real funds. Not all features are available yet. 

## Installation

Using pip:
```bash
pip install fractrade-hl-simple
```

Using poetry:
```bash
poetry add fractrade-hl-simple
```

## Setup

1. Create a `.env` file in your project root:
```env
HYPERLIQUID_ENV=testnet  # or mainnet
HYPERLIQUID_PUBLIC_ADDRESS=your_public_address
HYPERLIQUID_PRIVATE_KEY=your_private_key
```

We recommend creating a seperate API key wallet in the Hyperliquid UI for automated trading. This API wallets have not withdrawal permissions. 

2. Initialize the client:
```python
from fractrade_hl_simple import HyperliquidClient

client = HyperliquidClient()
```

## Basic Usage

### Get Market Prices
```python
# Get single price
btc_price = client.get_price("BTC")
print(f"BTC price: ${btc_price:,.2f}")

# Get all prices
all_prices = client.get_price()
for symbol, price in all_prices.items():
    print(f"{symbol}: ${price:,.2f}")
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
    print(f"Position: {pos.symbol} Size: {float(pos.size):+.3f}")
```

### Place Orders

Market Buy:
```python
order = client.buy("BTC", size=0.001)  # Market buy 0.001 BTC
print(f"Order placed: {order.order_id}")
```

Limit Buy:
```python
current_price = client.get_price("BTC")
limit_price = current_price * 0.99  # 1% below market
order = client.buy("BTC", size=0.001, limit_price=limit_price)
print(f"Limit order placed: {order.order_id}")
```

Market Sell:
```python
order = client.sell("BTC", size=0.001)  # Market sell 0.001 BTC
print(f"Order placed: {order.order_id}")
```

### Close Position
```python
close_order = client.close("BTC")
print(f"Position closed with order: {close_order.order_id}")
```

### Cancel Orders
```python
# Cancel orders for specific symbol
client.cancel_all_orders("BTC")

# Cancel all orders across all symbols
client.cancel_all()
```

## Complete Trading Example

Here's a full example showing a basic trading flow:

```python
from fractrade_hl_simple import HyperliquidClient
import time

def main():
    client = HyperliquidClient()
    SYMBOL = "BTC"
    POSITION_SIZE = 0.001
    
    try:
        # Check current price
        price = client.get_price(SYMBOL)
        print(f"Current {SYMBOL} price: ${price:,.2f}")
        
        # Place limit buy order
        limit_price = price * 0.99  # 1% below market
        order = client.buy(SYMBOL, POSITION_SIZE, limit_price=limit_price)
        print(f"Limit order placed: {order.order_id}")
        
        time.sleep(2)
        
        # Cancel limit order if not filled
        client.cancel_all_orders(SYMBOL)
        
        # Open market position
        order = client.buy(SYMBOL, POSITION_SIZE)
        print(f"Position opened with order: {order.order_id}")
        
        # Check position
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == SYMBOL), None)
        if position:
            print(f"Current position: {float(position.size):+.3f} {position.symbol}")
            print(f"Entry price: ${float(position.entry_price):,.2f}")
            print(f"Unrealized PnL: ${float(position.unrealized_pnl):,.2f}")
        
        # Close position
        close_order = client.close(SYMBOL)
        print(f"Position closed with order: {close_order.order_id}")
        
    except Exception as e:
        print(f"Error: {str(e)}")
        # Cleanup
        client.cancel_all_orders(SYMBOL)
        client.close(SYMBOL)

if __name__ == "__main__":
    main()
```


## Contributing
Contributions are welcome! Please feel free to submit a Pull Request.

## License
MIT

## Disclaimer
This software is for educational purposes only. Use at your own risk. The authors take no responsibility for any financial losses incurred while using this software.
```
