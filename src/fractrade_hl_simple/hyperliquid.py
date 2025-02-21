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
    print_market_specs_diff
)
from functools import partialmethod
import time
import os
from dacite import from_dict, Config as DaciteConfig
import eth_account
import logging
from decimal import Decimal


class HyperliquidClient:
    def __init__(self, account: Optional[HyperliquidAccount] = None):
        """Initialize HyperliquidClient.
        
        If account is provided, uses those credentials.
        If not, tries to load from environment variables.
        Falls back to unauthenticated mode if no credentials are available.
        """
        # Default to mainnet for unauthenticated client
        self.env = "mainnet"
        self.base_url = constants.MAINNET_API_URL
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
            logging.warning(
                f"Running in unauthenticated mode. Only public endpoints available. Error: {str(e)}"
            )

    def _setup_authenticated_client(self, account: HyperliquidAccount):
        """Set up authenticated client with account details."""
        # Validate account
        if not isinstance(account, HyperliquidAccount):
            raise TypeError("account must be an instance of HyperliquidAccount")
        
        if account.env not in ["mainnet", "testnet"]:
            raise ValueError("env must be either 'mainnet' or 'testnet'")

        if not account.public_address:
            raise ValueError("public_address is required")
        
        if not account.private_key:
            raise ValueError("private_key is required")

        # Set up authenticated client
        self.account = account
        self.exchange_account = eth_account.Account.from_key(account.private_key)
        self.public_address = account.public_address
        
        # Update URL if needed
        if account.env == "testnet":
            self.env = "testnet"
            self.base_url = constants.TESTNET_API_URL
            self.info = Info(self.base_url, skip_ws=True)
        
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
            min_size = 0.001
        else:
            specs = self.market_specs[symbol]
            size_decimals = specs["size_decimals"]
            min_size = specs["min_size"]
            
            if size < min_size:
                raise ValueError(f"Size must be at least {min_size} for {symbol}")

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
        print(f"Original limit price: {limit_price}")
        
        # Validate and format size and price
        size, limit_price = self._validate_and_format_order(symbol, size, limit_price)
        
        # Debug logging
        print(f"Formatted limit price: {limit_price}")

        # Construct order type
        order_type = {"limit": {"tif": time_in_force}}
        if post_only:
            if time_in_force == "Ioc":
                raise ValueError("post_only cannot be used with IOC orders")
            order_type["limit"]["postOnly"] = True

        try:
            response = self.exchange.order(
                name=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type=order_type,
                reduce_only=reduce_only
            )
            
            # Check for error response
            if isinstance(response, dict):
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
            
            raise ValueError("Unexpected response format")
        
        except Exception as e:
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
        stop_price: float,
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
        size, stop_price = self._validate_and_format_order(symbol, size, stop_price)

        order_type = {
            "trigger": {
                "triggerPx": stop_price,
                "isMarket": True,
                "tpsl": "sl"
            }
        }

        response = self.exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=stop_price,
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
                    "limit_price": str(stop_price)
                }
                return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
        
        raise ValueError("Unexpected response format")

    def take_profit(
        self,
        symbol: str,
        size: float,
        take_profit_price: float,
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
        size, take_profit_price = self._validate_and_format_order(symbol, size, take_profit_price)

        order_type = {
            "trigger": {
                "triggerPx": take_profit_price,
                "isMarket": True,
                "tpsl": "tp"
            }
        }

        response = self.exchange.order(
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=take_profit_price,
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
                    "limit_price": str(take_profit_price)
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

    def get_perp_balance(self, address: Optional[str] = None) -> Decimal:
        """Get perpetual balance for an address."""
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")
            
        if address is None:
            address = self.public_address
            
        state = self.get_user_state(address)
        return state.margin_summary.account_value

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
                "min_size": float(market.get('minSz', '0.001'))
            }
        
        return specs


