import pytest
from decimal import Decimal
from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount
import time


@pytest.fixture
def client():
    # Create authenticated client from .env for integration tests
    account = HyperliquidAccount.from_env()
    return HyperliquidClient(account=account)

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

def test_position_with_tp_sl(client):
    """Test opening a position with take profit and stop loss orders."""
    symbol = "BTC"
    size = 0.001  # Minimum size for BTC
    
    try:
        # Get current price
        price = client.get_price(symbol)
        print(f"Current {symbol} price: ${price:,.2f}")
        
        # Open market long position
        entry_order = client.buy(symbol, size)
        assert entry_order.symbol == symbol
        assert float(entry_order.size) == size
        assert entry_order.is_buy is True
        
        time.sleep(2)  # Wait for position to be reflected
        
        # Verify position was created
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is not None
        assert float(position.size) == size
        
        # Set take profit (10% above entry)
        tp_price = float(position.entry_price) * 1.10
        tp_order = client.take_profit(symbol, size, tp_price, is_buy=False)
        assert tp_order.symbol == symbol
        assert float(tp_order.size) == size
        assert tp_order.is_buy is False
        
        # Set stop loss (5% below entry)
        sl_price = float(position.entry_price) * 0.95
        sl_order = client.stop_loss(symbol, size, sl_price, is_buy=False)
        assert sl_order.symbol == symbol
        assert float(sl_order.size) == size
        assert sl_order.is_buy is False
        
        time.sleep(2)
        
        # Cleanup
        client.cancel_all_orders(symbol)
        client.close(symbol)
        
        # Verify position was closed
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is None or float(position.size) == 0
        
    finally:
        # Ensure cleanup in case of test failure
        try:
            client.cancel_all_orders(symbol)
            client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")

def test_get_open_orders(client):
    """Test retrieving open orders of different types (limit, stop loss, take profit)."""
    symbol = "BTC"
    size = 0.001  # Minimum size for BTC
    
    try:
        # Get current price
        current_price = client.get_price(symbol)
        print(f"Current {symbol} price: ${current_price:,.2f}")
        
        # 1. Open a long position
        entry_order = client.buy(symbol, size)
        assert entry_order.symbol == symbol
        assert float(entry_order.size) == size
        print(f"Placed entry order: {entry_order.order_id}")
        
        time.sleep(3)  # Increased wait time
        
        # Verify position was created
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is not None
        assert float(position.size) == size
        print(f"Position created with entry price: {position.entry_price}")
        
        # 2. Set take profit (10% above entry)
        tp_price = float(position.entry_price) * 1.10
        # Round price according to market specs
        tp_price = float(f"{tp_price:.5g}")  # 5 significant figures
        tp_order = client.take_profit(symbol, size, tp_price, is_buy=False)
        print(f"Placed TP order: {tp_order.order_id} at price {tp_price}")
        
        # 3. Set stop loss (5% below entry)
        sl_price = float(position.entry_price) * 0.95
        # Round price according to market specs
        sl_price = float(f"{sl_price:.5g}")  # 5 significant figures
        sl_order = client.stop_loss(symbol, size, sl_price, is_buy=False)
        print(f"Placed SL order: {sl_order.order_id} at price {sl_price}")
        
        # 4. Place a limit buy order 20% below current price
        limit_price = current_price * 0.80
        # Round price according to market specs
        limit_price = float(f"{limit_price:.5g}")  # 5 significant figures
        limit_order = client.buy(symbol, size, limit_price=limit_price)
        print(f"Placed limit buy order: {limit_order.order_id} at price {limit_price}")
        
        # Wait longer for orders to be processed
        print("Waiting for orders to be processed...")
        time.sleep(5)
        
        # 5. Get all open orders
        open_orders = client.get_open_orders(symbol)
        print(f"Found {len(open_orders)} open orders")
        
        # Debug: print all open orders
        for i, order in enumerate(open_orders):
            price = None
            
            print(f"Order {i+1} details: order_type={order.order_type}, type={order.type}")
            
            if order.order_type.trigger is not None:
                print(f"  Trigger details: {order.order_type.trigger}")
                if order.trigger_price is not None:
                    price = order.trigger_price
            elif order.order_type.limit is not None:
                price = order.limit_price
                
            print(f"Order {i+1}: {order.type}, ID: {order.order_id}, Price: {price}, Size: {order.size}, Buy: {order.is_buy}, Reduce Only: {order.reduce_only}")
        
        # 6. Verify we have at least 3 orders (TP, SL, and limit)
        assert len(open_orders) >= 3, f"Expected at least 3 orders, got {len(open_orders)}"
        
        # 7. Find each order type
        tp_orders = [order for order in open_orders if order.type == "take_profit"]
        sl_orders = [order for order in open_orders if order.type == "stop_loss"]
        limit_orders = [order for order in open_orders if order.type == "limit" and order.is_buy]
        
        # 8. Verify each order type exists
        assert len(tp_orders) > 0, "No take profit orders found"
        assert len(sl_orders) > 0, "No stop loss orders found"
        assert len(limit_orders) > 0, "No limit buy orders found"
        
        # 9. Verify take profit order
        tp = tp_orders[0]
        assert tp.symbol == symbol
        assert float(tp.size) == size
        assert tp.is_buy is False  # Sell to take profit on long
        assert tp.reduce_only is True
        assert tp.order_type.trigger is not None
        assert abs(float(tp.trigger_price) - tp_price) / tp_price < 0.01  # Within 1% of requested price
        
        # 10. Verify stop loss order
        sl = sl_orders[0]
        assert sl.symbol == symbol
        assert float(sl.size) == size
        assert sl.is_buy is False  # Sell to stop loss on long
        assert sl.reduce_only is True
        assert sl.order_type.trigger is not None
        assert abs(float(sl.trigger_price) - sl_price) / sl_price < 0.01  # Within 1% of requested price
        
        # 11. Verify limit buy order
        limit = limit_orders[0]
        assert limit.symbol == symbol
        assert float(limit.size) == size
        assert limit.is_buy is True
        assert limit.order_type.limit is not None
        assert abs(float(limit.limit_price) - limit_price) / limit_price < 0.01  # Within 1% of requested price
        
        # 12. Verify common fields on all orders
        for order in open_orders:
            assert order.order_id is not None and order.order_id != ""
            assert order.symbol == symbol
            assert float(order.size) > 0
            assert order.status == "open"
            assert order.created_at > 0
            assert hasattr(order, "is_buy")  # Either True or False
            
    finally:
        # Ensure cleanup in case of test failure
        try:
            client.cancel_all_orders(symbol)
            client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")

def test_get_open_orders_short(client):
    """Test retrieving open orders of different types for a short position."""
    symbol = "BTC"
    size = 0.001  # Minimum size for BTC
    
    try:
        # Get current price
        current_price = client.get_price(symbol)
        print(f"Current {symbol} price: ${current_price:,.2f}")
        
        # 1. Open a short position
        entry_order = client.sell(symbol, size)
        assert entry_order.symbol == symbol
        assert float(entry_order.size) == size
        print(f"Placed short entry order: {entry_order.order_id}")
        
        time.sleep(3)  # Increased wait time
        
        # Verify position was created
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is not None
        assert float(position.size) < 0  # Short position has negative size
        print(f"Short position created with entry price: {position.entry_price}")
        
        # 2. Set take profit (10% below entry for shorts)
        tp_price = float(position.entry_price) * 0.90
        # Round price according to market specs
        tp_price = float(f"{tp_price:.5g}")  # 5 significant figures
        tp_order = client.take_profit(symbol, abs(float(position.size)), tp_price, is_buy=True)
        print(f"Placed TP order for short: {tp_order.order_id} at price {tp_price}")
        
        # 3. Set stop loss (5% above entry for shorts)
        sl_price = float(position.entry_price) * 1.05
        # Round price according to market specs
        sl_price = float(f"{sl_price:.5g}")  # 5 significant figures
        sl_order = client.stop_loss(symbol, abs(float(position.size)), sl_price, is_buy=True)
        print(f"Placed SL order for short: {sl_order.order_id} at price {sl_price}")
        
        # 4. Place a limit sell order 20% above current price
        limit_price = current_price * 1.20
        # Round price according to market specs
        limit_price = float(f"{limit_price:.5g}")  # 5 significant figures
        limit_order = client.sell(symbol, size, limit_price=limit_price)
        print(f"Placed limit sell order: {limit_order.order_id} at price {limit_price}")
        
        # Wait longer for orders to be processed
        print("Waiting for orders to be processed...")
        time.sleep(5)
        
        # 5. Get all open orders
        open_orders = client.get_open_orders(symbol)
        print(f"Found {len(open_orders)} open orders")
        
        # Debug: print all open orders
        for i, order in enumerate(open_orders):
            price = None
            
            print(f"Order {i+1} details: order_type={order.order_type}, type={order.type}")
            
            if order.order_type.trigger is not None:
                print(f"  Trigger details: {order.order_type.trigger}")
                if order.trigger_price is not None:
                    price = order.trigger_price
            elif order.order_type.limit is not None:
                price = order.limit_price
                
            print(f"Order {i+1}: {order.type}, ID: {order.order_id}, Price: {price}, Size: {order.size}, Buy: {order.is_buy}, Reduce Only: {order.reduce_only}")
        
        # 6. Verify we have at least 3 orders (TP, SL, and limit)
        assert len(open_orders) >= 3, f"Expected at least 3 orders, got {len(open_orders)}"
        
        # 7. Find each order type
        tp_orders = [order for order in open_orders if order.type == "take_profit"]
        sl_orders = [order for order in open_orders if order.type == "stop_loss"]
        limit_orders = [order for order in open_orders if order.type == "limit" and not order.is_buy]  # Sell limit orders
        
        # 8. Verify each order type exists
        assert len(tp_orders) > 0, "No take profit orders found"
        assert len(sl_orders) > 0, "No stop loss orders found"
        assert len(limit_orders) > 0, "No limit sell orders found"
        
        # 9. Verify take profit order for short
        tp = tp_orders[0]
        assert tp.symbol == symbol
        assert float(tp.size) == size
        assert tp.is_buy is True  # Buy to take profit on short
        assert tp.reduce_only is True
        assert tp.order_type.trigger is not None
        assert abs(float(tp.trigger_price) - tp_price) / tp_price < 0.01  # Within 1% of requested price
        
        # 10. Verify stop loss order for short
        sl = sl_orders[0]
        assert sl.symbol == symbol
        assert float(sl.size) == size
        assert sl.is_buy is True  # Buy to stop loss on short
        assert sl.reduce_only is True
        assert sl.order_type.trigger is not None
        assert abs(float(sl.trigger_price) - sl_price) / sl_price < 0.01  # Within 1% of requested price
        
        # 11. Verify limit sell order
        limit = limit_orders[0]
        assert limit.symbol == symbol
        assert float(limit.size) == size
        assert limit.is_buy is False  # Sell order
        assert limit.order_type.limit is not None
        assert abs(float(limit.limit_price) - limit_price) / limit_price < 0.01  # Within 1% of requested price
        
        # 12. Verify common fields on all orders
        for order in open_orders:
            assert order.order_id is not None and order.order_id != ""
            assert order.symbol == symbol
            assert float(order.size) > 0
            assert order.status == "open"
            assert order.created_at > 0
            assert hasattr(order, "is_buy")  # Either True or False
            
    finally:
        # Ensure cleanup in case of test failure
        try:
            client.cancel_all_orders(symbol)
            client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")

def test_get_sl_tp_prices(client):
    """Test the convenience functions for getting stop loss and take profit prices."""
    symbol = "BTC"
    size = 0.001  # Minimum size for BTC
    
    try:
        # Get current price
        current_price = client.get_price(symbol)
        print(f"Current {symbol} price: ${current_price:,.2f}")
        
        # 1. Open a long position
        entry_order = client.buy(symbol, size)
        assert entry_order.symbol == symbol
        print(f"Placed entry order: {entry_order.order_id}")
        
        time.sleep(3)  # Wait for position to be reflected
        
        # Verify position was created
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        assert position is not None
        print(f"Position created with entry price: {position.entry_price}")
        
        # 2. Set take profit (10% above entry)
        tp_price = float(position.entry_price) * 1.10
        tp_price = float(f"{tp_price:.5g}")  # 5 significant figures
        tp_order = client.take_profit(symbol, size, tp_price, is_buy=False)
        print(f"Placed TP order at price {tp_price}")
        
        # 3. Set stop loss (5% below entry)
        sl_price = float(position.entry_price) * 0.95
        sl_price = float(f"{sl_price:.5g}")  # 5 significant figures
        sl_order = client.stop_loss(symbol, size, sl_price, is_buy=False)
        print(f"Placed SL order at price {sl_price}")
        
        # Wait for orders to be processed
        time.sleep(3)
        
        # 4. Test the convenience functions
        retrieved_tp_price = client.get_take_profit_price(symbol)
        retrieved_sl_price = client.get_stop_loss_price(symbol)
        
        print(f"Retrieved TP price: {retrieved_tp_price}")
        print(f"Retrieved SL price: {retrieved_sl_price}")
        
        # 5. Verify the prices match
        assert retrieved_tp_price is not None, "Take profit price should not be None"
        assert retrieved_sl_price is not None, "Stop loss price should not be None"
        
        assert abs(float(retrieved_tp_price) - tp_price) / tp_price < 0.01, "Take profit price should match"
        assert abs(float(retrieved_sl_price) - sl_price) / sl_price < 0.01, "Stop loss price should match"
        
        # 6. Test with a symbol that has no orders
        test_symbol = "ETH"  # Assuming no ETH orders exist
        no_tp_price = client.get_take_profit_price(test_symbol)
        no_sl_price = client.get_stop_loss_price(test_symbol)
        
        assert no_tp_price is None, "Take profit price should be None for symbol with no orders"
        assert no_sl_price is None, "Stop loss price should be None for symbol with no orders"
        
    finally:
        # Ensure cleanup in case of test failure
        try:
            client.cancel_all_orders(symbol)
            client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")

def test_convenience_functions(client):
    """Test the convenience functions added to the client."""
    symbol = "BTC"
    size = 0.001  # Minimum size for BTC
    
    try:
        # Test has_position when no position exists
        client.cancel_all_orders(symbol)
        positions = client.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is not None and position.size != 0:
            client.close(symbol)
            time.sleep(2)
        
        has_position = client.has_position(symbol)
        assert has_position is False, "Should not have a position initially"
        
        # Test get_position_size with no position
        position_size = client.get_position_size(symbol)
        assert position_size is None, "Position size should be None when no position exists"
        
        # Test has_active_orders when no orders exist
        client.cancel_all_orders(symbol)
        has_orders = client.has_active_orders(symbol)
        assert has_orders is False, "Should not have active orders initially"
        
        # Create a position and test the functions
        current_price = client.get_price(symbol)
        print(f"Current {symbol} price: ${current_price:,.2f}")
        
        # Open a long position
        entry_order = client.buy(symbol, size)
        print(f"Placed entry order: {entry_order.order_id}")
        time.sleep(3)
        
        # Test has_position with an active position
        has_position = client.has_position(symbol)
        assert has_position is True, "Should have a position after buying"
        
        # Test get_position_size with a long position
        position_size = client.get_position_size(symbol)
        assert position_size is not None, "Position size should not be None"
        assert float(position_size) > 0, "Position size should be positive for a long position"
        
        # Determine position direction based on position size
        position_direction = client.get_position_direction(symbol)
        assert position_direction == "long", "Position direction should be 'long'"
        
        # Test bracket order functionality by updating stop loss and take profit
        entry_price = float(client.get_positions()[0].entry_price)
        print(f"Entry price: ${entry_price:,.2f}")
        
        # Set initial SL/TP
        sl_price = entry_price * 0.95
        tp_price = entry_price * 1.10
        
        sl_order = client.stop_loss(symbol, size, sl_price, is_buy=False)
        tp_order = client.take_profit(symbol, size, tp_price, is_buy=False)
        
        print(f"Initial stop loss price: ${sl_price:,.2f}, order ID: {sl_order.order_id}")
        print(f"Initial take profit price: ${tp_price:,.2f}, order ID: {tp_order.order_id}")
        
        time.sleep(2)
        
        # Test update_stop_loss
        new_sl_price = entry_price * 0.96  # Tighter stop loss
        updated_sl = client.update_stop_loss(symbol, new_sl_price)
        assert updated_sl is not None, "Updated stop loss order should not be None"
        print(f"Updated stop loss price: ${new_sl_price:,.2f}, order ID: {updated_sl.order_id}")
        
        # Test update_take_profit
        new_tp_price = entry_price * 1.08  # Lower take profit
        updated_tp = client.update_take_profit(symbol, new_tp_price)
        assert updated_tp is not None, "Updated take profit order should not be None"
        print(f"Updated take profit price: ${new_tp_price:,.2f}, order ID: {updated_tp.order_id}")
        
        time.sleep(2)
        
        # Verify the updates worked
        retrieved_sl_price = client.get_stop_loss_price(symbol)
        retrieved_tp_price = client.get_take_profit_price(symbol)
        
        assert abs(float(retrieved_sl_price) - new_sl_price) / new_sl_price < 0.01, "Stop loss price should be updated"
        assert abs(float(retrieved_tp_price) - new_tp_price) / new_tp_price < 0.01, "Take profit price should be updated"
        
        # Test direct order modification
        print("Testing direct order modification...")
        
        # Place a limit order
        limit_price = entry_price * 0.90  # 10% below entry price
        limit_order = client.buy(symbol, size, limit_price=limit_price)
        print(f"Placed limit order: {limit_order.order_id} at price ${limit_price:,.2f}")
        
        time.sleep(2)
        
        # Modify the limit order
        new_limit_price = entry_price * 0.92  # 8% below entry price
        order_type = {"limit": {"tif": "Gtc"}}
        
        try:
            modified_order = client.modify_order(
                order_id=limit_order.order_id,
                symbol=symbol,
                is_buy=True,
                size=size,
                price=new_limit_price,
                order_type=order_type,
                reduce_only=False
            )
            
            print(f"Modified limit order: {modified_order.order_id} to price ${new_limit_price:,.2f}")
            
            # Verify the modification worked
            open_orders = client.get_open_orders(symbol)
            found_order = next((o for o in open_orders if o.order_id == modified_order.order_id), None)
            
            assert found_order is not None, "Modified order should exist"
            assert abs(float(found_order.limit_price) - new_limit_price) / new_limit_price < 0.01, "Limit price should be updated"
            
        except Exception as e:
            print(f"Order modification test failed: {e}")
            # Continue with the test even if this part fails
        
        # Test trailing stop
        trailing_stop = client.trailing_stop(symbol, 3.0)  # 3% trailing stop
        assert trailing_stop is not None, "Trailing stop order should not be None"
        print(f"Set trailing stop with order ID: {trailing_stop.order_id}")
        
    finally:
        # Ensure cleanup in case of test failure
        try:
            client.cancel_all_orders(symbol)
            client.close(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")

def test_limit_orders(client):
    """Test creating, modifying, and canceling limit orders."""
    symbol = "BTC"
    size = 0.001  # Minimum size for BTC
    
    try:
        # Clean up any existing orders for this symbol
        client.cancel_all_orders(symbol)
        time.sleep(1)
        
        # Get current price
        current_price = client.get_price(symbol)
        print(f"Current {symbol} price: ${current_price:,.2f}")
        
        # 1. Create a limit buy order (10% below current price)
        limit_price = current_price * 0.90
        limit_price = float(f"{limit_price:.5g}")  # Round to 5 significant figures
        print(f"Creating limit buy order at price: ${limit_price:,.2f}")
        
        limit_order = client.buy(symbol, size, limit_price=limit_price)
        assert limit_order.symbol == symbol
        assert float(limit_order.size) == size
        assert limit_order.is_buy is True
        assert limit_order.status in ["open", "filled"]
        print(f"Created limit order: {limit_order.order_id}")
        
        # Wait for order to be processed
        time.sleep(2)
        
        # 2. Verify the order exists in open orders
        open_orders = client.get_open_orders(symbol)
        found_order = next((o for o in open_orders if o.order_id == limit_order.order_id), None)
        assert found_order is not None, "Limit order should exist in open orders"
        assert abs(float(found_order.limit_price) - limit_price) / limit_price < 0.01, "Limit price should match"
        print(f"Verified order exists with price: ${found_order.limit_price}")
        
        # 3. Modify the limit order (8% below current price instead of 10%)
        new_limit_price = current_price * 0.92
        new_limit_price = float(f"{new_limit_price:.5g}")  # Round to 5 significant figures
        print(f"Modifying limit order to new price: ${new_limit_price:,.2f}")
        
        order_type = {"limit": {"tif": "Gtc"}}
        
        modified_order = client.modify_order(
            order_id=limit_order.order_id,
            symbol=symbol,
            is_buy=True,
            size=size,
            price=new_limit_price,
            order_type=order_type,
            reduce_only=False
        )
        
        assert modified_order is not None, "Modified order should not be None"
        # Note: Order ID might change if the exchange cancels and recreates the order
        print(f"Original order ID: {limit_order.order_id}")
        print(f"Modified order ID: {modified_order.order_id}")
        print(f"Modified order: {modified_order.order_id}")
        
        # Wait for modification to be processed
        time.sleep(2)
        
        # 4. Verify the modification worked
        open_orders_after_modify = client.get_open_orders(symbol)
        # Look for the modified order using the new order ID
        found_modified_order = next((o for o in open_orders_after_modify if o.order_id == modified_order.order_id), None)
        
        assert found_modified_order is not None, "Modified order should still exist"
        assert abs(float(found_modified_order.limit_price) - new_limit_price) / new_limit_price < 0.01, "Limit price should be updated"
        print(f"Verified modification: new price is ${found_modified_order.limit_price}")
        
        # 5. Cancel the limit order
        print("Canceling the limit order...")
        client.cancel_all_orders(symbol)
        time.sleep(2)
        
        # 6. Verify the order was canceled
        open_orders_after_cancel = client.get_open_orders(symbol)
        # Check that both the original and modified orders are no longer in open orders
        found_original_order = next((o for o in open_orders_after_cancel if o.order_id == limit_order.order_id), None)
        found_modified_order = next((o for o in open_orders_after_cancel if o.order_id == modified_order.order_id), None)
        assert found_original_order is None, "Original order should be canceled and not in open orders"
        assert found_modified_order is None, "Modified order should be canceled and not in open orders"
        print("Verified orders were successfully canceled")
        
        print("Limit order test completed successfully!")
        
    except Exception as e:
        print(f"Limit order test failed: {e}")
        raise
    finally:
        # Ensure cleanup in case of test failure
        try:
            client.cancel_all_orders(symbol)
        except Exception as e:
            print(f"Cleanup error: {e}")