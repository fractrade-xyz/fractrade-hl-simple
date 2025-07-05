import pytest
import time
from decimal import Decimal
from fractrade_hl_simple import HyperliquidClient
from fractrade_hl_simple.models import HyperliquidAccount
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

@pytest.fixture
def client():
    """Create authenticated client for testing."""
    return HyperliquidClient()

@pytest.fixture
def symbol():
    """Test symbol."""
    return "BLAST"

class TestBlastIntegration:
    """Integration tests for BLAST trading operations."""
    
    def test_get_blast_price(self, client, symbol):
        """Test getting BLAST price."""
        price = client.get_price(symbol)
        assert isinstance(price, float)
        assert price > 0
        print(f"BLAST current price: ${price:,.6f}")
    
    def test_get_blast_order_book(self, client, symbol):
        """Test getting BLAST order book."""
        order_book = client.get_order_book(symbol)
        assert "bids" in order_book
        assert "asks" in order_book
        assert "best_bid" in order_book
        assert "best_ask" in order_book
        assert "spread" in order_book
        assert "mid_price" in order_book
        
        print(f"BLAST order book:")
        print(f"  Best bid: ${order_book['best_bid']:,.6f}")
        print(f"  Best ask: ${order_book['best_ask']:,.6f}")
        print(f"  Spread: ${order_book['spread']:,.6f}")
        print(f"  Mid price: ${order_book['mid_price']:,.6f}")
    
    def test_get_blast_optimal_prices(self, client, symbol):
        """Test getting optimal limit prices for BLAST."""
        # Test buy prices
        patient_buy = client.get_optimal_limit_price(symbol, "buy", urgency_factor=0.1)
        aggressive_buy = client.get_optimal_limit_price(symbol, "buy", urgency_factor=0.9)
        
        # Test sell prices
        patient_sell = client.get_optimal_limit_price(symbol, "sell", urgency_factor=0.1)
        aggressive_sell = client.get_optimal_limit_price(symbol, "sell", urgency_factor=0.9)
        
        current_price = client.get_price(symbol)
        
        print(f"BLAST optimal prices (current: ${current_price:,.6f}):")
        print(f"  Patient buy: ${patient_buy:,.6f}")
        print(f"  Aggressive buy: ${aggressive_buy:,.6f}")
        print(f"  Patient sell: ${patient_sell:,.6f}")
        print(f"  Aggressive sell: ${aggressive_sell:,.6f}")
        
        # Validate prices
        assert patient_buy <= aggressive_buy
        assert aggressive_sell <= patient_sell
        assert patient_buy > 0
        assert aggressive_buy > 0
        assert patient_sell > 0
        assert aggressive_sell > 0
    
    def test_blast_min_notional_error(self, client, symbol):
        """Test that orders below $10 notional raise a clear error."""
        small_size = 1  # For BLAST, price ~0.002, so 1*0.002 < $10
        current_price = client.get_price(symbol)
        with pytest.raises(ValueError, match="Order must have minimum value of \$10"):
            client.buy(symbol, small_size, limit_price=current_price)

    def test_blast_market_buy_and_sell(self, client, symbol):
        """Test BLAST market buy and sell operations with notional > $20."""
        if not client.is_authenticated():
            pytest.skip("Authentication required for trading tests")
        
        # Use a size that ensures notional > $20
        current_price = client.get_price(symbol)
        position_size = int((21 // current_price) + 1)  # Ensure > $20
        
        try:
            client.cancel_all_orders(symbol)
            time.sleep(1)
            print(f"\n=== Testing BLAST Market Buy ===")
            buy_order = client.buy(symbol, position_size)
            print(f"Market buy order placed: {buy_order.order_id}")
            print(f"Order status: {buy_order.status}")
            if hasattr(buy_order, 'average_fill_price'):
                print(f"Average fill price: ${float(buy_order.average_fill_price):,.6f}")
            time.sleep(2)
            positions = client.get_positions()
            blast_position = next((p for p in positions if p.symbol == symbol), None)
            if blast_position:
                print(f"Position size: {float(blast_position.size):+.3f}")
                print(f"Entry price: ${float(blast_position.entry_price):,.6f}")
                print(f"Unrealized PnL: ${float(blast_position.unrealized_pnl):,.2f}")
            print(f"\n=== Testing BLAST Market Sell ===")
            sell_order = client.sell(symbol, position_size)
            print(f"Market sell order placed: {sell_order.order_id}")
            print(f"Order status: {sell_order.status}")
            if hasattr(sell_order, 'average_fill_price'):
                print(f"Average fill price: ${float(sell_order.average_fill_price):,.6f}")
            time.sleep(2)
            positions = client.get_positions()
            blast_position = next((p for p in positions if p.symbol == symbol), None)
            if blast_position:
                print(f"Remaining position size: {float(blast_position.size):+.3f}")
            else:
                print("Position successfully closed")
        except Exception as e:
            print(f"Error in market buy/sell test: {str(e)}")
            try:
                client.cancel_all_orders(symbol)
                client.close(symbol)
            except:
                pass
            raise
    
    def test_blast_limit_buy_and_sell(self, client, symbol):
        """Test BLAST limit buy and sell operations."""
        if not client.is_authenticated():
            pytest.skip("Authentication required for trading tests")
        
        # Use a size that ensures notional > $20 (same as market order test)
        current_price = client.get_price(symbol)
        position_size = int((21 // current_price) + 1)  # Ensure > $20
        
        try:
            # Cancel any existing orders first
            client.cancel_all_orders(symbol)
            time.sleep(1)
            
            # Get current price and set limit prices
            current_price = client.get_price(symbol)
            buy_limit_price = current_price * 0.99  # 1% below market
            sell_limit_price = current_price * 1.01  # 1% above market
            
            print(f"\n=== Testing BLAST Limit Buy ===")
            print(f"Current price: ${current_price:,.6f}")
            print(f"Buy limit price: ${buy_limit_price:,.6f}")
            
            # Limit buy
            buy_order = client.buy(symbol, position_size, limit_price=buy_limit_price)
            print(f"Limit buy order placed: {buy_order.order_id}")
            print(f"Order status: {buy_order.status}")
            
            # Wait a moment
            time.sleep(2)
            
            # Cancel limit order if not filled
            client.cancel_all_orders(symbol)
            time.sleep(1)
            
            # Check if we have a position
            positions = client.get_positions()
            blast_position = next((p for p in positions if p.symbol == symbol), None)
            
            if blast_position and float(blast_position.size) > 0:
                print(f"Position opened: {float(blast_position.size):+.3f} at ${float(blast_position.entry_price):,.6f}")
                
                # Limit sell to close
                print(f"\n=== Testing BLAST Limit Sell ===")
                print(f"Sell limit price: ${sell_limit_price:,.6f}")
                
                sell_order = client.sell(symbol, position_size, limit_price=sell_limit_price)
                print(f"Limit sell order placed: {sell_order.order_id}")
                print(f"Order status: {sell_order.status}")
                
                # Wait a moment
                time.sleep(2)
                
                # Cancel limit order if not filled
                client.cancel_all_orders(symbol)
                time.sleep(1)
                
                # Market close if limit didn't fill
                positions = client.get_positions()
                blast_position = next((p for p in positions if p.symbol == symbol), None)
                if blast_position and float(blast_position.size) > 0:
                    print("Limit sell didn't fill, closing with market order")
                    close_order = client.close(symbol)
                    print(f"Market close order: {close_order.order_id}")
                    time.sleep(2)
            else:
                print("Limit buy didn't fill, no position opened")
            
            # Final cleanup
            client.cancel_all_orders(symbol)
            time.sleep(1)
            
        except Exception as e:
            print(f"Error in limit buy/sell test: {str(e)}")
            # Cleanup
            try:
                client.cancel_all_orders(symbol)
                client.close(symbol)
            except:
                pass
            raise
    
    def test_blast_order_book_analysis(self, client, symbol):
        """Test detailed order book analysis for BLAST."""
        order_book = client.get_order_book(symbol)
        
        print(f"\n=== BLAST Order Book Analysis ===")
        print(f"Total bids: {len(order_book['bids'])}")
        print(f"Total asks: {len(order_book['asks'])}")
        
        # Show top 10 bids and asks
        print("\nTop 10 Bids:")
        for i, bid in enumerate(order_book['bids'][:10]):
            print(f"  {i+1:2d}. ${bid['price']:,.6f} - {bid['size']:.3f}")
        
        print("\nTop 10 Asks:")
        for i, ask in enumerate(order_book['asks'][:10]):
            print(f"  {i+1:2d}. ${ask['price']:,.6f} - {ask['size']:.3f}")
        
        # Calculate some metrics
        if order_book['bids'] and order_book['asks']:
            spread_pct = (order_book['spread'] / order_book['mid_price']) * 100
            print(f"\nSpread: ${order_book['spread']:,.6f} ({spread_pct:.4f}%)")
            
            # Calculate depth
            bid_depth = sum(bid['size'] for bid in order_book['bids'][:5])
            ask_depth = sum(ask['size'] for ask in order_book['asks'][:5])
            print(f"Bid depth (top 5): {bid_depth:.3f}")
            print(f"Ask depth (top 5): {ask_depth:.3f}")
    
    def test_blast_market_specs(self, client, symbol):
        """Test getting BLAST market specifications."""
        market_info = client.get_market_info(symbol)
        
        print(f"\n=== BLAST Market Specifications ===")
        print(f"Market info: {market_info}")
        
        # Check if we have market specs
        if symbol in client.market_specs:
            specs = client.market_specs[symbol]
            print(f"Size decimals: {specs.get('size_decimals', 'N/A')}")
            print(f"Price decimals: {specs.get('price_decimals', 'N/A')}")
        else:
            print("No market specs found for BLAST")
    
    def test_blast_price_validation(self, client, symbol):
        """Test price validation for BLAST orders."""
        current_price = client.get_price(symbol)
        
        print(f"\n=== BLAST Price Validation ===")
        print(f"Current price: ${current_price:,.6f}")
        
        # Test various price levels
        test_prices = [
            current_price * 0.95,  # 5% below
            current_price * 0.99,  # 1% below
            current_price,          # current
            current_price * 1.01,  # 1% above
            current_price * 1.05,  # 5% above
        ]
        
        for price in test_prices:
            try:
                # Test price formatting
                _, formatted_price = client._validate_and_format_order(symbol, 1.0, price)
                print(f"Price ${price:,.6f} -> ${formatted_price:,.6f}")
            except Exception as e:
                print(f"Price ${price:,.6f} validation failed: {str(e)}")

if __name__ == "__main__":
    # Run specific test
    pytest.main([__file__, "-v", "-s"]) 