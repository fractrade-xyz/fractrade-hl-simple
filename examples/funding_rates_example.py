#!/usr/bin/env python3
"""
Example demonstrating how to use the get_funding_rates function
from the HyperliquidClient.
"""

from fractrade_hl_simple import HyperliquidClient

def main():
    # Initialize client (no authentication needed for funding rates)
    client = HyperliquidClient()
    
    print("=== Hyperliquid Funding Rates Example ===\n")
    
    # Get all funding rates sorted from highest positive to lowest negative
    print("1. Getting all funding rates (sorted by value):")
    try:
        funding_rates = client.get_funding_rates()
        
        print(f"Found {len(funding_rates)} tokens with funding rates:")
        print("-" * 50)
        
        for i, rate_data in enumerate(funding_rates, 1):
            symbol = rate_data['symbol']
            rate = rate_data['funding_rate']
            rate_percent = rate * 100  # Convert to percentage
            
            # Color coding: positive rates in green, negative in red
            if rate > 0:
                print(f"{i:2d}. {symbol:8s}: +{rate_percent:6.4f}% (positive)")
            else:
                print(f"{i:2d}. {symbol:8s}: {rate_percent:6.4f}% (negative)")
        
        print("-" * 50)
        
        # Show top 5 positive and bottom 5 negative rates
        positive_rates = [r for r in funding_rates if r['funding_rate'] > 0]
        negative_rates = [r for r in funding_rates if r['funding_rate'] < 0]
        
        print(f"\nTop 5 Positive Funding Rates:")
        for i, rate_data in enumerate(positive_rates[:5], 1):
            print(f"  {i}. {rate_data['symbol']}: +{rate_data['funding_rate']*100:.4f}%")
        
        print(f"\nBottom 5 Negative Funding Rates:")
        for i, rate_data in enumerate(negative_rates[-5:], 1):
            print(f"  {i}. {rate_data['symbol']}: {rate_data['funding_rate']*100:.4f}%")
            
    except Exception as e:
        print(f"Error getting all funding rates: {e}")
    
    # Get funding rate for a specific symbol
    print(f"\n2. Getting funding rate for specific symbols:")
    symbols_to_check = ["BTC", "ETH", "SOL"]
    
    for symbol in symbols_to_check:
        try:
            rate = client.get_funding_rates(symbol)
            rate_percent = rate * 100
            
            if rate > 0:
                print(f"  {symbol}: +{rate_percent:.4f}% (positive)")
            else:
                print(f"  {symbol}: {rate_percent:.4f}% (negative)")
                
        except Exception as e:
            print(f"  {symbol}: Error - {e}")
    
    # Compare the unified approach
    print(f"\n3. Using unified get_funding_rates function:")
    try:
        btc_rate = client.get_funding_rates("BTC")
        print(f"  BTC funding rate: {btc_rate*100:.4f}%")
    except Exception as e:
        print(f"  Error getting BTC funding rate: {e}")

if __name__ == "__main__":
    main() 