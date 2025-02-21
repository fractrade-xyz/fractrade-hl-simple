from fractrade_hl_simple import HyperliquidClient
from fractrade_hl_simple.models import get_current_market_specs, print_market_specs_diff

def main():
    # Initialize client
    client = HyperliquidClient()
    
    print("\nGetting current market specifications...")
    current_specs = get_current_market_specs(client.info)
    
    print("\nComparing with stored specifications:")
    print_market_specs_diff(current_specs)
    
    print("\nFull current specifications:")
    for symbol, specs in sorted(current_specs.items()):
        print(f"\n{symbol}:")
        for key, value in specs.items():
            print(f"  {key}: {value}")

if __name__ == "__main__":
    main()

# Example output:
# Changed market BTC:
#   size_decimals: 3 -> 4
# New market SOL: {'size_decimals': 1, 'price_decimals': 3, 'min_size': 0.1}


