import pytest
from unittest.mock import Mock, patch
from fractrade_hl_simple import HyperliquidClient
import time
import math

@pytest.fixture
def client():
    return HyperliquidClient()

@pytest.fixture
def mock_l2_data():
    """Mock L2 order book data"""
    return {
        "coin": "BTC",
        "levels": [
            [  # Bids (level 0)
                {"n": 1, "px": "50000.0", "sz": "1.5"},
                {"n": 2, "px": "49999.0", "sz": "2.0"},
                {"n": 3, "px": "49998.0", "sz": "1.0"},
            ],
            [  # Asks (level 1)
                {"n": 1, "px": "50001.0", "sz": "1.2"},
                {"n": 2, "px": "50002.0", "sz": "2.5"},
                {"n": 3, "px": "50003.0", "sz": "1.8"},
            ]
        ],
        "time": int(time.time() * 1000)
    }

class TestOrderBook:
    def test_get_order_book_success(self, client, mock_l2_data):
        """Test successful order book retrieval"""
        with patch.object(client.info, 'l2_snapshot', return_value=mock_l2_data):
            order_book = client.get_order_book("BTC")
            
            assert order_book["symbol"] == "BTC"
            assert order_book["best_bid"] == 50000.0
            assert order_book["best_ask"] == 50001.0
            assert order_book["spread"] == 1.0
            assert order_book["mid_price"] == 50000.5
            
            # Check bids are sorted descending
            assert order_book["bids"][0]["price"] == 50000.0
            assert order_book["bids"][1]["price"] == 49999.0
            assert order_book["bids"][2]["price"] == 49998.0
            
            # Check asks are sorted ascending
            assert order_book["asks"][0]["price"] == 50001.0
            assert order_book["asks"][1]["price"] == 50002.0
            assert order_book["asks"][2]["price"] == 50003.0
            
            # Check sizes
            assert order_book["bids"][0]["size"] == 1.5
            assert order_book["asks"][0]["size"] == 1.2

    def test_get_order_book_invalid_response(self, client):
        """Test order book retrieval with invalid response"""
        with patch.object(client.info, 'l2_snapshot', return_value={"invalid": "data"}):
            with pytest.raises(ValueError, match="Invalid order book response"):
                client.get_order_book("BTC")

    def test_get_order_book_insufficient_levels(self, client):
        """Test order book retrieval with insufficient levels"""
        with patch.object(client.info, 'l2_snapshot', return_value={"levels": [[]]}):
            with pytest.raises(ValueError, match="Insufficient order book levels"):
                client.get_order_book("BTC")

    def test_get_order_book_empty_levels(self, client):
        """Test order book retrieval with empty levels"""
        with patch.object(client.info, 'l2_snapshot', return_value={"levels": [[], []]}):
            order_book = client.get_order_book("BTC")
            
            assert order_book["best_bid"] is None
            assert order_book["best_ask"] is None
            assert order_book["spread"] is None
            assert order_book["mid_price"] is None
            assert len(order_book["bids"]) == 0
            assert len(order_book["asks"]) == 0

class TestOptimalLimitPrice:
    def test_get_optimal_limit_price_buy_patient(self, client, mock_l2_data):
        """Test optimal buy price with low urgency (patient)"""
        with patch.object(client.info, 'l2_snapshot', return_value=mock_l2_data):
            with patch.object(client, 'get_price', return_value=50000.5):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="buy",
                    urgency_factor=0.0  # Very patient
                )
                
                # Should be close to best bid (very patient)
                assert abs(price - 50000.0) < 1.0

    def test_get_optimal_limit_price_buy_aggressive(self, client, mock_l2_data):
        """Test optimal buy price with high urgency (aggressive)"""
        with patch.object(client.info, 'l2_snapshot', return_value=mock_l2_data):
            with patch.object(client, 'get_price', return_value=50000.5):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="buy",
                    urgency_factor=1.0  # Very aggressive
                )
                
                # Should be close to best ask (very aggressive)
                assert abs(price - 50001.0) < 1.0

    def test_get_optimal_limit_price_sell_patient(self, client, mock_l2_data):
        """Test optimal sell price with low urgency (patient)"""
        with patch.object(client.info, 'l2_snapshot', return_value=mock_l2_data):
            with patch.object(client, 'get_price', return_value=50000.5):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="sell",
                    urgency_factor=0.0  # Very patient
                )
                
                # Should be close to best ask (very patient)
                assert abs(price - 50001.0) < 1.0

    def test_get_optimal_limit_price_sell_aggressive(self, client, mock_l2_data):
        """Test optimal sell price with high urgency (aggressive)"""
        with patch.object(client.info, 'l2_snapshot', return_value=mock_l2_data):
            with patch.object(client, 'get_price', return_value=50000.5):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="sell",
                    urgency_factor=1.0  # Very aggressive
                )
                
                # Should be close to best bid (very aggressive)
                assert abs(price - 50000.0) < 1.0

    def test_get_optimal_limit_price_urgency_range(self, client, mock_l2_data):
        """Test optimal price calculation across different urgency levels"""
        with patch.object(client.info, 'l2_snapshot', return_value=mock_l2_data):
            with patch.object(client, 'get_price', return_value=50000.5):
                # Test different urgency levels for buy orders
                prices = []
                for urgency in [0.0, 0.25, 0.5, 0.75, 1.0]:
                    price = client.get_optimal_limit_price(
                        symbol="BTC",
                        side="buy",
                        urgency_factor=urgency
                    )
                    prices.append(price)
                
                # Prices should be monotonically increasing with urgency
                for i in range(1, len(prices)):
                    assert prices[i] >= prices[i-1], f"Price should increase with urgency: {prices[i-1]} -> {prices[i]}"

    def test_get_optimal_limit_price_invalid_side(self, client):
        """Test optimal price calculation with invalid side"""
        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            client.get_optimal_limit_price("BTC", "invalid", 0.5)

    def test_get_optimal_limit_price_invalid_urgency(self, client):
        """Test optimal price calculation with invalid urgency factor"""
        with pytest.raises(ValueError, match="urgency_factor must be between 0.0 and 1.0"):
            client.get_optimal_limit_price("BTC", "buy", 1.5)

    def test_get_optimal_limit_price_fallback(self, client):
        """Test optimal price calculation fallback when order book fails"""
        with patch.object(client.info, 'l2_snapshot', side_effect=Exception("API error")):
            with patch.object(client, 'get_price', return_value=50000.0):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="buy",
                    urgency_factor=0.5
                )
                
                # Should use fallback calculation
                assert price > 50000.0  # Should be slightly above current price

    def test_get_optimal_limit_price_no_bids(self, client):
        """Test optimal price calculation when no bids are available"""
        mock_data_no_bids = {
            "coin": "BTC",
            "levels": [[], [{"n": 1, "px": "50001.0", "sz": "1.0"}]],  # No bids, some asks
            "time": int(time.time() * 1000)
        }
        
        with patch.object(client.info, 'l2_snapshot', return_value=mock_data_no_bids):
            with patch.object(client, 'get_price', return_value=50000.0):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="buy",
                    urgency_factor=0.5
                )
                
                # Should use current price with small premium
                assert price > 50000.0

    def test_get_optimal_limit_price_no_asks(self, client):
        """Test optimal price calculation when no asks are available"""
        mock_data_no_asks = {
            "coin": "BTC",
            "levels": [[{"n": 1, "px": "50000.0", "sz": "1.0"}], []],  # Some bids, no asks
            "time": int(time.time() * 1000)
        }
        
        with patch.object(client.info, 'l2_snapshot', return_value=mock_data_no_asks):
            with patch.object(client, 'get_price', return_value=50000.0):
                price = client.get_optimal_limit_price(
                    symbol="BTC",
                    side="sell",
                    urgency_factor=0.5
                )
                
                # Should use current price with small discount
                assert price < 50000.0

def test_integration_optimal_pricing(client):
    """Integration test for optimal pricing with real API calls"""
    # Test with a real symbol
    symbol = "BTC"
    
    # Get order book
    order_book = client.get_order_book(symbol)
    assert order_book["symbol"] == symbol
    assert order_book["best_bid"] is not None
    assert order_book["best_ask"] is not None
    assert order_book["spread"] > 0
    
    
    # Test optimal prices
    buy_price = client.get_optimal_limit_price(
        symbol=symbol,
        side="buy",
        urgency_factor=0.5
    )
    
    sell_price = client.get_optimal_limit_price(
        symbol=symbol,
        side="sell",
        urgency_factor=0.5
    )
    
    # Validate prices are reasonable
    assert buy_price >= order_book["best_bid"] or math.isclose(buy_price, order_book["best_bid"], rel_tol=1e-8)  # Buy price should be at or above best bid
    assert sell_price <= order_book["best_ask"] or math.isclose(sell_price, order_book["best_ask"], rel_tol=1e-8)  # Sell price should be at or below best ask
    assert buy_price <= sell_price  # Buy price should not be higher than sell price 