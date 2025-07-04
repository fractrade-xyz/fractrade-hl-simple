from typing import Optional, Dict, List, Union, Literal, Any
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
from hyperliquid.info import Info
from .models import (
    HyperliquidAccount, 
    UserState, 
    Position, 
    Order,
    DACITE_CONFIG,
    convert_api_response,
    MARKET_SPECS,
    get_current_market_specs,
    print_market_specs_diff,
    SpotState,
    SpotTokenBalance
)
from functools import partialmethod
import time
import os
from dacite import from_dict, Config as DaciteConfig
import eth_account
import logging
from decimal import Decimal
import threading
import requests
import json

# Set up logger
logger = logging.getLogger("fractrade_hl_simple")
logger.addHandler(logging.NullHandler())

class HyperliquidClient:
    def __init__(self, account: Optional[HyperliquidAccount] = None, env: str = "mainnet"):
        """Initialize HyperliquidClient.
        
        Args:
            account (Optional[HyperliquidAccount]): Account credentials. If None, tries to load from environment.
            env (str): The environment to use, either "mainnet" or "testnet". Defaults to "mainnet".
            
        Raises:
            ValueError: If env is not 'mainnet' or 'testnet'
        """
        # Validate environment
        if env not in ["mainnet", "testnet"]:
            raise ValueError("env must be either 'mainnet' or 'testnet'")
            
        # Set up environment
        self.env = env
        self.base_url = constants.TESTNET_API_URL if env == "testnet" else constants.MAINNET_API_URL
        self.info = Info(self.base_url, skip_ws=True)
        
        # Initialize market specs
        try:
            self.market_specs = self._fetch_market_specs()
        except Exception as e:
            logging.warning(f"Failed to fetch market specs: {e}. Using default specs.")
            self.market_specs = MARKET_SPECS

        # Try to set up authenticated client
        try:
            if account is not None:
                self._setup_authenticated_client(account)
            else:
                # Try loading from environment
                env_account = HyperliquidAccount.from_env()
                self._setup_authenticated_client(env_account)
        except (ValueError, KeyError, TypeError) as e:
            # If authentication fails, log warning and continue in unauthenticated mode
            logging.debug(
                f"Running in unauthenticated mode. Only public endpoints available. Error: {str(e)}"
            )

    def _setup_authenticated_client(self, account: HyperliquidAccount):
        """Set up authenticated client with account details."""
        # Validate account
        if not isinstance(account, HyperliquidAccount):
            raise TypeError("account must be an instance of HyperliquidAccount")
        
        if not account.public_address:
            raise ValueError("public_address is required")
        
        if not account.private_key:
            raise ValueError("private_key is required")

        # Set up authenticated client
        self.account = account
        self.exchange_account = eth_account.Account.from_key(account.private_key)
        self.public_address = account.public_address
        
        # Initialize exchange
        self.exchange = Exchange(self.exchange_account, self.base_url)

    def is_authenticated(self) -> bool:
        """Check if the client is authenticated with valid credentials.
        
        Returns:
            bool: True if client has valid account credentials, False otherwise
        """
        return (
            hasattr(self, 'account') and 
            self.account is not None and 
            self.account.private_key is not None and 
            self.account.public_address is not None
        )

    def get_user_state(self, address: Optional[str] = None) -> UserState:
        """Get the state of any user by their address."""
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")
            
        if address is None:
            address = self.public_address
            
        # Add address validation
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("Invalid address format")
            
        response = self.info.user_state(address)
        
        # Format the response to match our data structure
        formatted_response = {
            "asset_positions": [],  # Initialize with empty list if no positions
            "margin_summary": {
                "account_value": response.get("marginSummary", {}).get("accountValue", "0"),
                "total_margin_used": response.get("marginSummary", {}).get("totalMarginUsed", "0"),
                "total_ntl_pos": response.get("marginSummary", {}).get("totalNtlPos", "0"),
                "total_raw_usd": response.get("marginSummary", {}).get("totalRawUsd", "0")
            },
            "cross_margin_summary": {
                "account_value": response.get("crossMarginSummary", {}).get("accountValue", "0"),
                "total_margin_used": response.get("crossMarginSummary", {}).get("totalMarginUsed", "0"),
                "total_ntl_pos": response.get("crossMarginSummary", {}).get("totalNtlPos", "0"),
                "total_raw_usd": response.get("crossMarginSummary", {}).get("totalRawUsd", "0")
            },
            "withdrawable": response.get("withdrawable", "0")
        }
        
        # Add positions if they exist
        if "assetPositions" in response:
            formatted_response["asset_positions"] = [
                {
                    "position": {
                        "symbol": pos["position"]["coin"],
                        "entry_price": pos["position"].get("entryPx"),
                        "leverage": {
                            "type": pos["position"]["leverage"]["type"],
                            "value": pos["position"]["leverage"]["value"]
                        },
                        "liquidation_price": pos["position"].get("liquidationPx"),
                        "margin_used": pos["position"]["marginUsed"],
                        "max_trade_sizes": pos["position"].get("maxTradeSzs"),
                        "position_value": pos["position"]["positionValue"],
                        "return_on_equity": pos["position"]["returnOnEquity"],
                        "size": pos["position"]["szi"],
                        "unrealized_pnl": pos["position"]["unrealizedPnl"]
                    },
                    "type": pos["type"]
                }
                for pos in response.get("assetPositions", [])
            ]
        
        return from_dict(data_class=UserState, data=formatted_response, config=DACITE_CONFIG)
        
        
    def get_positions(self) -> List[Position]:
        """Get current open positions."""
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        state = self.get_user_state(None)
        return [pos.position for pos in state.asset_positions]
        
    def _validate_and_format_order(
        self, 
        symbol: str, 
        size: float, 
        limit_price: Optional[float]
    ) -> tuple[float, float]:
        """Validate and format order size and price.
        
        Follows Hyperliquid's official rounding rules:
        - Prices must be rounded to 5 significant figures
        - Size must be rounded based on szDecimals
        - For prices > 100k, round to integer
        """
        if symbol not in self.market_specs:
            # Use default values for unknown markets
            size_decimals = 3
        else:
            specs = self.market_specs[symbol]
            size_decimals = specs["size_decimals"]

        # Round size based on szDecimals
        size = round(float(size), size_decimals)

        if limit_price is not None:
            # For prices over 100k, round to integer
            if limit_price > 100_000:
                limit_price = round(float(limit_price))
            else:
                # Round to 5 significant figures using string formatting
                limit_price = float(f"{limit_price:.5g}")
            
        return size, limit_price

    def create_order(
        self,
        symbol: str,
        size: float,
        is_buy: bool,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        time_in_force: Literal["Gtc", "Ioc", "Alo"] = "Gtc",
    ) -> Order:
        """Create an order with simplified parameters.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            is_buy (bool): True for buy orders, False for sell orders
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, uses market price with 0.5% slippage
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only valid for limit orders)
            time_in_force (str): Order time in force - "Gtc" (Good till Cancel), 
                                "Ioc" (Immediate or Cancel), "Alo" (Add Limit Only)
            
        Returns:
            Order: Order response from the exchange
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        # For market orders, get current price and add slippage
        if limit_price is None:
            current_price = self.get_price(symbol)
            slippage = 0.005  # 0.5% slippage for market orders
            limit_price = current_price * (1 + slippage) if is_buy else current_price * (1 - slippage)

        # Debug logging
        logger.debug(f"Original limit price: {limit_price}")
        
        # Validate and format size and price
        size, limit_price = self._validate_and_format_order(symbol, size, limit_price)
        
        # Debug logging
        logger.debug(f"Formatted limit price: {limit_price}")

        # Construct order type
        order_type = {"limit": {"tif": time_in_force}}
        if post_only:
            if time_in_force == "Ioc":
                raise ValueError("post_only cannot be used with IOC orders")
            order_type["limit"]["postOnly"] = True

        # Debug logging
        logger.debug(f"Order type structure: {order_type}")
        logger.debug(f"Order parameters: symbol={symbol}, size={size}, limit_price={limit_price}, is_buy={is_buy}")

        try:
            response = self.exchange.order(
                name=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type=order_type,
                reduce_only=reduce_only
            )
            
            # Debug logging
            logger.debug(f"Order response: {response}")
            
            # Check for error response
            if isinstance(response, dict):
                # Check if response has status field
                if response.get("status") != "ok":
                    raise ValueError(f"Order failed with status: {response.get('status')}")
                
                # Check for response.data structure
                if "response" in response and "data" in response["response"]:
                    statuses = response["response"]["data"].get("statuses", [])
                    if statuses and "error" in statuses[0]:
                        raise ValueError(f"Order error: {statuses[0]['error']}")
                    
                    # Extract order details from the response
                    if statuses and "resting" in statuses[0]:
                        order_data = {
                            "order_id": str(statuses[0]["resting"]["oid"]),
                            "symbol": symbol,
                            "is_buy": is_buy,
                            "size": str(size),
                            "order_type": order_type,
                            "reduce_only": reduce_only,
                            "status": "open",
                            "time_in_force": time_in_force,
                            "created_at": int(time.time() * 1000),
                            "limit_price": str(limit_price)
                        }
                        return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
                    elif statuses and "filled" in statuses[0]:
                        order_data = {
                            "order_id": str(statuses[0]["filled"]["oid"]),
                            "symbol": symbol,
                            "is_buy": is_buy,
                            "size": str(size),
                            "filled_size": str(size),
                            "average_fill_price": str(statuses[0]["filled"]["avgPx"]),
                            "order_type": order_type,
                            "reduce_only": reduce_only,
                            "status": "filled",
                            "time_in_force": time_in_force,
                            "created_at": int(time.time() * 1000),
                            "limit_price": str(limit_price)
                        }
                        return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
                else:
                    # Try alternative response format
                    logger.debug(f"Trying alternative response format: {response}")
                    raise ValueError(f"Unexpected response structure: {response}")
            
            raise ValueError(f"Unexpected response format: {type(response)} - {response}")
        
        except Exception as e:
            logger.error(f"Order placement failed for {symbol}: {str(e)}")
            logger.error(f"Order details: symbol={symbol}, size={size}, limit_price={limit_price}, order_type={order_type}")
            raise ValueError(f"Failed to place order: {str(e)}")

    def buy(
        self,
        symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> Order:
        """Simple buy order function.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, creates a market order
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only for limit orders)
        
        Returns:
            Order: Order response from the exchange
        """
        time_in_force = "Gtc" if limit_price is not None else "Ioc"
        return self.create_order(
            symbol=symbol,
            size=size,
            is_buy=True,
            limit_price=limit_price,
            reduce_only=reduce_only,
            post_only=post_only,
            time_in_force=time_in_force
        )

    def sell(
        self,
        symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> Order:
        """Simple sell order function.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, creates a market order
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only for limit orders)
        
        Returns:
            Order: Order response from the exchange
        """
        time_in_force = "Gtc" if limit_price is not None else "Ioc"
        return self.create_order(
            symbol=symbol,
            size=size,
            is_buy=False,
            limit_price=limit_price,
            reduce_only=reduce_only,
            post_only=post_only,
            time_in_force=time_in_force
        )

    def stop_loss(
        self,
        symbol: str,
        size: float,
        trigger_price: float, # better to name this trigger_price to later have not only stop market orders but also stop limit orders
        is_buy: bool = False  # Default to sell (for long positions)
    ) -> Order:
        """Place a stop loss order.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            stop_price (float): Stop loss price level
            is_buy (bool): True for shorts' SL, False for longs' SL (default)
        """
        # Get current position to determine direction
        positions = self.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if not position:
            raise ValueError(f"No position found for {symbol}")
        
        # Validate and format size and price using the same logic as limit orders
        size, trigger_price = self._validate_and_format_order(symbol, size, trigger_price)

        order_type = {
            "trigger": {
                "triggerPx": trigger_price,
                "isMarket": True,
                "tpsl": "sl"
            }
        }

        response = self.exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=trigger_price,
            reduce_only=True,
            order_type=order_type
        )
        
        # Error handling and response formatting
        if isinstance(response, dict):
            if response.get("status") != "ok":
                raise ValueError(f"Failed to place stop loss order: {response}")
            
            statuses = response.get("response", {}).get("data", {}).get("statuses", [{}])[0]
            if "error" in statuses:
                raise ValueError(f"Stop loss order error: {statuses['error']}")
            
            # Format response data
            if "resting" in statuses:
                order_data = {
                    "order_id": str(statuses["resting"]["oid"]),
                    "symbol": symbol,
                    "is_buy": is_buy,
                    "size": str(size),
                    "order_type": order_type,
                    "reduce_only": True,
                    "status": "open",
                    "time_in_force": "Gtc",
                    "created_at": int(time.time() * 1000),
                    "trigger_price": str(trigger_price)
                }
                return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
        
        raise ValueError("Unexpected response format")

    def take_profit(
        self,
        symbol: str,
        size: float,
        trigger_price: float,
        is_buy: bool = False  # Default to sell (for long positions)
    ) -> Order:
        """Place a take profit order.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            take_profit_price (float): Take profit price level
            is_buy (bool): True for shorts' TP, False for longs' TP (default)
        """
        positions = self.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if not position:
            raise ValueError(f"No position found for {symbol}")
        
        # Validate and format size and price using the same logic as limit orders
        size, trigger_price = self._validate_and_format_order(symbol, size, trigger_price)

        order_type = {
            "trigger": {
                "triggerPx": trigger_price,
                "isMarket": True,
                "tpsl": "tp"
            }
        }

        response = self.exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=trigger_price,
            reduce_only=True,
            order_type=order_type
        )
        
        # Error handling and response formatting
        if isinstance(response, dict):
            if response.get("status") != "ok":
                raise ValueError(f"Failed to place take profit order: {response}")
            
            statuses = response.get("response", {}).get("data", {}).get("statuses", [{}])[0]
            if "error" in statuses:
                raise ValueError(f"Take profit order error: {statuses['error']}")
            
            # Format response data
            if "resting" in statuses:
                order_data = {
                    "order_id": str(statuses["resting"]["oid"]),
                    "symbol": symbol,
                    "is_buy": is_buy,
                    "size": str(size),
                    "order_type": order_type,
                    "reduce_only": True,
                    "status": "open",
                    "time_in_force": "Gtc",
                    "created_at": int(time.time() * 1000),
                    "trigger_price": str(trigger_price)
                }
                return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
        
        raise ValueError("Unexpected response format")

    def open_long_position(
        self,
        symbol: str,
        size: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> Dict[str, Order]:
        """Open a long position with optional stop loss and take profit orders.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Position size
            stop_loss_price (Optional[float]): Stop loss price level
            take_profit_price (Optional[float]): Take profit price level
            limit_price (Optional[float]): Limit price for entry, None for market order
        
        Returns:
            Dict[str, Order]: Dictionary containing entry order and optional sl/tp orders
        """
        orders = {"entry": self.buy(symbol, size, limit_price)}
        
        current_price = self.get_price(symbol)
        if stop_loss_price:
            if stop_loss_price >= (limit_price or current_price):
                raise ValueError("Stop loss price must be below entry price for longs")
            orders["stop_loss"] = self.stop_loss(symbol, size, stop_loss_price)
        
        if take_profit_price:
            if take_profit_price <= (limit_price or current_price):
                raise ValueError("Take profit price must be above entry price for longs")
            orders["take_profit"] = self.take_profit(symbol, size, take_profit_price)
        
        return orders

    def open_short_position(
        self,
        symbol: str,
        size: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        limit_price: Optional[float] = None,
    ) -> Dict[str, Order]:
        """Open a short position with optional stop loss and take profit orders.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Position size
            stop_loss_price (Optional[float]): Stop loss price level
            take_profit_price (Optional[float]): Take profit price level
            limit_price (Optional[float]): Limit price for entry, None for market order
        
        Returns:
            Dict[str, Order]: Dictionary containing entry order and optional sl/tp orders
        """
        orders = {"entry": self.sell(symbol, size, limit_price)}
        
        current_price = self.get_price(symbol)
        if stop_loss_price:
            if stop_loss_price <= (limit_price or current_price):
                raise ValueError("Stop loss price must be above entry price for shorts")
            orders["stop_loss"] = self.stop_loss(symbol, size, stop_loss_price)
        
        if take_profit_price:
            if take_profit_price >= (limit_price or current_price):
                raise ValueError("Take profit price must be below entry price for shorts")
            orders["take_profit"] = self.take_profit(symbol, size, take_profit_price)
        
        return orders

    def close(
        self,
        symbol: str,
        position: Optional[Position] = None
    ) -> Order:
        """Close an existing position.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            position (Optional[Position]): Position object, if None will fetch current position
            
        Returns:
            Order: Order response for the closing order
            
        Raises:
            ValueError: If no position exists for the symbol
        """
        if position is None:
            positions = self.get_positions()
            position = next((p for p in positions if p.symbol == symbol), None)
        
        if not position:
            raise ValueError(f"No open position found for {symbol}")
        
        size = abs(float(position.size))
        is_buy = float(position.size) < 0  # Buy to close shorts, sell to close longs
        
        return self.create_order(
            symbol=symbol,
            size=size,
            is_buy=is_buy,
            reduce_only=True,
            time_in_force="Ioc"  # Market order
        )

    def _validate_price(self, symbol: str, price: float) -> None:
        """Validate if price is within reasonable bounds."""
        current_price = self.get_price(symbol)
        if price <= 0:
            raise ValueError("Price must be positive")
        if abs(price - current_price) / current_price > 0.5:  # 50% deviation
            raise ValueError(f"Price {price} seems unreasonable compared to current price {current_price}")

    def _validate_size(self, symbol: str, size: float) -> None:
        """Validate if order size is valid."""
        if size <= 0:
            raise ValueError("Size must be positive")
        # Could add more validation based on exchange limits

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        """Cancel all open orders, optionally filtered by symbol.
        
        Args:
            symbol (Optional[str]): If provided, only cancels orders for this symbol
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        
        try:
            # Get open orders
            open_orders = self.info.open_orders(self.account.public_address)
            
            # Filter by symbol if provided
            if symbol:
                open_orders = [order for order in open_orders if order["coin"] == symbol]
            
            # Cancel each order
            for order in open_orders:
                try:
                    self.exchange.cancel(order["coin"], order["oid"])
                except Exception as e:
                    logging.warning(f"Failed to cancel order {order['oid']} for {order['coin']}: {str(e)}")
                
        except Exception as e:
            logging.warning(f"Failed to get open orders: {str(e)}")

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Order]:
        """Get all open orders for the authenticated user.
        
        Args:
            symbol (Optional[str]): If provided, only returns orders for this symbol
            
        Returns:
            List[Order]: List of open orders
            
        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
            
        try:
            # Get open orders from the API using frontend_open_orders for more details
            logger.debug(f"Calling frontend_open_orders for address: {self.account.public_address}")
            open_orders_response = self.info.frontend_open_orders(self.account.public_address)
            logger.debug(f"Raw frontend_open_orders response: {open_orders_response}")
            
            # Filter by symbol if provided
            if symbol:
                logger.debug(f"Filtering orders for symbol: {symbol}")
                open_orders_response = [order for order in open_orders_response if order["coin"] == symbol]
                logger.debug(f"After filtering, found {len(open_orders_response)} orders")
            
            # Convert API response to our Order model
            orders = []
            for order_data in open_orders_response:
                try:
                    # Determine order type (limit, market, trigger)
                    order_type = {}
                    order_type_str = "unknown"
                    
                    # Check if it's a trigger order (stop loss or take profit)
                    if order_data.get("isTrigger", False):
                        tpsl = None
                        # Determine if it's a take profit or stop loss based on orderType
                        if "Take Profit" in order_data.get("orderType", ""):
                            tpsl = "tp"
                            order_type_str = "take_profit"
                        elif "Stop" in order_data.get("orderType", ""):
                            tpsl = "sl"
                            order_type_str = "stop_loss"
                            
                        order_type["trigger"] = {
                            "triggerPx": order_data.get("triggerPx"),
                            "isMarket": True,
                            "tpsl": tpsl
                        }
                    # Otherwise it's a limit order
                    else:
                        order_type["limit"] = {
                            "tif": order_data.get("tif", "Gtc"),
                            "px": order_data.get("limitPx")
                        }
                        order_type_str = "limit"
                    
                    # Map side to is_buy (A = Ask/Sell, B = Bid/Buy)
                    is_buy = order_data.get("side") == "B"
                    
                    # Create order object
                    order_dict = {
                        "order_id": str(order_data.get("oid", "")),
                        "symbol": order_data.get("coin", ""),
                        "is_buy": is_buy,
                        "size": Decimal(str(order_data.get("sz", 0))),
                        "order_type": order_type,
                        "reduce_only": order_data.get("reduceOnly", False),
                        "status": "open",  # All orders returned are open
                        "time_in_force": order_data.get("tif", "Gtc") or "Gtc",  # Use "Gtc" as default if tif is None
                        "created_at": order_data.get("timestamp", 0),
                        "filled_size": Decimal(str(order_data.get("origSz", 0))) - Decimal(str(order_data.get("sz", 0))),
                        "type": order_type_str
                    }
                    
                    # Add limit price if available
                    if "limitPx" in order_data:
                        order_dict["limit_price"] = Decimal(str(order_data["limitPx"]))
                    
                    # Add trigger price if available
                    if "triggerPx" in order_data:
                        order_dict["trigger_price"] = Decimal(str(order_data["triggerPx"]))
                    
                    # Create Order object
                    order = from_dict(data_class=Order, data=order_dict, config=DACITE_CONFIG)
                    orders.append(order)
                except Exception as e:
                    logger.error(f"Error processing order data: {e}, order_data: {order_data}")
                
            return orders
            
        except Exception as e:
            logger.error(f"Failed to get open orders: {str(e)}")
            # Try to get raw orders for debugging
            try:
                raw_orders = self.info.open_orders(self.account.public_address)
                logger.debug(f"Raw open_orders response: {raw_orders}")
            except Exception as e2:
                logger.error(f"Failed to get raw orders: {str(e2)}")
            return []

    def get_price(self, symbol: Optional[str] = None) -> Union[float, Dict[str, float]]:
        """Get current price(s). No authentication required."""
        response = self.info.all_mids()
        
        # Convert all prices to float
        prices = {sym: float(price) for sym, price in response.items()}
        
        if symbol is not None:
            if symbol not in prices:
                raise ValueError(f"Symbol {symbol} not found. Available symbols: {', '.join(sorted(prices.keys()))}")
            return prices[symbol]
        
        return prices

    def get_perp_balance(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """Get perpetual trading balance for an address.
        
        Args:
            address (Optional[str]): The address to get balance for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the total balance. If False, returns detailed information.
            
        Returns:
            Union[Decimal, Dict[str, Any]]: If simple=True (default), returns just the total balance.
                                          If simple=False, returns a dictionary containing:
                                          - balance: Total balance in USD
                                          - positions: List of open positions
                                          - margin_used: Current margin usage
                                          - state: Full user state data
        """
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")
            
        if address is None:
            address = self.public_address
            
        # Get user state which includes all necessary information
        user_state = self.get_user_state(address)
        
        if simple:
            return user_state.margin_summary.account_value
            
        return {
            'balance': user_state.margin_summary.account_value,
            'positions': [ap.position for ap in user_state.asset_positions],
            'margin_used': user_state.margin_summary.total_margin_used,
            'state': user_state
        }

    def get_spot_balance(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, SpotState]:
        """Get spot trading balance for an address.
        
        Args:
            address (Optional[str]): The address to get balance for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the total balance. If False, returns detailed information.
            
        Returns:
            Union[Decimal, SpotState]: If simple=True (default), returns just the total balance in USD.
                                     If simple=False, returns a SpotState object containing:
                                     - total_balance: Total balance in USD
                                     - tokens: Dict of token balances (SpotTokenBalance objects)
                                     - raw_state: Original API response
        """
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")
            
        if address is None:
            address = self.public_address
            
        # Get spot user state
        response = self.info.spot_user_state(address)
        
        # Get current prices for all tokens
        prices = self.get_price()
        
        # Process balances
        total_balance = Decimal('0')
        token_balances: Dict[str, SpotTokenBalance] = {}
        
        for balance in response.get('balances', []):
            try:
                token = balance.get('coin')
                if not token:
                    continue
                    
                # Get token amount
                token_amount = Decimal(str(balance.get('total', '0')))
                if token_amount == 0:
                    continue
                    
                # Get token price
                token_price = Decimal(str(prices.get(token, '0')))
                
                # Calculate USD value
                usd_value = token_amount * token_price
                total_balance += usd_value
                
                # Create SpotTokenBalance object
                token_balances[token] = SpotTokenBalance(
                    token=token,
                    amount=token_amount,
                    usd_value=usd_value,
                    price=token_price,
                    hold=Decimal(str(balance.get('hold', '0'))),
                    entry_ntl=Decimal(str(balance.get('entryNtl', '0')))
                )
                
            except (ValueError, TypeError) as e:
                self.logger.warning(f"Error processing balance for token {token}: {str(e)}")
                continue
                
        if simple:
            return total_balance
            
        return SpotState(
            total_balance=total_balance,
            tokens=token_balances,
            raw_state=response
        )

    def get_evm_balance(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """Get EVM chain balance for an address.
        
        Args:
            address (Optional[str]): The address to get balance for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the total balance. If False, returns detailed information.
            
        Returns:
            Union[Decimal, Dict[str, Any]]: If simple=True (default), returns just the total balance in USD.
                                          If simple=False, returns a dictionary containing:
                                          - balance: Total balance in USD
                                          - state: Full EVM state data
        """
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")
            
        if address is None:
            address = self.public_address
            
        # Get EVM state
        response = self.info.evm_state(address)
        
        # Calculate total balance
        total_balance = Decimal(str(response.get('totalBalance', '0')))
        
        if simple:
            return total_balance
            
        return {
            'balance': total_balance,
            'state': response
        }

    def get_all_balances(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """Get all balances (perp, spot, and EVM) for an address.
        
        Args:
            address (Optional[str]): The address to get balances for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the total balance. If False, returns detailed information.
            
        Returns:
            Union[Decimal, Dict[str, Any]]: If simple=True (default), returns just the total balance in USD.
                                          If simple=False, returns a dictionary containing:
                                          - total_balance: Sum of all balances in USD
                                          - perp: Perpetual trading balance and details
                                          - spot: Spot trading balance and details
                                          - evm: EVM chain balance and details
        """
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")
            
        if address is None:
            address = self.public_address
            
        # Get all balances
        perp = self.get_perp_balance(address, simple=simple)
        spot = self.get_spot_balance(address, simple=simple)
        evm = self.get_evm_balance(address, simple=simple)
        
        # Calculate total balance
        if simple:
            return perp + spot + evm
            
        return {
            'total_balance': perp['balance'] + spot['balance'] + evm['balance'],
            'perp': perp,
            'spot': spot,
            'evm': evm
        }

    def get_market_info(self, symbol: str = None) -> Union[Dict, List[Dict]]:
        """Get market information from the exchange.
        
        Args:
            symbol (Optional[str]): If provided, returns info for specific symbol
                                  If None, returns info for all markets
        
        Returns:
            Union[Dict, List[Dict]]: Market information
        
        Example:
            # Get all market specs
            specs = client.get_market_info()
            models.print_market_specs_diff(specs)
            
            # Get specific market
            btc_info = client.get_market_info("BTC")
        """
        response = self.info.meta()
        markets = response['universe']
        
        if symbol:
            market = next((m for m in markets if m['name'] == symbol), None)
            if not market:
                raise ValueError(f"Symbol {symbol} not found")
            return market
        
        return markets

    def get_funding_rates(self, symbol: Optional[str] = None, threshold: Optional[float] = None) -> Union[float, List[Dict[str, Any]]]:
        """Get funding rates for all tokens or a specific symbol.
        
        Args:
            symbol (Optional[str]): If provided, returns funding rate for specific symbol.
                                  If None, returns funding rates for all tokens sorted by value.
            threshold (Optional[float]): If provided, only returns symbols where the absolute funding rate
                                       is greater than the absolute threshold value.
            
        Returns:
            Union[float, List[Dict[str, Any]]]: 
                - If symbol is provided: float funding rate for the symbol
                - If symbol is None: List of dicts with symbol and funding rate, sorted from highest positive to lowest negative
                
        Example:
            # Get all funding rates sorted
            rates = client.get_funding_rates()
            for rate in rates:
                print(f"{rate['symbol']}: {rate['funding_rate']:.6f}")
                
            # Get specific symbol funding rate
            btc_rate = client.get_funding_rates("BTC")
            print(f"BTC funding rate: {btc_rate:.6f}")
            
            # Get funding rates above 0.01% threshold
            high_rates = client.get_funding_rates(threshold=0.0001)
            for rate in high_rates:
                print(f"{rate['symbol']}: {rate['funding_rate']:.6f}")
        """
        # API endpoint for funding rates
        url = "https://api.hyperliquid.xyz/info"
        
        payload = json.dumps({
            "type": "predictedFundings"
        })
        headers = {
            'Content-Type': 'application/json'
        }
        
        try:
            response = requests.post(url, headers=headers, data=payload)
            response.raise_for_status()
            rates = response.json()
            
            # Get market info to map symbols
            market_info = self.get_market_info()
            market_names = {market['name'] for market in market_info}
            
            # Process funding rates
            funding_data = []
            
            for rate in rates:
                symbol_name = rate[0]
                if symbol_name in market_names:
                    # Extract funding rate from the nested structure
                    funding_rate = None
                    for item in rate[1]:
                        if item[0] == 'HlPerp':
                            funding_rate = item[1]['fundingRate']
                            break
                    
                    if funding_rate is not None:
                        funding_data.append({
                            'symbol': symbol_name,
                            'funding_rate': float(funding_rate)
                        })
            
            # If specific symbol requested, return just that rate
            if symbol is not None:
                for data in funding_data:
                    if data['symbol'] == symbol:
                        return data['funding_rate']
                raise ValueError(f"Symbol {symbol} not found in funding rates")
            
            # Apply threshold filter if provided
            if threshold is not None:
                funding_data = [
                    data for data in funding_data 
                    if abs(data['funding_rate']) >= abs(threshold)
                ]
            
            # Sort by funding rate: highest positive to lowest negative
            funding_data.sort(key=lambda x: x['funding_rate'], reverse=True)
            
            return funding_data
            
        except requests.RequestException as e:
            raise ValueError(f"Failed to fetch funding rates: {str(e)}")
        except (KeyError, IndexError, ValueError) as e:
            raise ValueError(f"Failed to parse funding rates response: {str(e)}")

    def cancel_all(self) -> None:
        """Cancel all open orders across all symbols."""
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        
        try:
            # Get open orders
            open_orders = self.info.open_orders(self.account.public_address)
            
            # Cancel each order
            for order in open_orders:
                try:
                    self.exchange.cancel(order["coin"], order["oid"])
                except Exception as e:
                    logging.warning(f"Failed to cancel order {order['oid']} for {order['coin']}: {str(e)}")
                
        except Exception as e:
            logging.warning(f"Failed to get open orders: {str(e)}")

    def _fetch_market_specs(self) -> Dict[str, Dict]:
        """Fetch current market specifications from the API."""
        response = self.info.meta()
        specs = {}
        
        for market in response['universe']:
            specs[market['name']] = {
                "size_decimals": market.get('szDecimals', 3),
            }
        
        return specs

    def get_stop_loss_price(self, symbol: str) -> Optional[Decimal]:
        """Get the stop loss price for a specific symbol.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            
        Returns:
            Optional[Decimal]: The stop loss price if a stop loss order exists, None otherwise
            
        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
            
        # Get all open orders for the symbol
        open_orders = self.get_open_orders(symbol)
        
        # Filter for stop loss orders
        sl_orders = [order for order in open_orders if order.type == "stop_loss"]
        
        if not sl_orders:
            return None
            
        # Return the price of the first stop loss order
        # If there are multiple stop loss orders, this returns the first one
        return sl_orders[0].trigger_price
    
    def get_take_profit_price(self, symbol: str) -> Optional[Decimal]:
        """Get the take profit price for a given symbol.
        
        Args:
            symbol (str): The trading symbol to check
            
        Returns:
            Optional[Decimal]: The trigger price of the first take profit order found, or None if no take profit order exists
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to get take profit price")
            
        orders = self.get_open_orders(symbol)
        tp_orders = [order for order in orders if order.type == "take_profit"]
        
        if not tp_orders:
            return None
            
        return tp_orders[0].trigger_price
        
    def has_position(self, symbol: str) -> bool:
        """Check if the user has an open position for the given symbol.
        
        Args:
            symbol (str): The trading symbol to check
            
        Returns:
            bool: True if a position exists, False otherwise
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to check positions")
            
        positions = self.get_positions()
        return any(p.symbol == symbol and p.size != 0 for p in positions)
    
    def get_position_size(self, symbol: str) -> Optional[Decimal]:
        """Get the size of a position for a given symbol.
        
        Args:
            symbol: The trading symbol (e.g., "BTC")
            
        Returns:
            The position size as a Decimal, or None if no position exists
        """
        positions = self.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            return None
        return position.size
        
    def get_position_direction(self, symbol: str) -> Optional[str]:
        """Get the direction of a position for a given symbol.
        
        Args:
            symbol: The trading symbol (e.g., "BTC")
            
        Returns:
            "long" if position size is positive, "short" if negative, None if no position exists
        """
        position_size = self.get_position_size(symbol)
        if position_size is None:
            return None
        return "long" if float(position_size) > 0 else "short"
        
    def has_active_orders(self, symbol: Optional[str] = None) -> bool:
        """Check if there are any active orders for the given symbol.
        
        Args:
            symbol (Optional[str]): The trading symbol to check
            
        Returns:
            bool: True if there are active orders, False otherwise
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to check active orders")
            
        orders = self.get_open_orders(symbol)
        return len(orders) > 0
        
    def calculate_position_size(self, symbol: str, risk_amount: Decimal, stop_loss_price: Decimal) -> Decimal:
        """Calculate the optimal position size based on risk management.
        
        Args:
            symbol (str): The trading symbol
            risk_amount (Decimal): The amount to risk in USD
            stop_loss_price (Decimal): The stop loss price level
            
        Returns:
            Decimal: The calculated position size
            
        Raises:
            ValueError: If the stop loss price is invalid
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to calculate position size")
            
        # Get current price
        current_price = Decimal(str(self.get_price(symbol)))
        
        # Calculate risk per unit
        if current_price > stop_loss_price:  # Long position
            risk_per_unit = current_price - stop_loss_price
        elif current_price < stop_loss_price:  # Short position
            risk_per_unit = stop_loss_price - current_price
        else:
            raise ValueError("Stop loss price cannot be equal to current price")
            
        # Calculate position size
        position_size = risk_amount / risk_per_unit
        
        # Get market specs for size formatting
        if symbol in self.market_specs:
            size_decimals = self.market_specs[symbol]["size_decimals"]
            
            # Round to the appropriate number of decimals
            position_size = position_size.quantize(Decimal('0.' + '0' * size_decimals))
            
        return position_size
        
    def modify_order(
        self,
        order_id: str,
        symbol: str,
        is_buy: bool,
        size: float,
        price: float,
        order_type: Dict,
        reduce_only: bool = False
    ) -> Order:
        """Modify an existing order.
        
        Args:
            order_id (str): The ID of the order to modify
            symbol (str): Trading pair symbol (e.g., "BTC")
            is_buy (bool): True for buy orders, False for sell orders
            size (float): New order size
            price (float): New price for the order
            order_type (Dict): Order type specification
            reduce_only (bool): Whether the order should only reduce position
            
        Returns:
            Order: Updated order response
            
        Raises:
            RuntimeError: If client is not authenticated
            ValueError: If order modification fails
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
            
        # Validate and format size and price
        size, price = self._validate_and_format_order(symbol, size, price)
        
        try:
            # Convert order_id to integer as required by the SDK
            oid = int(order_id)
            response = self.exchange.modify_order(
                oid=oid,
                name=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=price,
                order_type=order_type,
                reduce_only=reduce_only
            )
            
            # Check for error response
            if isinstance(response, dict):
                if response.get("status") != "ok":
                    raise ValueError(f"Failed to modify order: {response}")
                
                statuses = response.get("response", {}).get("data", {}).get("statuses", [{}])[0]
                if "error" in statuses:
                    raise ValueError(f"Order modification error: {statuses['error']}")
                
                # Format response data
                if "resting" in statuses:
                    # For limit orders
                    order_data = {
                        "order_id": str(statuses["resting"]["oid"]),
                        "symbol": symbol,
                        "is_buy": is_buy,
                        "size": str(size),
                        "order_type": order_type,
                        "reduce_only": reduce_only,
                        "status": "open",
                        "time_in_force": order_type.get("limit", {}).get("tif", "Gtc") if "limit" in order_type else "Gtc",
                        "created_at": int(time.time() * 1000),
                        "limit_price": str(price)
                    }
                    
                    # Add type field based on order_type
                    if "trigger" in order_type:
                        if order_type["trigger"].get("tpsl") == "tp":
                            order_data["type"] = "take_profit"
                            order_data["trigger_price"] = str(price)
                        elif order_type["trigger"].get("tpsl") == "sl":
                            order_data["type"] = "stop_loss"
                            order_data["trigger_price"] = str(price)
                    else:
                        order_data["type"] = "limit"
                    
                    return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
            
            raise ValueError("Unexpected response format")
            
        except Exception as e:
            raise ValueError(f"Failed to modify order: {str(e)}")

    def update_stop_loss(self, symbol: str, new_price: float) -> Optional[Order]:
        """Update the stop loss price for an existing position.
        
        Args:
            symbol (str): The trading symbol
            new_price (float): The new stop loss price
            
        Returns:
            Optional[Order]: The updated stop loss order, or None if no position exists or no stop loss order found
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to update stop loss")
        
            
        # Get position size
        position_size = abs(float(self.get_position_size(symbol)))
        
        # Find existing stop loss orders
        open_orders = self.get_open_orders(symbol)
        sl_orders = [order for order in open_orders if order.type == "stop_loss"]
        
        if not sl_orders:
            # No existing stop loss order, create a new one
            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.stop_loss(symbol, position_size, new_price, is_buy=is_buy)
        
        # Use the first stop loss order
        sl_order = sl_orders[0]
        
        # Create order type for stop loss
        order_type = {
            "trigger": {
                "triggerPx": new_price,
                "isMarket": True,
                "tpsl": "sl"
            }
        }
        
        # Modify the existing stop loss order
        try:
            return self.modify_order(
                order_id=sl_order.order_id,
                symbol=symbol,
                is_buy=sl_order.is_buy,
                size=float(sl_order.size),
                price=new_price,
                order_type=order_type,
                reduce_only=True
            )
        except Exception as e:
            logging.warning(f"Failed to modify stop loss order, falling back to cancel and create: {str(e)}")
            
            # Fallback to cancel and create
            for order in sl_orders:
                try:
                    self.exchange.cancel(symbol, order.order_id)
                    time.sleep(0.5)  # Small delay to ensure order is cancelled
                except Exception as e:
                    logging.warning(f"Failed to cancel stop loss order {order.order_id}: {str(e)}")
            
            # Create new stop loss order
            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.stop_loss(symbol, position_size, new_price, is_buy=is_buy)
        
    def update_take_profit(self, symbol: str, new_price: float) -> Optional[Order]:
        """Update the take profit price for an existing position.
        
        Args:
            symbol (str): The trading symbol
            new_price (float): The new take profit price
            
        Returns:
            Optional[Order]: The updated take profit order, or None if no position exists or no take profit order found
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to update take profit")
            
            
        # Get position size
        position_size = abs(float(self.get_position_size(symbol)))
        
        # Find existing take profit orders
        open_orders = self.get_open_orders(symbol)
        tp_orders = [order for order in open_orders if order.type == "take_profit"]
        
        if not tp_orders:
            # No existing take profit order, create a new one
            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.take_profit(symbol, position_size, new_price, is_buy=is_buy)
        
        # Use the first take profit order
        tp_order = tp_orders[0]
        
        # Create order type for take profit
        order_type = {
            "trigger": {
                "triggerPx": new_price,
                "isMarket": True,
                "tpsl": "tp"
            }
        }
        
        # Modify the existing take profit order
        try:
            return self.modify_order(
                order_id=tp_order.order_id,
                symbol=symbol,
                is_buy=tp_order.is_buy,
                size=float(tp_order.size),
                price=new_price,
                order_type=order_type,
                reduce_only=True
            )
        except Exception as e:
            logging.warning(f"Failed to modify take profit order, falling back to cancel and create: {str(e)}")
            
            # Fallback to cancel and create
            for order in tp_orders:
                try:
                    self.exchange.cancel(symbol, order.order_id)
                    time.sleep(0.5)  # Small delay to ensure order is cancelled
                except Exception as e:
                    logging.warning(f"Failed to cancel take profit order {order.order_id}: {str(e)}")
            
            # Create new take profit order
            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.take_profit(symbol, position_size, new_price, is_buy=is_buy)
        
    def trailing_stop(self, symbol: str, trail_percent: float) -> Optional[Order]:
        """Create a trailing stop based on the current price and trail percentage.
        
        This updates an existing stop loss order or creates a new one based on the current price.
        
        Args:
            symbol (str): The trading symbol
            trail_percent (float): The trailing percentage (e.g., 2.0 for 2%)
            
        Returns:
            Optional[Order]: The updated stop loss order, or None if no position exists
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to set trailing stop")
            
        # Get current price
        current_price = self.get_price(symbol)
        
        # Get position direction
        position_direction = self.get_position_direction(symbol)
        if position_direction is None:
            return None
        
        # Calculate new stop loss price based on trail percentage
        if position_direction == "long":
            # For long positions, stop loss is below current price
            new_stop_price = current_price * (1 - trail_percent / 100)
        else:
            # For short positions, stop loss is above current price
            new_stop_price = current_price * (1 + trail_percent / 100)
            
        # Update stop loss using the improved method
        return self.update_stop_loss(symbol, new_stop_price)

    def get_order_by_id(self, symbol: str, order_id: str) -> Optional[Order]:
        """Retrieve an order by its ID.
        
        Args:
            symbol (str): The trading symbol
            order_id (str): The order ID to retrieve
            
        Returns:
            Optional[Order]: The order if found, None otherwise
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client is not authenticated")
            
        orders = self.get_open_orders(symbol)
        for order in orders:
            if order.order_id == order_id:
                return order
        return None

    def close_all_positions(self) -> Dict[str, Order]:
        """Close all open positions.
        
        Returns:
            Dict[str, Order]: A dictionary mapping symbols to close orders
            
        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client is not authenticated")
            
        positions = self.get_positions()
        results = {}
        
        for position in positions:
            try:
                close_order = self.close(position.symbol, position)
                results[position.symbol] = close_order
            except Exception as e:
                logging.error(f"Failed to close position for {position.symbol}: {str(e)}")
                
        return results

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel a specific order by order ID and symbol.
        
        Args:
            order_id (str): The order ID to cancel
            symbol (str): The symbol the order is for
            
        Returns:
            bool: True if order was successfully cancelled, False otherwise
            
        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
            
        try:
            # Convert order_id string to integer as required by the SDK
            oid = int(order_id)
            self.exchange.cancel(symbol, oid)
            logger.info(f"Successfully cancelled order {order_id} for {symbol}")
            return True
        except ValueError as e:
            logger.error(f"Invalid order ID format {order_id}: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id} for {symbol}: {str(e)}")
            return False


