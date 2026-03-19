"""
Example showing how to inspect market specifications for trading pairs.

Market specs include size decimals (minimum position granularity) and
max leverage for each asset. These are fetched from the API on client
init and cached for 24 hours.
"""

from fractrade_hl_simple import HyperliquidClient


def main():
    client = HyperliquidClient()

    # Show specs for major trading pairs
    print("=== Market Specifications ===\n")
    majors = ["BTC", "ETH", "SOL", "DOGE", "kPEPE"]
    for symbol in majors:
        specs = client.market_specs.get(symbol)
        if specs:
            sz_dec = specs["size_decimals"]
            min_size = 1 if sz_dec == 0 else 1.0 / (10 ** sz_dec)
            print(f"{symbol:8s}  size_decimals={sz_dec}  min_size={min_size}  max_leverage={specs.get('max_leverage', '?')}x")

    # Show total number of available markets
    print(f"\nTotal markets available: {len(client.market_specs)}")

    # Force refresh from API
    print("\nRefreshing market specs from API...")
    specs = client.refresh_market_specs()
    print(f"Refreshed: {len(specs)} markets")


if __name__ == "__main__":
    main()
