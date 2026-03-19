"""
Example showing how to fetch and filter funding rates.

Funding rates indicate the cost of holding a position. Positive rates mean
longs pay shorts, negative rates mean shorts pay longs. Useful for
funding rate arbitrage and position cost monitoring.

No authentication required.
"""

from fractrade_hl_simple import HyperliquidClient


def main():
    client = HyperliquidClient()

    print("=== Funding Rates ===\n")

    # Get all funding rates sorted from highest to lowest
    all_rates = client.get_funding_rates()
    print(f"Total tokens with funding rates: {len(all_rates)}\n")

    # Show top 5 positive and bottom 5 negative
    positive = [r for r in all_rates if r["funding_rate"] > 0]
    negative = [r for r in all_rates if r["funding_rate"] < 0]

    print("Top 5 Positive (longs pay shorts):")
    for r in positive[:5]:
        print(f"  {r['symbol']:8s} {r['funding_rate']*100:+.4f}%")

    print("\nTop 5 Negative (shorts pay longs):")
    for r in negative[:5]:
        print(f"  {r['symbol']:8s} {r['funding_rate']*100:+.4f}%")

    # Get rate for specific symbols
    print("\nMajor coins:")
    for symbol in ["BTC", "ETH", "SOL"]:
        rate = client.get_funding_rates(symbol)
        print(f"  {symbol}: {rate*100:+.4f}%")

    # Filter by threshold (only high funding rates)
    print("\nHigh funding rates (|rate| > 0.01%):")
    high_rates = client.get_funding_rates(threshold=0.0001)
    if high_rates:
        for r in high_rates:
            print(f"  {r['symbol']:8s} {r['funding_rate']*100:+.4f}%")
    else:
        print("  None above threshold")

    # Historical funding rates
    print("\nBTC funding rate history (last 24h):")
    import time
    history = client.get_funding_history("BTC", start_time=int((time.time() - 86400) * 1000))
    for entry in history[-5:]:
        print(f"  rate={entry['funding_rate']*100:+.6f}%  premium={entry['premium']*100:+.6f}%")
    print(f"  ({len(history)} total entries)")


if __name__ == "__main__":
    main()
