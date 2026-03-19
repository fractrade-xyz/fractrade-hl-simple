"""
Example showing how to use order book data and optimal limit pricing.

The optimal limit price interpolates between the best bid and best ask
based on an urgency factor (0.0 = patient maker, 1.0 = aggressive taker).

No authentication required.
"""

from fractrade_hl_simple import HyperliquidClient


def main():
    client = HyperliquidClient()

    SYMBOL = "BTC"

    print(f"=== Order Book & Optimal Pricing for {SYMBOL} ===\n")

    # Get order book
    book = client.get_order_book(SYMBOL)
    print(f"Best bid:  ${book['best_bid']:,.2f}")
    print(f"Best ask:  ${book['best_ask']:,.2f}")
    print(f"Spread:    ${book['spread']:,.2f}")
    print(f"Mid price: ${book['mid_price']:,.2f}")

    # Show top 5 levels with order count
    print("\nTop 5 Bids:")
    for i, bid in enumerate(book["bids"][:5]):
        print(f"  {i+1}. ${bid['price']:>10,.2f}  size={bid['size']:.3f}  orders={bid['num_orders']}")

    print("\nTop 5 Asks:")
    for i, ask in enumerate(book["asks"][:5]):
        print(f"  {i+1}. ${ask['price']:>10,.2f}  size={ask['size']:.3f}  orders={ask['num_orders']}")

    # Show how urgency factor affects pricing
    print(f"\nOptimal buy prices at different urgency levels:")
    print(f"  {'Urgency':>8}  {'Price':>12}  {'Description'}")
    print(f"  {'-'*8}  {'-'*12}  {'-'*20}")

    levels = [
        (0.0, "at best bid (maker)"),
        (0.25, "quarter spread"),
        (0.5, "mid spread"),
        (0.75, "three-quarter spread"),
        (1.0, "at best ask (taker)"),
    ]

    for urgency, desc in levels:
        price = client.get_optimal_limit_price(SYMBOL, "buy", urgency_factor=urgency)
        print(f"  {urgency:>8.2f}  ${price:>10,.2f}  {desc}")

    print(f"\nOptimal sell prices:")
    for urgency, desc in levels:
        price = client.get_optimal_limit_price(SYMBOL, "sell", urgency_factor=urgency)
        print(f"  {urgency:>8.2f}  ${price:>10,.2f}  {desc}")


if __name__ == "__main__":
    main()
