from decimal import Decimal
import os
from fractrade_hl_simple import HyperliquidClient
import time



def main():
    # Initialize client
    client = HyperliquidClient()
    
    # Trading parameters
    SYMBOL = "BTC"
    POSITION_SIZE = 0.001  # Smaller size for BTC since it's more expensive
    
    try:
        # Get current price
        print(f"\nGetting {SYMBOL} price...")
        btc_price = client.get_price(SYMBOL)
        print(f"Current {SYMBOL} price: ${btc_price:,.2f}")
        
        time.sleep(1)
        
        # Get all available prices
        print("\nGetting all prices...")
        all_prices = client.get_price()
        for sym, current_price in all_prices.items():
            print(f"{sym}: ${current_price:,.2f}")
            
        time.sleep(1)
        
        # Check current positions before trading
        print("\nChecking current positions...")
        positions = client.get_positions()
        for pos in positions:
            print(f"Position: {pos.symbol} Size: {float(pos.size):+.3f}")
            
        time.sleep(1)
        
        # Place a limit buy order slightly below market
        print(f"\nPlacing limit buy order for {POSITION_SIZE} {SYMBOL}...")
        limit_price = btc_price * 0.99
        order = client.buy(SYMBOL, POSITION_SIZE, limit_price=limit_price)
        print(f"Limit order placed: {order.order_id}")
        
        time.sleep(2)
        
        # Cancel the limit order if not filled
        print("\nCancelling all orders...")
        client.cancel_all_orders(SYMBOL)
        print("Orders cancelled")
        
        time.sleep(2)
        
        # Open a market buy (long) position
        print(f"\nOpening long position for {POSITION_SIZE} {SYMBOL}...")
        order = client.buy(SYMBOL, POSITION_SIZE)  # No limit_price = market order
        print(f"Position opened with order: {order.order_id}")
            
        time.sleep(3)
        
        # Check the position
        print("\nChecking position details...")
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == SYMBOL), None)
        if position:
            print(f"Current position: {float(position.size):+.3f} {position.symbol}")
            print(f"Entry price: ${float(position.entry_price):,.2f}")
            print(f"Unrealized PnL: ${float(position.unrealized_pnl):,.2f}")
            
        time.sleep(2)
        
        # Close the long position
        print("\nClosing long position...")
        client.cancel_all_orders(SYMBOL)
        close_order = client.close(SYMBOL)
        print(f"Position closed with order: {close_order.order_id}")
        
        time.sleep(2)
        
        # Verify position is closed
        print("\nVerifying position closure...")
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == SYMBOL), None)
        if position:
            print(f"Remaining position: {float(position.size):+.3f} {position.symbol}")
        else:
            print("Position successfully closed")
        
        # Open a market sell (short) position
        print(f"\nOpening short position for {POSITION_SIZE} {SYMBOL}...")
        order = client.sell(SYMBOL, POSITION_SIZE)  # No limit_price = market order
        print(f"Short position opened with order: {order.order_id}")
            
        time.sleep(3)
        
        # Close short position
        print("\nClosing short position...")
        client.cancel_all_orders(SYMBOL)
        close_order = client.close(SYMBOL)
        print(f"Short position closed with order: {close_order.order_id}")
        
        time.sleep(2)
        
        # Final position check
        print("\nFinal position check...")
        positions = client.get_positions()
        if not positions:
            print("All positions closed successfully")
        else:
            for pos in positions:
                print(f"Remaining position: {float(pos.size):+.3f} {pos.symbol}")
                
    except Exception as e:
        print(f"\nError occurred: {str(e)}")
        # Try to close any open positions on error
        try:
            client.cancel_all_orders(SYMBOL)
            client.close(SYMBOL)
        except:
            pass
        raise

if __name__ == "__main__":
    main() 