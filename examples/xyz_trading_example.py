"""
Example showing how to trade extended universe (xyz:) symbols —
stocks, commodities, indices, and forex on Hyperliquid.

Works with or without authentication. Price queries are public;
trading requires credentials in .env.
"""

from fractrade_hl_simple import HyperliquidClient


def main():
    client = HyperliquidClient(extended_universe=True)

    # ── Price queries (no auth required) ──────────────────────────────

    # Get a single xyz symbol price
    tsla = client.get_price("xyz:TSLA")
    gold = client.get_price("xyz:GOLD")
    sp500 = client.get_price("xyz:SP500")
    print(f"TSLA:  ${tsla:,.2f}")
    print(f"GOLD:  ${gold:,.2f}")
    print(f"SP500: ${sp500:,.2f}")

    # Get all prices — includes both crypto and xyz symbols
    all_prices = client.get_price()
    xyz_prices = {k: v for k, v in all_prices.items() if k.startswith("xyz:")}
    print(f"\nTotal symbols: {len(all_prices)} ({len(xyz_prices)} extended universe)")

    # ── Market info ───────────────────────────────────────────────────

    tsla_info = client.get_market_info("xyz:TSLA")
    print(f"\nxyz:TSLA market info:")
    print(f"  Size decimals: {tsla_info['szDecimals']}")
    print(f"  Max leverage:  {tsla_info['maxLeverage']}x")

    # Market specs (cached, includes xyz symbols)
    specs = client.market_specs
    print(f"\nxyz:GOLD specs: {specs.get('xyz:GOLD')}")

    # ── Order book ────────────────────────────────────────────────────

    book = client.get_order_book("xyz:TSLA")
    print(f"\nxyz:TSLA order book:")
    print(f"  Best bid: ${book['best_bid']:,.2f}")
    print(f"  Best ask: ${book['best_ask']:,.2f}")
    print(f"  Spread:   ${book['spread']:,.4f}")

    # ── Trading (requires auth) ───────────────────────────────────────

    if not client.is_authenticated():
        print("\nSkipping trades — no credentials found.")
        print("Add HYPERLIQUID_PRIVATE_KEY and HYPERLIQUID_PUBLIC_ADDRESS to .env to trade.")
        return

    SYMBOL = "xyz:TSLA"
    SIZE = 1.0  # 1 share equivalent

    balance = client.get_perp_balance()
    print(f"\nBalance: ${float(balance):,.2f}")

    # Set leverage
    client.set_leverage(SYMBOL, 5)
    print(f"Set {SYMBOL} leverage to 5x")

    # Place a limit buy below market
    price = client.get_price(SYMBOL)
    limit = round(price * 0.99, 2)
    order = client.buy(SYMBOL, SIZE, limit_price=limit)
    print(f"Limit buy placed: {order}")

    # Cancel it
    client.cancel_all_orders(SYMBOL)
    print("Cancelled")

    # Market buy
    order = client.buy(SYMBOL, SIZE)
    print(f"Market buy filled: {order}")

    # Close
    close = client.close(SYMBOL)
    print(f"Closed: {close}")


if __name__ == "__main__":
    main()
