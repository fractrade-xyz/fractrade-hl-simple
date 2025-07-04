#!/usr/bin/env python3
"""
Example demonstrating optimal limit order pricing based on order book analysis.

This example shows how to:
1. Get current order book data
2. Calculate optimal limit order prices based on urgency factors
3. Use funding rates to adjust urgency
4. Implement a loop to increase urgency until order gets filled
"""

from fractrade_hl_simple import HyperliquidClient
import time

def main():
    # Initialize client
    client = HyperliquidClient()
    
    # Trading parameters
    SYMBOL = "BTC"
    ORDER_SIZE = 0.001  # Small size for demonstration
    
    print(f"=== Optimal Limit Order Pricing Example for {SYMBOL} ===\n")
    
    try:
        # 1. Get current order book
        print("1. Getting current order book...")
        order_book = client.get_order_book(SYMBOL)
        
        print(f"   Best bid: ${order_book['best_bid']:,.2f}")
        print(f"   Best ask: ${order_book['best_ask']:,.2f}")
        print(f"   Spread: ${order_book['spread']:,.2f}")
        print(f"   Mid price: ${order_book['mid_price']:,.2f}")
        print(f"   Number of bid levels: {len(order_book['bids'])}")
        print(f"   Number of ask levels: {len(order_book['asks'])}")
        
        # Show top 5 bids and asks
        print("\n   Top 5 Bids:")
        for i, bid in enumerate(order_book['bids'][:5]):
            print(f"     {i+1}. ${bid['price']:,.2f} - {bid['size']:.3f}")
            
        print("\n   Top 5 Asks:")
        for i, ask in enumerate(order_book['asks'][:5]):
            print(f"     {i+1}. ${ask['price']:,.2f} - {ask['size']:.3f}")
        
        # 2. Calculate optimal prices with different urgency factors
        print(f"\n2. Calculating optimal limit prices with different urgency factors...")
        
        urgency_levels = [0.0, 0.25, 0.5, 0.75, 1.0]
        
        print("\n   Buy Orders:")
        for urgency in urgency_levels:
            optimal_price = client.get_optimal_limit_price(
                symbol=SYMBOL,
                side="buy",
                urgency_factor=urgency
            )
            print(f"     Urgency {urgency:.2f}: ${optimal_price:,.2f}")
        
        print("\n   Sell Orders:")
        for urgency in urgency_levels:
            optimal_price = client.get_optimal_limit_price(
                symbol=SYMBOL,
                side="sell",
                urgency_factor=urgency
            )
            print(f"     Urgency {urgency:.2f}: ${optimal_price:,.2f}")
        
        # 3. Demonstrate urgency loop (without actually placing orders)
        print(f"\n3. Demonstrating urgency loop for buy order...")
        print("   (This simulates increasing urgency until order gets filled)")
        
        # Simulate different urgency levels
        for urgency in [0.1, 0.3, 0.5, 0.7, 0.9]:
            optimal_price = client.get_optimal_limit_price(
                symbol=SYMBOL,
                side="buy",
                urgency_factor=urgency
            )
            
            # Calculate distance from mid price
            distance_from_mid = abs(optimal_price - order_book['mid_price']) / order_book['mid_price'] * 100
            
            print(f"   Urgency {urgency:.1f}: ${optimal_price:,.2f} "
                  f"(distance from mid: {distance_from_mid:.3f}%)")
            
            # Simulate order placement decision
            if urgency >= 0.7:
                print(f"     -> Order would likely get filled at this urgency level")
                break
            else:
                print(f"     -> Order might not get filled, increasing urgency...")
        
        # 4. Show how different urgency factors affect pricing
        print(f"\n4. Showing how different urgency factors affect pricing...")
        
        # Test with different urgency levels
        test_urgency_levels = [0.1, 0.3, 0.5, 0.7, 0.9]
        
        for urgency in test_urgency_levels:
            optimal_price = client.get_optimal_limit_price(
                symbol=SYMBOL,
                side="buy",
                urgency_factor=urgency
            )
            
            print(f"   Urgency {urgency:.1f}: ${optimal_price:,.2f}")
        
        print(f"\n=== Example completed successfully! ===")
        
    except Exception as e:
        print(f"Error: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main()) 