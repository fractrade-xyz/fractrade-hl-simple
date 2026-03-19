"""
Example showing a complete trading flow: check price, place limit order,
cancel it, open market long, close it, open market short, close it.

Requires authentication (.env file with credentials).
"""

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

        # Check balance
        balance = client.get_perp_balance()
        print(f"Account balance: ${float(balance):,.2f}")

        # Check existing positions
        positions = client.get_positions()
        if positions:
            for pos in positions:
                print(f"Existing position: {pos}")
        else:
            print("No open positions")

        # Place a limit buy order slightly below market
        print(f"\nPlacing limit buy order...")
        limit_price = price * 0.99
        order = client.buy(SYMBOL, POSITION_SIZE, limit_price=limit_price)
        print(f"Limit order placed: {order}")

        time.sleep(2)

        # Cancel the limit order
        print("Cancelling limit order...")
        client.cancel_all_orders(SYMBOL)
        print("Cancelled")

        time.sleep(2)

        # Open a market long position
        print(f"\nOpening long position...")
        order = client.buy(SYMBOL, POSITION_SIZE)
        print(f"Filled: {order}")

        time.sleep(2)

        # Check position details
        position = next((p for p in client.get_positions() if p.symbol == SYMBOL), None)
        if position:
            print(f"Position: {position}")

        # Close the long position
        print("\nClosing long position...")
        close_order = client.close(SYMBOL)
        print(f"Closed: {close_order}")

        time.sleep(2)

        # Open and close a short position
        print(f"\nOpening short position...")
        order = client.sell(SYMBOL, POSITION_SIZE)
        print(f"Filled: {order}")

        time.sleep(2)

        print("Closing short position...")
        close_order = client.close(SYMBOL)
        print(f"Closed: {close_order}")

        time.sleep(2)

        # Final check
        positions = client.get_positions()
        if not positions:
            print("\nAll positions closed successfully")

    except Exception as e:
        print(f"\nError: {e}")
        try:
            client.cancel_all_orders(SYMBOL)
            client.close(SYMBOL)
        except:
            pass
        raise


if __name__ == "__main__":
    main()
