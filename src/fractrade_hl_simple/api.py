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
            return new_client.cancel_order(order_id, symbol)
    return client.cancel_order(order_id, symbol)

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
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> Union[float, List[Dict[str, Any]]]:
    """Get funding rates for all tokens or a specific symbol.
    
    Args:
        symbol (Optional[str]): If provided, returns funding rate for specific symbol.
                              If None, returns funding rates for all tokens sorted by value.
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials (not needed for funding rates)
        client (Optional[HyperliquidClient]): Existing client instance
        
    Returns:
        Union[float, List[Dict[str, Any]]]: 
            - If symbol is provided: float funding rate for the symbol
            - If symbol is None: List of dicts with symbol and funding rate, sorted from highest positive to lowest negative
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_funding_rates(symbol)
    return client.get_funding_rates(symbol)
