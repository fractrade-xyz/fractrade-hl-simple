import pytest
from decimal import Decimal
from fractrade_hl_simple import HyperliquidClient
import time


@pytest.fixture
def client():
    return HyperliquidClient()

@pytest.fixture
def test_params():
    return {
        "symbol": "BTC",
        "size": 0.001,    # Minimal position size for BTC
        "slippage": 0.01  # 1% slippage tolerance
    }

def test_get_price(client, test_params):
    """Test getting current price."""
    # Get all prices
    prices = client.get_price()
    assert isinstance(prices, dict)
    assert test_params["symbol"] in prices
    price = client.get_price(test_params["symbol"])
    assert isinstance(price, float)
    assert price > 0  # Price should be positive
    
    # Verify both methods return similar prices (within 1% tolerance)
    assert abs(prices[test_params["symbol"]] - price) / price < 0.01

    # Test error case
    with pytest.raises(ValueError):
        client.get_price("INVALID_SYMBOL")

def test_get_perp_balance(client):
    balance = client.get_perp_balance()
    assert isinstance(balance, Decimal)
    assert balance > 0  # Ensure account has funds

def test_trading_flow(client, test_params):
    """Test full trading flow with minimal positions."""
    
    symbol = test_params["symbol"]
    size = test_params["size"]
    
    # Get initial state
    initial_balance = float(client.get_perp_balance())
    assert initial_balance > 0, "Account must have funds for trading tests"
    
    try:
        # 1. Place and cancel limit order
        current_price = client.get_price(symbol)
        limit_price = current_price * 0.999  # 0.1% below market instead of 5%
        
        order = client.buy(symbol, size, limit_price=limit_price)
        assert order.symbol == symbol
        assert float(order.size) == size
        assert order.is_buy is True
        
        time.sleep(2)
        client.cancel_all_orders(symbol)
        
        # 2. Open market long position
        order = client.buy(symbol, size)  # No limit_price = market order
        assert order.symbol == symbol
        assert float(order.size) == size
        
        time.sleep(2)
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is not None
        assert float(position.size) == size
        
        # 3. Close long position
        close_order = client.close(symbol)
        assert close_order.symbol == symbol
        assert float(close_order.size) == size
        assert close_order.is_buy is False
        
        time.sleep(2)
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is None or float(position.size) == 0
        
        # 4. Open and close short position
        order = client.sell(symbol, size)  # No limit_price = market order
        assert order.symbol == symbol
        assert float(order.size) == size
        
        time.sleep(2)
        close_order = client.close(symbol)
        assert close_order.symbol == symbol
        assert float(close_order.size) == size
        assert close_order.is_buy is True
        
        # Verify final state
        time.sleep(2)
        final_balance = float(client.get_perp_balance())
        balance_change = abs(final_balance - initial_balance)
        assert balance_change < initial_balance * test_params["slippage"], \
            f"Balance changed by {balance_change}, which is more than {test_params['slippage']*100}% of initial balance"
        
    finally:
        # Cleanup: ensure no positions are left open
        try:
            client.cancel_all_orders(symbol)
            positions = client.get_positions()
            if any(p.symbol == symbol for p in positions):
                client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")

def test_buy_sell_market(client):
    """Test basic market buy and sell functionality."""
    symbol = "BTC"
    size = 0.001
    
    try:
        # Buy market order
        buy_order = client.buy(symbol, size)
        assert buy_order.symbol == symbol
        assert float(buy_order.size) == size
        assert buy_order.is_buy is True
        assert buy_order.status in ["open", "filled"]
        
        time.sleep(2)  # Wait for position to be reflected
        
        # Verify position was created
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is not None
        assert float(position.size) == size
        
        # Sell market order
        sell_order = client.sell(symbol, size)
        assert sell_order.symbol == symbol
        assert float(sell_order.size) == size
        assert sell_order.is_buy is False
        assert sell_order.status in ["open", "filled"]
        
        time.sleep(2)  # Wait for position to be closed
        
        # Verify position was closed
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is None or float(position.size) == 0
        
    finally:
        # Cleanup: ensure no positions are left open
        try:
            client.cancel_all_orders(symbol)
            positions = client.get_positions()
            if any(p.symbol == symbol for p in positions):
                client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")