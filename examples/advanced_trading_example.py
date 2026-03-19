"""
Advanced trading example showing leverage, custom slippage, bulk orders,
order tracking, and fill history.

Requires authentication (.env file with credentials).
"""

from fractrade_hl_simple import HyperliquidClient, PositionNotFoundException
import time


def main():
    # Initialize with custom retry and slippage settings
    client = HyperliquidClient(
        default_slippage=0.03,  # 3% default slippage for market orders
        max_retries=3,          # retry transient failures up to 3 times
        retry_delay=1.0,        # 1 second base delay between retries
    )

    SYMBOL = "BTC"
    SIZE = 0.001

    try:
        balance = client.get_perp_balance()
        print(f"Balance: ${float(balance):,.2f}")

        # Set leverage before trading
        print(f"\n--- Leverage ---")
        client.set_leverage(SYMBOL, 20, is_cross=True)
        print(f"Set {SYMBOL} to 20x cross leverage")

        # Open position with default slippage (3%)
        print(f"\n--- Market Buy (3% slippage) ---")
        order = client.buy(SYMBOL, SIZE)
        print(f"Filled: {order}")
        time.sleep(2)

        # Check the fill details
        print(f"\n--- Fill Details ---")
        fills = client.get_fills(SYMBOL)
        print(f"Total {SYMBOL} fills: {len(fills)}")
        if fills:
            latest = fills[0]
            print(f"Latest: {latest}")
            print(f"  Fee: {latest.fee}")

        # Query order status
        print(f"\n--- Order Status ---")
        status = client.get_order_status(int(order.order_id))
        print(f"Order {order.order_id}: {status['order']['status']}")

        # Set TP/SL
        print(f"\n--- TP/SL ---")
        position = next(p for p in client.get_positions() if p.symbol == SYMBOL)
        entry = float(position.entry_price)

        sl = client.stop_loss(SYMBOL, SIZE, entry * 0.985)
        tp = client.take_profit(SYMBOL, SIZE, entry * 1.015)
        print(f"SL: {sl}")
        print(f"TP: {tp}")
        time.sleep(1)

        # Cancel TP/SL and close position
        client.cancel_all_orders(SYMBOL)
        time.sleep(1)
        close = client.close(SYMBOL)
        print(f"\nClosed: {close}")
        time.sleep(2)

        # Bulk orders: place multiple limit orders at once
        print(f"\n--- Bulk Orders ---")
        price = client.get_price(SYMBOL)
        result = client.bulk_order([
            {"symbol": SYMBOL, "is_buy": True,  "size": SIZE, "limit_price": price * 0.90},
            {"symbol": SYMBOL, "is_buy": True,  "size": SIZE, "limit_price": price * 0.85},
            {"symbol": SYMBOL, "is_buy": False, "size": SIZE, "limit_price": price * 1.10},
        ])
        print(f"Placed 3 limit orders: {result['status']}")
        time.sleep(1)

        # Verify they're open
        orders = client.get_open_orders(SYMBOL)
        print(f"Open orders: {len(orders)}")
        for o in orders:
            print(f"  {o}")

        # Bulk cancel
        cancels = [{"symbol": SYMBOL, "order_id": o.order_id} for o in orders]
        result = client.bulk_cancel(cancels)
        print(f"Bulk cancelled: {result['status']}")

        # Market buy with tighter slippage override
        print(f"\n--- Custom Slippage (1%) ---")
        order = client.buy(SYMBOL, SIZE, slippage=0.01)
        print(f"Filled: {order}")
        time.sleep(1)

        # Close
        close = client.close(SYMBOL)
        print(f"Closed: {close}")
        time.sleep(2)

        # Check fills from the last few minutes
        print(f"\n--- Recent Fills ---")
        recent = client.get_fills_by_time(
            start_time=int((time.time() - 300) * 1000),
            symbol=SYMBOL,
        )
        print(f"Fills in last 5 minutes: {len(recent)}")
        for fill in recent:
            print(f"  {fill}")

        # Final balance
        final = client.get_perp_balance()
        print(f"\nFinal balance: ${float(final):,.2f}")

    except Exception as e:
        print(f"\nError: {e}")
        try:
            client.cancel_all_orders(SYMBOL)
            client.close(SYMBOL)
        except (PositionNotFoundException, Exception):
            pass
        raise


if __name__ == "__main__":
    main()
