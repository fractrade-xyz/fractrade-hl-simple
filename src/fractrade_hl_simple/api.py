from typing import Optional, Dict, List, Union, Literal, Tuple, Any
from decimal import Decimal
import os
from contextlib import contextmanager
from .hyperliquid import HyperliquidClient
from .models import (
    HyperliquidAccount,
    UserState,
    Position,
    Order,
    DACITE_CONFIG
)
from dacite import from_dict

@contextmanager
def get_client(account: Optional[Union[Dict, HyperliquidAccount]] = None) -> HyperliquidClient:
    """Context manager to handle client creation and cleanup."""
    client = HyperliquidClient(account=account)
    try:
        yield client
    finally:
        # Add any cleanup if needed
        pass

def get_user_state(address: Optional[str] = None) -> UserState:
    """Get user state information for a given address or authenticated user."""
    client = HyperliquidClient()
    return client.get_user_state(address)

def get_positions(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                 client: Optional[HyperliquidClient] = None) -> List[Position]:
    """Get open positions for authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_positions()
    return client.get_positions()

def get_price(symbol: Optional[str] = None,
             account: Optional[Union[Dict, HyperliquidAccount]] = None,
             client: Optional[HyperliquidClient] = None) -> Union[float, Dict[str, float]]:
    """Get current price for a symbol or all symbols."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_price(symbol)
    return client.get_price(symbol)

def get_perp_balance(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None) -> Decimal:
    """Get perpetual balance for a given address or authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_perp_balance(address)
    return client.get_perp_balance(address)

def buy(symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        account: Optional[Union[Dict, HyperliquidAccount]] = None,
        client: Optional[HyperliquidClient] = None) -> Order:
    """Place a buy order (market or limit)."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.buy(symbol, size, limit_price)
    return client.buy(symbol, size, limit_price)

def sell(symbol: str,
         size: float,
         limit_price: Optional[float] = None,
         account: Optional[Union[Dict, HyperliquidAccount]] = None,
         client: Optional[HyperliquidClient] = None) -> Order:
    """Place a sell order (market or limit)."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.sell(symbol, size, limit_price)
    return client.sell(symbol, size, limit_price)

def close(symbol: str,
          account: Optional[Union[Dict, HyperliquidAccount]] = None,
          client: Optional[HyperliquidClient] = None) -> Order:
    """Close position for a given symbol."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.close(symbol)
    return client.close(symbol)

def stop_loss(symbol: str,
              size: float,
              stop_price: float,
              is_buy: bool = False,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None) -> Order:
    """Place a stop loss order."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.stop_loss(symbol, size, stop_price, is_buy)
    return client.stop_loss(symbol, size, stop_price, is_buy)

def take_profit(symbol: str,
                size: float,
                take_profit_price: float,
                is_buy: bool = False,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> Order:
    """Place a take profit order."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.take_profit(symbol, size, take_profit_price, is_buy)
    return client.take_profit(symbol, size, take_profit_price, is_buy)

def open_long_position(symbol: str,
                      size: float,
                      stop_loss_price: Optional[float] = None,
                      take_profit_price: Optional[float] = None,
                      account: Optional[Union[Dict, HyperliquidAccount]] = None,
                      client: Optional[HyperliquidClient] = None) -> Dict[str, Order]:
    """Open a long position with optional stop loss and take profit orders."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.open_long_position(symbol, size, stop_loss_price, take_profit_price)
    return client.open_long_position(symbol, size, stop_loss_price, take_profit_price)

def open_short_position(symbol: str,
                       size: float,
                       stop_loss_price: Optional[float] = None,
                       take_profit_price: Optional[float] = None,
                       account: Optional[Union[Dict, HyperliquidAccount]] = None,
                       client: Optional[HyperliquidClient] = None) -> Dict[str, Order]:
    """Open a short position with optional stop loss and take profit orders."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.open_short_position(symbol, size, stop_loss_price, take_profit_price)
    return client.open_short_position(symbol, size, stop_loss_price, take_profit_price)

def cancel_all_orders(symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all orders for a symbol or all symbols."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.cancel_all_orders(symbol)
    return client.cancel_all_orders(symbol)

def cancel_order(order_id: str,
                symbol: str,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel a specific order by order ID and symbol.
    
    Args:
        order_id (str): The order ID to cancel
        symbol (str): The symbol the order is for
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials
        client (Optional[HyperliquidClient]): Existing client instance
        
    Returns:
        bool: True if order was successfully cancelled, False otherwise
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.cancel_order(int(order_id), symbol)
    return client.cancel_order(int(order_id), symbol)

def get_open_orders(symbol: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None) -> List[Order]:
    """Get all open orders for the authenticated user.
    
    Args:
        symbol (Optional[str]): If provided, only returns orders for this symbol
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials
        client (Optional[HyperliquidClient]): Existing client instance
        
    Returns:
        List[Order]: List of open orders
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_open_orders(symbol)
    return client.get_open_orders(symbol)

def get_funding_rates(symbol: Optional[str] = None,
                     threshold: Optional[float] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> Union[float, List[Dict[str, Any]]]:
    """Get funding rates for all tokens or a specific symbol.
    
    Args:
        symbol (Optional[str]): If provided, returns funding rate for specific symbol.
                              If None, returns funding rates for all tokens sorted by value.
        threshold (Optional[float]): If provided, only returns symbols where the absolute funding rate
                                   is greater than or equal to the absolute threshold value.
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials (not needed for funding rates)
        client (Optional[HyperliquidClient]): Existing client instance
        
    Returns:
        Union[float, List[Dict[str, Any]]]: 
            - If symbol is provided: float funding rate for the symbol
            - If symbol is None: List of dicts with symbol and funding rate, sorted from highest positive to lowest negative
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_funding_rates(symbol, threshold)
    return client.get_funding_rates(symbol, threshold)

def get_order_book(symbol: str,
                  account: Optional[Union[Dict, HyperliquidAccount]] = None,
                  client: Optional[HyperliquidClient] = None) -> Dict[str, Any]:
    """Get the current order book (L2 snapshot) for a symbol.
    
    Args:
        symbol (str): Trading pair symbol (e.g., "BTC")
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials (not needed for order book)
        client (Optional[HyperliquidClient]): Existing client instance
        
    Returns:
        Dict[str, Any]: Order book data with the following structure:
            {
                "symbol": str,
                "bids": List[Dict[str, float]],  # List of {price, size} dicts
                "asks": List[Dict[str, float]],  # List of {price, size} dicts
                "timestamp": int,
                "best_bid": float,
                "best_ask": float,
                "spread": float,
                "mid_price": float
            }
            
    Raises:
        ValueError: If symbol is not found or order book data is invalid
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_order_book(symbol)
    return client.get_order_book(symbol)

def get_optimal_limit_price(symbol: str,
                           side: str,
                           urgency_factor: float = 0.5,
                           account: Optional[Union[Dict, HyperliquidAccount]] = None,
                           client: Optional[HyperliquidClient] = None) -> float:
    """Get optimal limit price by analyzing order book and urgency factor.
    
    Args:
        symbol (str): Trading pair symbol (e.g., "BTC")
        side (str): 'buy' or 'sell'
        urgency_factor (float): Urgency factor from 0.0 to 1.0. 
                              0.0 = very patient (far from market), 1.0 = very aggressive (close to market)
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials (not needed for price calculation)
        client (Optional[HyperliquidClient]): Existing client instance
        
    Returns:
        float: Optimal limit price
        
    Raises:
        ValueError: If parameters are invalid or order book data is unavailable
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_optimal_limit_price(symbol, side, urgency_factor)
    return client.get_optimal_limit_price(symbol, side, urgency_factor)

def get_spot_balance(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True) -> Union[Decimal, 'SpotState']:
    """Get spot trading balance for an address or authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_balance(address, simple=simple)
    return client.get_spot_balance(address, simple=simple)

def get_evm_balance(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
    """Get EVM chain balance for an address or authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_evm_balance(address, simple=simple)
    return client.get_evm_balance(address, simple=simple)

def get_all_balances(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
    """Get all balances (perp, spot, and EVM) for an address or authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_all_balances(address, simple=simple)
    return client.get_all_balances(address, simple=simple)

def get_market_info(symbol: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None) -> Union[Dict, List[Dict]]:
    """Get market information from the exchange."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_market_info(symbol)
    return client.get_market_info(symbol)

def cancel_all(account: Optional[Union[Dict, HyperliquidAccount]] = None,
               client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all open orders across all symbols."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.cancel_all()
    return client.cancel_all()
