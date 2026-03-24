"""
Example showing spot trading: check price, buy tokens, place limit order,
cancel it, sell tokens.

Requires authentication (.env file with credentials).
Requires USDC in the spot wallet — transfer via the web UI or use
transfer_to_spot() with the main wallet key.
"""

from fractrade_hl_simple import HyperliquidClient
import time


def main():
    client = HyperliquidClient()

    TOKEN = "FRAC"
    SIZE = 500  # 500 FRAC tokens

    try:
        # Check spot balance
        balance = client.get_spot_balance(simple=False)
        print(f"Spot balance: ${float(balance.total_balance):,.2f}")
        for tok, b in balance.tokens.items():
            print(f"  {tok}: {float(b.amount):,.2f}")

        # Check price
        price = client.get_spot_price(TOKEN)
        print(f"\n{TOKEN} spot price: ${price:.6f}")

        # Market buy
        print(f"\nBuying {SIZE} {TOKEN}...")
        order = client.spot_buy(TOKEN, SIZE)
        print(f"Filled: {order}")

        time.sleep(2)

        # Check updated balance
        balance = client.get_spot_balance(simple=False)
        if TOKEN in balance.tokens:
            print(f"{TOKEN} balance: {float(balance.tokens[TOKEN].amount):,.1f}")

        # Place a limit sell above market (won't fill)
        limit_price = round(price * 1.5, 6)
        print(f"\nPlacing limit sell at ${limit_price}...")
        order = client.spot_sell(TOKEN, SIZE, limit_price=limit_price)
        print(f"Limit order: {order}")

        time.sleep(2)

        # Check open orders
        open_orders = client.get_spot_open_orders(TOKEN)
        print(f"\nOpen {TOKEN} orders: {len(open_orders)}")
        for o in open_orders:
            print(f"  {o}")

        # Cancel all spot orders
        print("\nCancelling all spot orders...")
        client.spot_cancel_all_orders(TOKEN)
        print("Cancelled")

        time.sleep(2)

        # Market sell
        print(f"\nSelling {SIZE} {TOKEN}...")
        order = client.spot_sell(TOKEN, SIZE)
        print(f"Filled: {order}")

        # Final balance
        balance = client.get_spot_balance(simple=False)
        print(f"\nFinal spot balance: ${float(balance.total_balance):,.2f}")

    except Exception as e:
        print(f"\nError: {e}")
        try:
            client.spot_cancel_all_orders(TOKEN)
        except:
            pass
        raise


if __name__ == "__main__":
    main()
