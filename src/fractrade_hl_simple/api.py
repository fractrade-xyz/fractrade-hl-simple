"""Module-level functions: each one calls the HyperliquidClient method of the same name.

A function uses `client`, or without one a new client for `account` (a
HyperliquidAccount, or a dict of its fields; without it the credentials come from the
environment, as in HyperliquidClient). The arguments, results and errors are those of
the method.
"""

import threading
import warnings
from typing import ContextManager, Optional, Dict, Iterator, List, Union, Any, Sequence, Tuple
from decimal import Decimal
from contextlib import contextmanager, nullcontext
from .exceptions import InvalidArgumentException
from ._inputs import TimeInForce
from .hyperliquid import HyperliquidClient
from .models import (
    HyperliquidAccount,
    UserState,
    Position,
    Order,
    SpotState,
)

@contextmanager
def get_client(account: Optional[Union[Dict, HyperliquidAccount]] = None) -> Iterator[HyperliquidClient]:
    """A new client for `account`, as a context manager (a client has nothing to close). A dict
    account holds the HyperliquidAccount fields: private_key and public_address, and is_vault
    (default False)."""
    if isinstance(account, dict):
        try:
            account = HyperliquidAccount(**account)
        except TypeError:  # a missing or unknown key; the message names no value
            raise InvalidArgumentException(
                "an account dict takes the keys private_key, public_address and is_vault") from None
    yield HyperliquidClient(account=account)

def _using(account: Optional[Union[Dict, HyperliquidAccount]],
           client: Optional[HyperliquidClient]) -> ContextManager[HyperliquidClient]:
    """`client`, or without one a new client for `account` (through get_client)."""
    return nullcontext(client) if client is not None else get_client(account)

def get_user_state(address: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   dex: object = HyperliquidClient._SENTINEL_DEX) -> UserState:
    """A wallet's perp account state; see HyperliquidClient.get_user_state."""
    with _using(account, client) as hl:
        return hl.get_user_state(address, dex=dex)

def get_positions(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                 client: Optional[HyperliquidClient] = None, *,
                 address: Optional[str] = None) -> List[Position]:
    """Get open positions of a wallet (address), by default the authenticated user."""
    with _using(account, client) as hl:
        return hl.get_positions(address=address)

def get_price(symbol: Optional[str] = None,
             account: Optional[Union[Dict, HyperliquidAccount]] = None,
             client: Optional[HyperliquidClient] = None) -> Union[float, Dict[str, float]]:
    """Get current price for a symbol or all symbols."""
    with _using(account, client) as hl:
        return hl.get_price(symbol)

def get_perp_balance(address: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
    """The balance that perp positions trade against; see HyperliquidClient.get_perp_balance."""
    with _using(account, client) as hl:
        return hl.get_perp_balance(address, simple=simple)

def prepare_order(symbol: str,
                  size: float,
                  is_buy: bool,
                  limit_price: Optional[float] = None,
                  account: Optional[Union[Dict, HyperliquidAccount]] = None,
                  client: Optional[HyperliquidClient] = None, *,
                  slippage: Optional[float] = None) -> tuple[float, float]:
    """The size and limit price an order would be sent with, without sending it; see
    HyperliquidClient.prepare_order."""
    with _using(account, client) as hl:
        return hl.prepare_order(symbol, size, is_buy, limit_price, slippage)

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
    with _using(account, client) as hl:
        return hl.buy(symbol, size, limit_price, reduce_only=reduce_only, post_only=post_only,
                      slippage=slippage, cloid=cloid)

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
    with _using(account, client) as hl:
        return hl.sell(symbol, size, limit_price, reduce_only=reduce_only, post_only=post_only,
                       slippage=slippage, cloid=cloid)

def close(symbol: str,
          account: Optional[Union[Dict, HyperliquidAccount]] = None,
          client: Optional[HyperliquidClient] = None, *,
          position: Optional[Position] = None,
          slippage: Optional[float] = None) -> Order:
    """Close the position of a symbol; see HyperliquidClient.close."""
    with _using(account, client) as hl:
        return hl.close(symbol, position, slippage=slippage)

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
    """Place a stop loss order; see HyperliquidClient.stop_loss. ``stop_price=`` is the
    deprecated name of ``trigger_price``."""
    trigger_price = _renamed(trigger_price, stop_price, "stop_price")
    with _using(account, client) as hl:
        return hl.stop_loss(symbol, size, trigger_price, is_buy, cloid=cloid)

def take_profit(symbol: str,
                size: float,
                trigger_price: Optional[float] = None,
                is_buy: Optional[bool] = None,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None, *,
                take_profit_price: Optional[float] = None,
                cloid: Optional[str] = None) -> Order:
    """Place a take profit order; see HyperliquidClient.take_profit. ``take_profit_price=`` is
    the deprecated name of ``trigger_price``."""
    trigger_price = _renamed(trigger_price, take_profit_price, "take_profit_price")
    with _using(account, client) as hl:
        return hl.take_profit(symbol, size, trigger_price, is_buy, cloid=cloid)

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
                       time_in_force: Optional[TimeInForce] = None) -> Dict[str, Order]:
    """Open a long position with optional stop loss and take profit orders; see
    HyperliquidClient.open_long_position."""
    with _using(account, client) as hl:
        return hl.open_long_position(
            symbol, size, stop_loss_price, take_profit_price, limit_price=limit_price, slippage=slippage,
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
                        time_in_force: Optional[TimeInForce] = None) -> Dict[str, Order]:
    """Open a short position with optional stop loss and take profit orders; see
    HyperliquidClient.open_short_position."""
    with _using(account, client) as hl:
        return hl.open_short_position(
            symbol, size, stop_loss_price, take_profit_price, limit_price=limit_price, slippage=slippage,
            stop_losses=stop_losses, take_profits=take_profits, time_in_force=time_in_force)

def trigger_order(symbol: str, is_buy: bool, size: float, trigger_price: float,
                  account: Optional[Union[Dict, HyperliquidAccount]] = None,
                  client: Optional[HyperliquidClient] = None, *,
                  limit_price: Optional[float] = None,
                  cloid: Optional[str] = None) -> Order:
    """A stop order that opens or adds to a position; see HyperliquidClient.trigger_order."""
    with _using(account, client) as hl:
        return hl.trigger_order(symbol, is_buy, size, trigger_price, limit_price=limit_price, cloid=cloid)

def maker_order(symbol: str, is_buy: bool, size: float,
               timeout: Optional[float] = None, reprice_interval: Optional[float] = None,
               fallback: str = "ioc", reduce_only: bool = False,
               bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
               fill_event: Optional[threading.Event] = None,
               account: Optional[Union[Dict, HyperliquidAccount]] = None,
               client: Optional[HyperliquidClient] = None) -> Order:
    """Place a maker (post_only) order with automatic chase and fallback; see
    HyperliquidClient.maker_order."""
    with _using(account, client) as hl:
        return hl.maker_order(symbol, is_buy, size, timeout, reprice_interval, fallback, reduce_only,
                              bbo_cache=bbo_cache, fill_event=fill_event)

def maker_buy(symbol: str, size: float,
             timeout: Optional[float] = None, reprice_interval: Optional[float] = None,
             fallback: str = "ioc", reduce_only: bool = False,
             bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
             fill_event: Optional[threading.Event] = None,
             account: Optional[Union[Dict, HyperliquidAccount]] = None,
             client: Optional[HyperliquidClient] = None) -> Order:
    """Place a maker buy order with automatic chase and fallback."""
    with _using(account, client) as hl:
        return hl.maker_buy(symbol, size, timeout, reprice_interval, fallback, reduce_only,
                            bbo_cache=bbo_cache, fill_event=fill_event)

def maker_sell(symbol: str, size: float,
              timeout: Optional[float] = None, reprice_interval: Optional[float] = None,
              fallback: str = "ioc", reduce_only: bool = False,
              bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
              fill_event: Optional[threading.Event] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None) -> Order:
    """Place a maker sell order with automatic chase and fallback."""
    with _using(account, client) as hl:
        return hl.maker_sell(symbol, size, timeout, reprice_interval, fallback, reduce_only,
                             bbo_cache=bbo_cache, fill_event=fill_event)

def cancel_all_orders(symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all open perp orders on the client's dexes, stop losses and take profits
    included, optionally of one symbol; spot orders stay open. See
    HyperliquidClient.cancel_all_orders."""
    with _using(account, client) as hl:
        return hl.cancel_all_orders(symbol)

def cancel_order(order_id: Union[str, int],
                symbol: str,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel an order by its order id: True if canceled, False if it is not open; see
    HyperliquidClient.cancel_order."""
    with _using(account, client) as hl:
        return hl.cancel_order(order_id, symbol)

def cancel_order_by_cloid(cloid: str,
                          symbol: str,
                          account: Optional[Union[Dict, HyperliquidAccount]] = None,
                          client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel an order by its client order id; see HyperliquidClient.cancel_order_by_cloid."""
    with _using(account, client) as hl:
        return hl.cancel_order_by_cloid(cloid, symbol)

def get_open_orders(symbol: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   address: Optional[str] = None) -> List[Order]:
    """The open orders of a wallet, optionally of one symbol; see
    HyperliquidClient.get_open_orders."""
    with _using(account, client) as hl:
        return hl.get_open_orders(symbol, address=address)

def get_funding_rates(symbol: Optional[str] = None,
                     threshold: Optional[float] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> Union[float, List[Dict[str, Any]]]:
    """The predicted funding rate of a symbol, or without one those of every market; see
    HyperliquidClient.get_funding_rates."""
    with _using(account, client) as hl:
        return hl.get_funding_rates(symbol, threshold)

def get_order_book(symbol: str,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   decimal: bool = False) -> Dict[str, Any]:
    """The current order book (L2 snapshot) of a market; see HyperliquidClient.get_order_book."""
    with _using(account, client) as hl:
        return hl.get_order_book(symbol, decimal=decimal)

def get_optimal_limit_price(symbol: str,
                           side: str,
                           urgency_factor: float = 0.5,
                           bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
                           account: Optional[Union[Dict, HyperliquidAccount]] = None,
                           client: Optional[HyperliquidClient] = None) -> float:
    """A limit price between the best bid and the best ask, by how urgent the order is; see
    HyperliquidClient.get_optimal_limit_price."""
    with _using(account, client) as hl:
        return hl.get_optimal_limit_price(symbol, side, urgency_factor, bbo_cache=bbo_cache)

def get_spot_balance(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True,
                    prices: Optional[Dict] = None) -> Union[Decimal, SpotState]:
    """Get spot trading balance for an address or authenticated user."""
    with _using(account, client) as hl:
        return hl.get_spot_balance(address, simple=simple, prices=prices)

def get_account_summary(address: Optional[str] = None,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None):
    """An account's balances by its account mode (see HyperliquidClient.get_account_summary)."""
    with _using(account, client) as hl:
        return hl.get_account_summary(address)

def get_all_balances(address: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None,
                    simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
    """Get the total account value (perp and spot) for an address or authenticated user."""
    with _using(account, client) as hl:
        return hl.get_all_balances(address, simple=simple)

def get_markets(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> list:
    """Every perp market with its current figures (see HyperliquidClient.get_markets)."""
    with _using(account, client) as hl:
        return hl.get_markets()

def get_spot_markets(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> list:
    """Every spot pair with its current figures (see HyperliquidClient.get_spot_markets)."""
    with _using(account, client) as hl:
        return hl.get_spot_markets()

def get_market_info(symbol: Optional[str] = None,
                    account: Optional[Union[Dict, HyperliquidAccount]] = None,
                    client: Optional[HyperliquidClient] = None) -> Union[Dict, List[Dict]]:
    """Get market information from the exchange."""
    with _using(account, client) as hl:
        return hl.get_market_info(symbol)

def cancel_all(account: Optional[Union[Dict, HyperliquidAccount]] = None,
               client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all open perp orders on the client's dexes; spot orders stay open. Alias for
    cancel_all_orders(), which new code should use."""
    with _using(account, client) as hl:
        return hl.cancel_all()

def set_leverage(symbol: str,
                 leverage: int,
                 is_cross: bool = True,
                 account: Optional[Union[Dict, HyperliquidAccount]] = None,
                 client: Optional[HyperliquidClient] = None) -> dict:
    """Set leverage for a symbol."""
    with _using(account, client) as hl:
        return hl.set_leverage(symbol, leverage, is_cross)

def add_isolated_margin(symbol: str,
                        amount: float,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None) -> dict:
    """Add margin to an isolated position."""
    with _using(account, client) as hl:
        return hl.add_isolated_margin(symbol, amount)

def get_fills(symbol: Optional[str] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None, *,
              address: Optional[str] = None) -> list:
    """Get recent fills of a wallet (address), by default the authenticated user."""
    with _using(account, client) as hl:
        return hl.get_fills(symbol, address=address)

def get_fills_by_time(start_time: int,
                      end_time: Optional[int] = None,
                      symbol: Optional[str] = None,
                      account: Optional[Union[Dict, HyperliquidAccount]] = None,
                      client: Optional[HyperliquidClient] = None, *,
                      address: Optional[str] = None) -> list:
    """Get fills of a wallet (address, by default the authenticated user) within a time range."""
    with _using(account, client) as hl:
        return hl.get_fills_by_time(start_time, end_time, symbol, address=address)

def get_user_funding(start_time: int,
                     end_time: Optional[int] = None,
                     symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     address: Optional[str] = None) -> list:
    """The funding payments of a wallet (address, by default the authenticated user) in a time range."""
    with _using(account, client) as hl:
        return hl.get_user_funding(start_time, end_time, symbol, address=address)

def get_fill_history(start_time: int,
                     end_time: Optional[int] = None,
                     symbol: Optional[str] = None,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     address: Optional[str] = None):
    """The fills of a time range and whether Hyperliquid's limits cut it short; see
    HyperliquidClient.get_fill_history."""
    with _using(account, client) as hl:
        return hl.get_fill_history(start_time, end_time, symbol, address=address)

def get_pnl(start_time: int,
            end_time: Optional[int] = None,
            symbol: Optional[str] = None,
            account: Optional[Union[Dict, HyperliquidAccount]] = None,
            client: Optional[HyperliquidClient] = None, *,
            spot: bool = False,
            address: Optional[str] = None):
    """A wallet's perp trades of a time range with their PnL (see HyperliquidClient.get_pnl)."""
    with _using(account, client) as hl:
        return hl.get_pnl(start_time, end_time, symbol, spot=spot, address=address)

def get_historical_orders(account: Optional[Union[Dict, HyperliquidAccount]] = None,
                          client: Optional[HyperliquidClient] = None, *,
                          address: Optional[str] = None) -> list:
    """A wallet's recent orders with their status (see HyperliquidClient.get_historical_orders)."""
    with _using(account, client) as hl:
        return hl.get_historical_orders(address=address)

def get_order_status(order_id: Union[int, str],
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None, *,
                     address: Optional[str] = None) -> dict:
    """Query the status of an order of a wallet (address), by default the authenticated user."""
    with _using(account, client) as hl:
        return hl.get_order_status(order_id, address=address)

def get_order_status_by_cloid(cloid: str,
                              account: Optional[Union[Dict, HyperliquidAccount]] = None,
                              client: Optional[HyperliquidClient] = None, *,
                              address: Optional[str] = None) -> dict:
    """Query the status of an order by its client order id; see
    HyperliquidClient.get_order_status_by_cloid."""
    with _using(account, client) as hl:
        return hl.get_order_status_by_cloid(cloid, address=address)

def bulk_order(orders: List[dict],
               account: Optional[Union[Dict, HyperliquidAccount]] = None,
               client: Optional[HyperliquidClient] = None) -> dict:
    """Place several orders in one action; see HyperliquidClient.bulk_order."""
    with _using(account, client) as hl:
        return hl.bulk_order(orders)

def bulk_cancel(cancels: List[dict],
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> dict:
    """Cancel several orders in one action; see HyperliquidClient.bulk_cancel."""
    with _using(account, client) as hl:
        return hl.bulk_cancel(cancels)

def get_candles(symbol: str,
                interval: str,
                start_time: Optional[int] = None,
                end_time: Optional[int] = None,
                lookback: Optional[int] = None,
                account: Optional[Union[Dict, HyperliquidAccount]] = None,
                client: Optional[HyperliquidClient] = None) -> list:
    """Candles of a market, oldest first (see HyperliquidClient.get_candles)."""
    with _using(account, client) as hl:
        return hl.get_candles(symbol, interval, start_time, end_time, lookback)

def get_funding_history(symbol: str,
                        start_time: int,
                        end_time: Optional[int] = None,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None) -> List[dict]:
    """Get historical funding rates for a symbol."""
    with _using(account, client) as hl:
        return hl.get_funding_history(symbol, start_time, end_time)

def get_portfolio(address: Optional[str] = None,
                  account: Optional[Union[Dict, HyperliquidAccount]] = None,
                  client: Optional[HyperliquidClient] = None) -> dict:
    """Get portfolio performance metrics."""
    with _using(account, client) as hl:
        return hl.get_portfolio(address)


# ── Spot trading ──────────────────────────────────────────────────────

def transfer_to_spot(amount: float,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> dict:
    """Transfer USDC from perp wallet to spot wallet."""
    with _using(account, client) as hl:
        return hl.transfer_to_spot(amount)

def transfer_to_perp(amount: float,
                     account: Optional[Union[Dict, HyperliquidAccount]] = None,
                     client: Optional[HyperliquidClient] = None) -> dict:
    """Transfer USDC from spot wallet to perp wallet."""
    with _using(account, client) as hl:
        return hl.transfer_to_perp(amount)

def get_spot_price(token: str,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None) -> float:
    """Get current mid price for a spot token."""
    with _using(account, client) as hl:
        return hl.get_spot_price(token)

def spot_buy(token: str,
             size: float,
             limit_price: Optional[float] = None,
             account: Optional[Union[Dict, HyperliquidAccount]] = None,
             client: Optional[HyperliquidClient] = None, *,
             post_only: bool = False,
             slippage: Optional[float] = None,
             cloid: Optional[str] = None) -> Order:
    """Buy a spot token (market or limit); see HyperliquidClient.spot_buy."""
    with _using(account, client) as hl:
        return hl.spot_buy(token, size, limit_price, post_only=post_only, slippage=slippage,
                           cloid=cloid)

def spot_sell(token: str,
              size: float,
              limit_price: Optional[float] = None,
              account: Optional[Union[Dict, HyperliquidAccount]] = None,
              client: Optional[HyperliquidClient] = None, *,
              post_only: bool = False,
              slippage: Optional[float] = None,
              cloid: Optional[str] = None) -> Order:
    """Sell a spot token (market or limit); see HyperliquidClient.spot_sell."""
    with _using(account, client) as hl:
        return hl.spot_sell(token, size, limit_price, post_only=post_only, slippage=slippage,
                            cloid=cloid)

def spot_cancel_order(order_id: Union[str, int],
                      token: str,
                      account: Optional[Union[Dict, HyperliquidAccount]] = None,
                      client: Optional[HyperliquidClient] = None) -> bool:
    """Cancel a specific spot order."""
    with _using(account, client) as hl:
        return hl.spot_cancel_order(order_id, token)

def spot_cancel_all_orders(token: Optional[str] = None,
                           account: Optional[Union[Dict, HyperliquidAccount]] = None,
                           client: Optional[HyperliquidClient] = None) -> None:
    """Cancel all open spot orders."""
    with _using(account, client) as hl:
        return hl.spot_cancel_all_orders(token)

def get_spot_open_orders(token: Optional[str] = None,
                         account: Optional[Union[Dict, HyperliquidAccount]] = None,
                         client: Optional[HyperliquidClient] = None, *,
                         address: Optional[str] = None) -> List[Order]:
    """Get open spot orders of a wallet (address), by default the authenticated user."""
    with _using(account, client) as hl:
        return hl.get_spot_open_orders(token, address=address)

def get_spot_order_book(token: str,
                        account: Optional[Union[Dict, HyperliquidAccount]] = None,
                        client: Optional[HyperliquidClient] = None, *,
                        decimal: bool = False) -> Dict:
    """Get the order book for a spot token; see HyperliquidClient.get_spot_order_book."""
    with _using(account, client) as hl:
        return hl.get_spot_order_book(token, decimal=decimal)

def get_spot_fills(token: Optional[str] = None,
                   account: Optional[Union[Dict, HyperliquidAccount]] = None,
                   client: Optional[HyperliquidClient] = None, *,
                   address: Optional[str] = None) -> list:
    """Get recent spot fills of a wallet (address), by default the authenticated user."""
    with _using(account, client) as hl:
        return hl.get_spot_fills(token, address=address)
