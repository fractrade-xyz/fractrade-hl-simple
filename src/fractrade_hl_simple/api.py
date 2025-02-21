from typing import Optional, Dict, List, Union, Literal
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
    env = os.getenv("HYPERLIQUID_ENV", "testnet")  # Get env from .env file
    client = HyperliquidClient(account=account, env=env)
    try:
        yield client
    finally:
        # Add any cleanup if needed
        pass

def get_user_state(address: Optional[str] = None) -> UserState:
    """Get user state information."""
    client = HyperliquidClient()  # Remove env parameter
    return client.get_user_state(address)

def get_positions(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                 client: Optional[HyperliquidClient] = None) -> List[Position]:
    """Get open positions for authenticated user."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.get_positions()
    return client.get_positions()

def create_order(
    coin: str,
    amount: float = 1.0,
    is_buy: bool = True,
    limit_price: Optional[float] = None,
    reduce_only: bool = False,
    post_only: bool = False,
    time_in_force: Literal["GTC", "IOC", "FOK"] = "GTC",
    close_position: bool = False,
    account: Optional[Union[Dict, HyperliquidAccount]] = None,
    client: Optional[HyperliquidClient] = None,
) -> Order:
    """Create a new order."""
    if client is None:
        with get_client(account) as new_client:
            return new_client.create_order(
                coin=coin,
                amount=amount,
                is_buy=is_buy,
                limit_price=limit_price,
                reduce_only=reduce_only,
                post_only=post_only,
                time_in_force=time_in_force,
                close_position=close_position,
            )
    return client.create_order(
        coin=coin,
        amount=amount,
        is_buy=is_buy,
        limit_price=limit_price,
        reduce_only=reduce_only,
        post_only=post_only,
        time_in_force=time_in_force,
        close_position=close_position,
    )

# Convenience functions with proper return types
def buy_market(coin: str, amount: float, **kwargs) -> Order:
    return create_order(coin=coin, amount=amount, is_buy=True, limit_price=None, **kwargs)

def sell_market(coin: str, amount: float, **kwargs) -> Order:
    return create_order(coin=coin, amount=amount, is_buy=False, limit_price=None, **kwargs)

def buy_limit(coin: str, amount: float, limit_price: float, **kwargs) -> Order:
    return create_order(coin=coin, amount=amount, is_buy=True, limit_price=limit_price, **kwargs)

def sell_limit(coin: str, amount: float, limit_price: float, **kwargs) -> Order:
    return create_order(coin=coin, amount=amount, is_buy=False, limit_price=limit_price, **kwargs)

def close_long(coin: str, **kwargs) -> Order:
    return create_order(coin=coin, is_buy=False, close_position=True, reduce_only=True, **kwargs)

def close_short(coin: str, **kwargs) -> Order:
    return create_order(coin=coin, is_buy=True, close_position=True, reduce_only=True, **kwargs)
