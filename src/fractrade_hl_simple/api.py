import threading
import warnings
from typing import Optional, Dict, List, Union, Any, Sequence, Tuple
from decimal import Decimal
from contextlib import contextmanager
from .exceptions import InvalidArgumentException
from .hyperliquid import HyperliquidClient
from .models import (
    HyperliquidAccount,
    UserState,
    Position,
    Order,
)

@contextmanager
def get_client(account: Optional[Union[Dict, HyperliquidAccount]] = None) -> HyperliquidClient:
    """Context manager to handle client creation and cleanup."""
    client = HyperliquidClient(account=account)
    try:
        yield client
    finally:
        # Add any cleanup if needed
        pass

def get_user_state(address: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   dex: object = HyperliquidClient._SENTINEL_DEX) -> UserState:
    """Get user state information for a given address or authenticated user; see HyperliquidClient.get_user_state."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_user_state(address, dex=dex)
    return client.get_user_state(address, dex=dex)

def get_positions(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                 client: Optional[HyperliquidClient] = None, *,
                 address: Optional[str] = None) -> List[Position]:
    """Get open positions of a wallet (address), by default the authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_positions(address=address)
    return client.get_positions(address=address)

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
                     client: Optional[HyperliquidClient] = None, *,
                     simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
    """Get perpetual balance for a given address or authenticated user; see HyperliquidClient.get_perp_balance."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_perp_balance(address, simple=simple)
    return client.get_perp_balance(address, simple=simple)

def prepare_order(symbol: str,
                  size: float,
                  is_buy: bool,
                  limit_price: Optional[float] = None,
                  account: Optional[Union[Dict, HyperliquidAccount]] = None,
                  client: Optional[HyperliquidClient] = None, *,
                  slippage: Optional[float] = None) -> tuple[float, float]:
    """The size and limit price an order would be sent with, without sending it; see
    HyperliquidClient.prepare_order."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.prepare_order(symbol, size, is_buy, limit_price, slippage)
    return client.prepare_order(symbol, size, is_buy, limit_price, slippage)

def buy(symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        account: Optional[Union[Dict, HyperliquidAccount]] = None,
        client: Optional[HyperliquidClient] = None, *,
        reduce_only: bool = False,
        post_only: bool = False,
        slippage: Optional[float] = None,
        cloid: Optional[str] = None) -> Order:
    """Place a buy order (market or limit); see HyperliquidClient.buy."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.buy(symbol, size, limit_price, reduce_only=reduce_only, post_only=post_only,
                                  slippage=slippage, cloid=cloid)
    return client.buy(symbol, size, limit_price, reduce_only=reduce_only, post_only=post_only, slippage=slippage,
                      cloid=cloid)

def sell(symbol: str,
         size: float,
         limit_price: Optional[float] = None,
         account: Optional[Union[Dict, HyperliquidAccount]] = None,
         client: Optional[HyperliquidClient] = None, *,
         reduce_only: bool = False,
         post_only: bool = False,
         slippage: Optional[float] = None,
         cloid: Optional[str] = None) -> Order:
    """Place a sell order (market or limit); see HyperliquidClient.sell."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.sell(symbol, size, limit_price, reduce_only=reduce_only, post_only=post_only,
                                  slippage=slippage, cloid=cloid)
    return client.sell(symbol, size, limit_price, reduce_only=reduce_only, post_only=post_only, slippage=slippage,
                      cloid=cloid)

def close(symbol: str,
          account: Optional[Union[Dict, HyperliquidAccount]] = None,
          client: Optional[HyperliquidClient] = None, *,
          position: Optional[Position] = None,
          slippage: Optional[float] = None) -> Order:
    """Close the position of a symbol; see HyperliquidClient.close."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.close(symbol, position, slippage=slippage)
    return client.close(symbol, position, slippage=slippage)

def _renamed(trigger_price: Optional[float], old_value: Optional[float], old_name: str) -> float:
    """trigger_price, also when passed under its old name (deprecated, with a warning)."""
    if old_value is not None:
        if trigger_price is not None:
            raise InvalidArgumentException(f"pass trigger_price or {old_name}, not both")
        warnings.warn(f"{old_name} is deprecated; pass trigger_price", DeprecationWarning, stacklevel=3)
        return old_value
    if trigger_price is None:
        raise InvalidArgumentException("trigger_price is required")
    return trigger_price


def stop_loss(symbol: str,
              size: float,
              trigger_price: Optional[float] = None,
              is_buy: Optional[bool] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None, *,
              stop_price: Optional[float] = None,
              cloid: Optional[str] = None) -> Order:
    """Place a stop loss order; see HyperliquidClient.stop_loss. ``stop_price=`` is the deprecated name of
    ``trigger_price``."""
    trigger_price = _renamed(trigger_price, stop_price, "stop_price")
    if client is None:
        with get_client(account) as new_client:
            return new_client.stop_loss(symbol, size, trigger_price, is_buy, cloid=cloid)
    return client.stop_loss(symbol, size, trigger_price, is_buy, cloid=cloid)

def take_profit(symbol: str,
                size: float,
                trigger_price: Optional[float] = None,
                is_buy: Optional[bool] = None,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None, *,
                take_profit_price: Optional[float] = None,
                cloid: Optional[str] = None) -> Order:
    """Place a take profit order; see HyperliquidClient.take_profit. ``take_profit_price=`` is the deprecated name of
    ``trigger_price``."""
    trigger_price = _renamed(trigger_price, take_profit_price, "take_profit_price")
    if client is None:
        with get_client(account) as new_client:
            return new_client.take_profit(symbol, size, trigger_price, is_buy, cloid=cloid)
    return client.take_profit(symbol, size, trigger_price, is_buy, cloid=cloid)

def open_long_position(symbol: str,
                       size: float,
                       stop_loss_price: Optional[float] = None,
                       take_profit_price: Optional[float] = None,
                       account: Optional[Union[Dict, HyperliquidAccount]] = None,
                       client: Optional[HyperliquidClient] = None, *,
                       limit_price: Optional[float] = None,
                       slippage: Optional[float] = None,
                       stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
                       take_profits: Optional[Sequence[Tuple[float, float]]] = None,
                       time_in_force: Optional[str] = None) -> Dict[str, Order]:
    """Open a long position with optional stop loss and take profit orders; see HyperliquidClient.open_long_position."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.open_long_position(symbol, size, stop_loss_price, take_profit_price, limit_price=limit_price, slippage=slippage,
                              stop_losses=stop_losses, take_profits=take_profits, time_in_force=time_in_force)
    return client.open_long_position(symbol, size, stop_loss_price, take_profit_price, limit_price=limit_price, slippage=slippage,
                              stop_losses=stop_losses, take_profits=take_profits, time_in_force=time_in_force)

def open_short_position(symbol: str,
                        size: float,
                        stop_loss_price: Optional[float] = None,
                        take_profit_price: Optional[float] = None,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None, *,
                        limit_price: Optional[float] = None,
                        slippage: Optional[float] = None,
                        stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
                        take_profits: Optional[Sequence[Tuple[float, float]]] = None,
                        time_in_force: Optional[str] = None) -> Dict[str, Order]:
    """Open a short position with optional stop loss and take profit orders; see HyperliquidClient.open_short_position."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.open_short_position(symbol, size, stop_loss_price, take_profit_price, limit_price=limit_price, slippage=slippage,
                              stop_losses=stop_losses, take_profits=take_profits, time_in_force=time_in_force)
    return client.open_short_position(symbol, size, stop_loss_price, take_profit_price, limit_price=limit_price, slippage=slippage,
                              stop_losses=stop_losses, take_profits=take_profits, time_in_force=time_in_force)

def maker_order(symbol: str, is_buy: bool, size: float,
               timeout: Optional[float] = None, reprice_interval: Optional[float] = None,
               fallback: str = "ioc", reduce_only: bool = False,
               bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
               fill_event: Optional[threading.Event] = None,
               account: Optional[Union[Dict, HyperliquidAccount]] = None,
               client: Optional[HyperliquidClient] = None) -> Order:
    """Place a maker (post_only) order with automatic chase and fallback."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.maker_order(symbol, is_buy, size, timeout, reprice_interval, fallback, reduce_only,
                                          bbo_cache=bbo_cache, fill_event=fill_event)
    return client.maker_order(symbol, is_buy, size, timeout, reprice_interval, fallback, reduce_only,
                              bbo_cache=bbo_cache, fill_event=fill_event)

def maker_buy(symbol: str, size: float,
             timeout: Optional[float] = None, reprice_interval: Optional[float] = None,
             fallback: str = "ioc", reduce_only: bool = False,
             bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
             fill_event: Optional[threading.Event] = None,
             account: Optional[Union[Dict, HyperliquidAccount]] = None,
             client: Optional[HyperliquidClient] = None) -> Order:
    """Place a maker buy order with automatic chase and fallback."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.maker_buy(symbol, size, timeout, reprice_interval, fallback, reduce_only,
                                        bbo_cache=bbo_cache, fill_event=fill_event)
    return client.maker_buy(symbol, size, timeout, reprice_interval, fallback, reduce_only,
                            bbo_cache=bbo_cache, fill_event=fill_event)

def maker_sell(symbol: str, size: float,
              timeout: Optional[float] = None, reprice_interval: Optional[float] = None,
              fallback: str = "ioc", reduce_only: bool = False,
              bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
              fill_event: Optional[threading.Event] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None) -> Order:
    """Place a maker sell order with automatic chase and fallback."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.maker_sell(symbol, size, timeout, reprice_interval, fallback, reduce_only,
                                         bbo_cache=bbo_cache, fill_event=fill_event)
    return client.maker_sell(symbol, size, timeout, reprice_interval, fallback, reduce_only,
                             bbo_cache=bbo_cache, fill_event=fill_event)

def cancel_all_orders(symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all orders for a symbol or all symbols."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.cancel_all_orders(symbol)
    return client.cancel_all_orders(symbol)

def cancel_order(order_id: Union[str, int],
                symbol: str,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel a specific order by order ID and symbol.

    Args:
        order_id (Union[str, int]): The order ID to cancel
        symbol (str): The symbol the order is for
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials
        client (Optional[HyperliquidClient]): Existing client instance

    Returns:
        bool: True if cancelled, False if order not found or already processed.

    Raises:
        Exception: On network errors, auth failures, or other unexpected errors
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.cancel_order(order_id, symbol)
    return client.cancel_order(order_id, symbol)

def cancel_order_by_cloid(cloid: str,
                          symbol: str,
                          account: Optional[Union[Dict, HyperliquidAccount]] = None,
                          client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel an order by its client order id; see HyperliquidClient.cancel_order_by_cloid."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.cancel_order_by_cloid(cloid, symbol)
    return client.cancel_order_by_cloid(cloid, symbol)

def get_open_orders(symbol: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   address: Optional[str] = None) -> List[Order]:
    """Get all open orders of a wallet, by default the authenticated user.

    Args:
        symbol (Optional[str]): If provided, only returns orders for this symbol
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials
        client (Optional[HyperliquidClient]): Existing client instance
        address (Optional[str]): The wallet to read. Defaults to the authenticated user.

    Returns:
        List[Order]: List of open orders
    """
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_open_orders(symbol, address=address)
    return client.get_open_orders(symbol, address=address)

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
                   client: Optional[HyperliquidClient] = None, *,
                   decimal: bool = False) -> Dict[str, Any]:
    """Get the current order book (L2 snapshot) for a symbol.
    
    Args:
        symbol (str): Trading pair symbol (e.g., "BTC")
        account (Optional[Union[Dict, HyperliquidAccount]]): Account credentials (not needed for order book)
        client (Optional[HyperliquidClient]): Existing client instance
        decimal (bool): Prices and sizes as Decimal instead of float
        
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
            return new_client.get_order_book(symbol, decimal=decimal)
    return client.get_order_book(symbol, decimal=decimal)

def get_optimal_limit_price(symbol: str,
                           side: str,
                           urgency_factor: float = 0.5,
                           bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
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
            return new_client.get_optimal_limit_price(symbol, side, urgency_factor, bbo_cache=bbo_cache)
    return client.get_optimal_limit_price(symbol, side, urgency_factor, bbo_cache=bbo_cache)

def get_spot_balance(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True,
                    prices: Optional[Dict] = None) -> Union[Decimal, 'SpotState']:
    """Get spot trading balance for an address or authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_balance(address, simple=simple, prices=prices)
    return client.get_spot_balance(address, simple=simple, prices=prices)

def get_account_summary(address: Optional[str] = None,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None):
    """An account's balances by its account mode (see HyperliquidClient.get_account_summary)."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_account_summary(address)
    return client.get_account_summary(address)

def get_all_balances(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
    """Get the total account value (perp and spot) for an address or authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_all_balances(address, simple=simple)
    return client.get_all_balances(address, simple=simple)

def get_markets(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> list:
    """Every perp market with its current figures (see HyperliquidClient.get_markets)."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_markets()
    return client.get_markets()

def get_spot_markets(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> list:
    """Every spot pair with its current figures (see HyperliquidClient.get_spot_markets)."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_markets()
    return client.get_spot_markets()

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

def set_leverage(symbol: str,
                 leverage: int,
                 is_cross: bool = True,
                 account: Optional[Union[Dict, HyperliquidAccount]] = None,
                 client: Optional[HyperliquidClient] = None) -> dict:
    """Set leverage for a symbol."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.set_leverage(symbol, leverage, is_cross)
    return client.set_leverage(symbol, leverage, is_cross)

def add_isolated_margin(symbol: str,
                        amount: float,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None) -> dict:
    """Add margin to an isolated position."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.add_isolated_margin(symbol, amount)
    return client.add_isolated_margin(symbol, amount)

def get_fills(symbol: Optional[str] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None, *,
              address: Optional[str] = None) -> list:
    """Get recent fills of a wallet (address), by default the authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_fills(symbol, address=address)
    return client.get_fills(symbol, address=address)

def get_fills_by_time(start_time: int,
                      end_time: Optional[int] = None,
                      symbol: Optional[str] = None,
                      account: Optional[Union[Dict, HyperliquidAccount]] = None,
                      client: Optional[HyperliquidClient] = None, *,
                      address: Optional[str] = None) -> list:
    """Get fills of a wallet (address, by default the authenticated user) within a time range."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_fills_by_time(start_time, end_time, symbol, address=address)
    return client.get_fills_by_time(start_time, end_time, symbol, address=address)

def get_user_funding(start_time: int,
                     end_time: Optional[int] = None,
                     symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     address: Optional[str] = None) -> list:
    """The funding payments of a wallet (address, by default the authenticated user) in a time range."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_user_funding(start_time, end_time, symbol, address=address)
    return client.get_user_funding(start_time, end_time, symbol, address=address)

def get_fill_history(start_time: int,
                     end_time: Optional[int] = None,
                     symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     address: Optional[str] = None):
    """The fills of a time range and whether Hyperliquid's limits cut it short (see HyperliquidClient.get_fill_history)."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_fill_history(start_time, end_time, symbol, address=address)
    return client.get_fill_history(start_time, end_time, symbol, address=address)

def get_order_status(order_id: Union[int, str],
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     address: Optional[str] = None) -> dict:
    """Query the status of an order of a wallet (address), by default the authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_order_status(order_id, address=address)
    return client.get_order_status(order_id, address=address)

def get_order_status_by_cloid(cloid: str,
                              account: Optional[Union[Dict, HyperliquidAccount]] = None,
                              client: Optional[HyperliquidClient] = None, *,
                              address: Optional[str] = None) -> dict:
    """Query the status of an order by its client order id; see HyperliquidClient.get_order_status_by_cloid."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_order_status_by_cloid(cloid, address=address)
    return client.get_order_status_by_cloid(cloid, address=address)

def bulk_order(orders: List[dict],
               account: Optional[Union[Dict, HyperliquidAccount]] = None,
               client: Optional[HyperliquidClient] = None) -> dict:
    """Place several orders in one action; see HyperliquidClient.bulk_order."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.bulk_order(orders)
    return client.bulk_order(orders)

def bulk_cancel(cancels: List[dict],
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> dict:
    """Cancel multiple orders atomically."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.bulk_cancel(cancels)
    return client.bulk_cancel(cancels)

def get_funding_history(symbol: str,
                        start_time: int,
                        end_time: Optional[int] = None,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None) -> List[dict]:
    """Get historical funding rates for a symbol."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_funding_history(symbol, start_time, end_time)
    return client.get_funding_history(symbol, start_time, end_time)

def get_portfolio(address: Optional[str] = None,
                  account: Optional[Union[Dict, HyperliquidAccount]] = None,
                  client: Optional[HyperliquidClient] = None) -> dict:
    """Get portfolio performance metrics."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_portfolio(address)
    return client.get_portfolio(address)


# ── Spot trading ──────────────────────────────────────────────────────

def transfer_to_spot(amount: float,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> dict:
    """Transfer USDC from perp wallet to spot wallet."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.transfer_to_spot(amount)
    return client.transfer_to_spot(amount)

def transfer_to_perp(amount: float,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> dict:
    """Transfer USDC from spot wallet to perp wallet."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.transfer_to_perp(amount)
    return client.transfer_to_perp(amount)

def get_spot_price(token: str,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None) -> float:
    """Get current mid price for a spot token."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_price(token)
    return client.get_spot_price(token)

def spot_buy(token: str,
             size: float,
             limit_price: Optional[float] = None,
             account: Optional[Union[Dict, HyperliquidAccount]] = None,
             client: Optional[HyperliquidClient] = None, *,
             post_only: bool = False,
             slippage: Optional[float] = None,
             cloid: Optional[str] = None) -> Order:
    """Buy a spot token (market or limit); see HyperliquidClient.spot_buy."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.spot_buy(token, size, limit_price, post_only=post_only, slippage=slippage, cloid=cloid)
    return client.spot_buy(token, size, limit_price, post_only=post_only, slippage=slippage, cloid=cloid)

def spot_sell(token: str,
              size: float,
              limit_price: Optional[float] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None, *,
              post_only: bool = False,
              slippage: Optional[float] = None,
              cloid: Optional[str] = None) -> Order:
    """Sell a spot token (market or limit); see HyperliquidClient.spot_sell."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.spot_sell(token, size, limit_price, post_only=post_only, slippage=slippage, cloid=cloid)
    return client.spot_sell(token, size, limit_price, post_only=post_only, slippage=slippage, cloid=cloid)

def spot_cancel_order(order_id: Union[str, int],
                      token: str,
                      account: Optional[Union[Dict, HyperliquidAccount]] = None,
                      client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel a specific spot order."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.spot_cancel_order(order_id, token)
    return client.spot_cancel_order(order_id, token)

def spot_cancel_all_orders(token: Optional[str] = None,
                           account: Optional[Union[Dict, HyperliquidAccount]] = None,
                           client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all open spot orders."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.spot_cancel_all_orders(token)
    return client.spot_cancel_all_orders(token)

def get_spot_open_orders(token: Optional[str] = None,
                         account: Optional[Union[Dict, HyperliquidAccount]] = None,
                         client: Optional[HyperliquidClient] = None, *,
                         address: Optional[str] = None) -> List[Order]:
    """Get open spot orders of a wallet (address), by default the authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_open_orders(token, address=address)
    return client.get_spot_open_orders(token, address=address)

def get_spot_order_book(token: str,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None, *,
                        decimal: bool = False) -> Dict:
    """Get the order book for a spot token; see HyperliquidClient.get_spot_order_book."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_order_book(token, decimal=decimal)
    return client.get_spot_order_book(token, decimal=decimal)

def get_spot_fills(token: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   address: Optional[str] = None) -> list:
    """Get recent spot fills of a wallet (address), by default the authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_spot_fills(token, address=address)
    return client.get_spot_fills(token, address=address)
