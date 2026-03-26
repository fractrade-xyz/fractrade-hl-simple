import json
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional, Union

import eth_account
import requests
from dacite import from_dict
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.error import ClientError

from .exceptions import (
    PositionNotFoundException,
    RateLimitException,
    ServerErrorException,
    UnauthorizedException,
    ConfigurationException,
)
from .models import (
    DACITE_CONFIG,
    MARKET_SPECS,
    HyperliquidAccount,
    Order,
    OrderType,
    Position,
    SpotState,
    SpotTokenBalance,
    UserState,
)

# Set up logger
logger = logging.getLogger("fractrade_hl_simple")
logger.addHandler(logging.NullHandler())

class HyperliquidClient:
    _cached_market_specs: Optional[Dict[str, Dict]] = None
    _cached_market_specs_at: float = 0
    _cached_meta: Optional[Any] = None
    _cached_spot_meta: Optional[Any] = None
    _CACHE_TTL: float = 86400  # 24 hours

    def __init__(
        self,
        account: Optional[HyperliquidAccount] = None,
        env: str = "mainnet",
        default_slippage: float = 0.05,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        cache_market_specs: bool = True,
        extended_universe: bool = False,
    ):
        """Initialize HyperliquidClient.

        Args:
            account (Optional[HyperliquidAccount]): Account credentials. If None, tries to load from environment.
            env (str): The environment to use, either "mainnet" or "testnet". Defaults to "mainnet".
            default_slippage (float): Default slippage for market orders (0.05 = 5%). Defaults to 0.05.
            max_retries (int): Maximum number of retries for transient failures. 0 to disable. Defaults to 3.
            retry_delay (float): Base delay in seconds between retries (exponential backoff). Defaults to 1.0.
            cache_market_specs (bool): If True (default), reuses cached market specs across instances
                within the same process. Cache expires after 24 hours. Set to False to always fetch
                fresh specs on init.
            extended_universe (bool): If True, enables trading of stocks, commodities, indices, and forex
                (xyz: symbols like xyz:TSLA, xyz:GOLD). Off by default to avoid extra API overhead.

        Raises:
            ValueError: If env is not 'mainnet' or 'testnet'
        """
        # Validate parameters
        if env not in ["mainnet", "testnet"]:
            raise ValueError("env must be either 'mainnet' or 'testnet'")
        if not 0 < default_slippage <= 0.5:
            raise ValueError("default_slippage must be between 0 (exclusive) and 0.5 (inclusive)")

        # Set up environment
        self.env = env
        self.default_slippage = default_slippage
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.base_url = constants.TESTNET_API_URL if env == "testnet" else constants.MAINNET_API_URL
        self.perp_dexs = ["", "xyz"] if extended_universe else [""]

        # Initialize market specs
        cache_hit = (
            cache_market_specs
            and HyperliquidClient._cached_market_specs is not None
            and time.time() - HyperliquidClient._cached_market_specs_at < self._CACHE_TTL
        )
        if cache_hit and HyperliquidClient._cached_meta is not None:
            self.info = Info(self.base_url, skip_ws=True, perp_dexs=self.perp_dexs,
                            meta=HyperliquidClient._cached_meta,
                            spot_meta=HyperliquidClient._cached_spot_meta)
        else:
            self.info = Info(self.base_url, skip_ws=True, perp_dexs=self.perp_dexs)
            HyperliquidClient._cached_meta = self.info.meta()
            HyperliquidClient._cached_spot_meta = self.info.spot_meta()
        if cache_hit:
            self.market_specs = HyperliquidClient._cached_market_specs
            self._market_specs_fetched_at = HyperliquidClient._cached_market_specs_at
        else:
            try:
                self.market_specs = self._fetch_market_specs()
                self._market_specs_fetched_at = time.time()
                HyperliquidClient._cached_market_specs = self.market_specs
                HyperliquidClient._cached_market_specs_at = self._market_specs_fetched_at
            except Exception as e:
                logging.warning(f"Failed to fetch market specs: {e}. Using default specs.")
                self.market_specs = MARKET_SPECS
                self._market_specs_fetched_at = 0

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
        # When using an API wallet, the signing key address differs from the
        # trading account address. Pass account_address so the SDK sends
        # orders on behalf of the correct account.
        # For sub-accounts (vaults), the SDK needs vault_address instead.
        account_address = None
        vault_address = None
        if self.exchange_account.address.lower() != account.public_address.lower():
            if account.is_vault:
                vault_address = account.public_address
            else:
                account_address = account.public_address
        self.exchange = Exchange(
            self.exchange_account, self.base_url,
            account_address=account_address,
            vault_address=vault_address,
            perp_dexs=self.perp_dexs,
        )

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

    def _with_retry(self, fn, *args, **kwargs):
        """Execute a function with exponential backoff on transient failures.

        Args:
            fn: The function to call.
            *args: Positional arguments to pass to fn.
            **kwargs: Keyword arguments to pass to fn.

        Returns:
            The return value of fn.

        Raises:
            The last exception if all retries are exhausted, or immediately
            for non-retryable errors.
        """
        last_exception = None
        attempts = self.max_retries + 1  # first attempt + retries

        for attempt in range(attempts):
            try:
                return fn(*args, **kwargs)
            except ClientError as e:
                if e.status_code == 429:
                    last_exception = e
                    if attempt < self.max_retries:
                        wait = self.retry_delay * (2 ** attempt)
                        logger.warning(f"Rate limited, retry {attempt + 1}/{self.max_retries} after {wait:.1f}s")
                        time.sleep(wait)
                    else:
                        raise RateLimitException(f"Rate limited after {self.max_retries} retries")
                else:
                    raise
            except (requests.ConnectionError, requests.Timeout,
                    RateLimitException, ServerErrorException) as e:
                last_exception = e
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** attempt)
                    logger.warning(f"Retry {attempt + 1}/{self.max_retries} after {wait:.1f}s: {e}")
                    time.sleep(wait)
                else:
                    raise
            except (ValueError, TypeError, UnauthorizedException,
                    ConfigurationException, RuntimeError):
                raise

        raise last_exception  # pragma: no cover – safety fallback

    def get_user_state(self, address: Optional[str] = None, dex: str = "") -> UserState:
        """Get the state of any user by their address."""
        if address is None and not self.is_authenticated():
            raise ValueError("Address required when client is not authenticated")

        if address is None:
            address = self.public_address

        # Add address validation
        if not address.startswith("0x") or len(address) != 42:
            raise ValueError("Invalid address format")

        response = self._with_retry(self.info.user_state, address, dex=dex)

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
            "withdrawable": response.get("withdrawable", "0"),
            "cross_maintenance_margin_used": response.get("crossMaintenanceMarginUsed"),
        }

        # Add positions if they exist
        if "assetPositions" in response:
            formatted_response["asset_positions"] = []
            for pos in response.get("assetPositions", []):
                pos_data = pos["position"]
                position_dict = {
                    "symbol": pos_data["coin"],
                    "entry_price": pos_data.get("entryPx"),
                    "leverage": {
                        "type": pos_data["leverage"]["type"],
                        "value": pos_data["leverage"]["value"]
                    },
                    "liquidation_price": pos_data.get("liquidationPx"),
                    "margin_used": pos_data["marginUsed"],
                    "max_trade_sizes": pos_data.get("maxTradeSzs"),
                    "position_value": pos_data["positionValue"],
                    "return_on_equity": pos_data["returnOnEquity"],
                    "size": pos_data["szi"],
                    "unrealized_pnl": pos_data["unrealizedPnl"],
                    "max_leverage": pos_data.get("maxLeverage"),
                }
                # Parse cumulative funding if present
                cf = pos_data.get("cumFunding")
                if cf and isinstance(cf, dict):
                    position_dict["cum_funding"] = {
                        "all_time": cf.get("allTime", "0"),
                        "since_open": cf.get("sinceOpen", "0"),
                        "since_change": cf.get("sinceChange", "0"),
                    }
                formatted_response["asset_positions"].append({
                    "position": position_dict,
                    "type": pos["type"]
                })
        
        return from_dict(data_class=UserState, data=formatted_response, config=DACITE_CONFIG)
        
        
    def get_positions(self) -> List[Position]:
        """Get current open positions across all perp dexes."""
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        positions = []
        for dex in self.perp_dexs:
            state = self.get_user_state(None, dex=dex)
            positions.extend(pos.position for pos in state.asset_positions)
        return positions
        
    def _format_price(self, symbol: str, price: float) -> float:
        """Format a price for the exchange, matching the SDK's rounding logic.

        Uses 5 significant figures, then rounds to (6 - sz_decimals) decimal places
        for perps. This naturally handles all price ranges: BTC gets ~1 decimal,
        micro-caps get ~6 decimals.

        Args:
            symbol: Trading pair symbol.
            price: Raw price to format.

        Returns:
            Formatted price as float.
        """
        sz_decimals = self.market_specs.get(symbol, {}).get("size_decimals", 0)
        # Match SDK: round(float(f"{px:.5g}"), 6 - sz_decimals)
        return round(float(f"{price:.5g}"), max(0, 6 - sz_decimals))

    def _format_size(self, symbol: str, size: float) -> float:
        """Format a size for the exchange, rounding to sz_decimals.

        Args:
            symbol: Trading pair symbol.
            size: Raw size to format.

        Returns:
            Formatted size as float.
        """
        sz_decimals = self.market_specs.get(symbol, {}).get("size_decimals", 0)
        return round(size, sz_decimals)

    def _validate_and_format_order(
        self,
        symbol: str,
        size: float,
        limit_price: Optional[float]
    ) -> tuple[float, float]:
        """Validate and format order size and price using Hyperliquid SDK logic.

        - Sizes are rounded to sz_decimals (from market specs)
        - Prices are rounded to 5 significant figures, then to (6 - sz_decimals) decimal places
        - Validates minimum position sizes based on market specs
        """
        # Auto-refresh stale market specs
        self._ensure_fresh_market_specs()

        # Validate minimum position size
        if symbol in self.market_specs:
            specs = self.market_specs[symbol]
            size_decimals = specs.get("size_decimals", 0)

            min_size = 1.0 / (10 ** size_decimals) if size_decimals > 0 else 1
            if size < min_size:
                raise ValueError(
                    f"Minimum position size for {symbol} is {min_size} "
                    f"(size_decimals={size_decimals}). Got: {size}"
                )

        # Format size to sz_decimals
        size = self._format_size(symbol, size)

        # Format price using SDK-matching logic
        if limit_price is not None:
            limit_price = self._format_price(symbol, limit_price)

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
        slippage: Optional[float] = None,
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

        # For market orders, get current price and add slippage (matching SDK logic)
        if limit_price is None:
            # Get midprice like SDK does
            current_price = self.get_price(symbol)
            logger.debug(f"Current price for {symbol}: {current_price}")
            
            # Check if price is valid
            if current_price <= 0:
                raise ValueError(f"Invalid current price for {symbol}: {current_price}")
                
            effective_slippage = slippage if slippage is not None else self.default_slippage
            if not 0 < effective_slippage <= 0.5:
                raise ValueError("slippage must be between 0 (exclusive) and 0.5 (inclusive)")
            limit_price = current_price * (1 + effective_slippage) if is_buy else current_price * (1 - effective_slippage)

        # Validate and format size and price (handles all rounding)
        size, limit_price = self._validate_and_format_order(symbol, size, limit_price)
        logger.debug(f"Formatted order: symbol={symbol}, size={size}, limit_price={limit_price}")

        # Construct order type (matching SDK)
        order_type = {"limit": {"tif": time_in_force}}
        if post_only:
            if time_in_force == "Ioc":
                raise ValueError("post_only cannot be used with IOC orders")
            order_type["limit"]["postOnly"] = True

        # Debug logging
        logger.debug(f"Order type structure: {order_type}")
        logger.debug(f"Order parameters: symbol={symbol}, size={size}, limit_price={limit_price}, is_buy={is_buy}")

        try:
            response = self._with_retry(
                self.exchange.order,
                name=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type=order_type,
                reduce_only=reduce_only,
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
                        filled_sz = statuses[0]["filled"].get("totalSz", size)
                        order_data = {
                            "order_id": str(statuses[0]["filled"]["oid"]),
                            "symbol": symbol,
                            "is_buy": is_buy,
                            "size": str(size),
                            "filled_size": str(filled_sz),
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
        
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Order placement failed for {symbol}: {str(e)}")
            logger.error(f"Order details: symbol={symbol}, size={size}, limit_price={limit_price}, order_type={order_type}")
            raise

    def buy(
        self,
        symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        slippage: Optional[float] = None,
    ) -> Order:
        """Simple buy order function.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, creates a market order
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only for limit orders)
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage if set.

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
            time_in_force=time_in_force,
            slippage=slippage,
        )

    def sell(
        self,
        symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        slippage: Optional[float] = None,
    ) -> Order:
        """Simple sell order function.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, creates a market order
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only for limit orders)
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage if set.

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
            time_in_force=time_in_force,
            slippage=slippage,
        )

    def stop_loss(
        self,
        symbol: str,
        size: float,
        trigger_price: float,
        is_buy: bool = False
    ) -> Order:
        """Place a stop loss order.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            trigger_price (float): Price at which the stop loss triggers
            is_buy (bool): True for shorts' SL, False for longs' SL (default)
        """
        # Get current position to determine direction
        positions = self.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if not position:
            raise PositionNotFoundException(f"No position found for {symbol}")

        # Validate and format size and trigger price (handles all rounding)
        size, trigger_price = self._validate_and_format_order(symbol, size, trigger_price)

        order_type = {
            "trigger": {
                "triggerPx": trigger_price,
                "isMarket": True,
                "tpsl": "sl"
            }
        }

        response = self._with_retry(
            self.exchange.order,
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=trigger_price,
            reduce_only=True,
            order_type=order_type,
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
        is_buy: bool = False
    ) -> Order:
        """Place a take profit order.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            trigger_price (float): Price at which the take profit triggers
            is_buy (bool): True for shorts' TP, False for longs' TP (default)
        """
        positions = self.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if not position:
            raise PositionNotFoundException(f"No position found for {symbol}")

        # Validate and format size and trigger price (handles all rounding)
        size, trigger_price = self._validate_and_format_order(symbol, size, trigger_price)

        order_type = {
            "trigger": {
                "triggerPx": trigger_price,
                "isMarket": True,
                "tpsl": "tp"
            }
        }

        response = self._with_retry(
            self.exchange.order,
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=trigger_price,
            reduce_only=True,
            order_type=order_type,
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
        slippage: Optional[float] = None,
    ) -> Dict[str, Order]:
        """Open a long position with optional stop loss and take profit orders.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Position size
            stop_loss_price (Optional[float]): Stop loss price level
            take_profit_price (Optional[float]): Take profit price level
            limit_price (Optional[float]): Limit price for entry, None for market order
            slippage (Optional[float]): Slippage for market entry. Overrides default_slippage if set.

        Returns:
            Dict[str, Order]: Dictionary containing entry order and optional sl/tp orders
        """
        orders = {"entry": self.buy(symbol, size, limit_price, slippage=slippage)}

        reference_price = limit_price or float(orders["entry"].limit_price or 0) or self.get_price(symbol)
        if stop_loss_price:
            if stop_loss_price >= reference_price:
                raise ValueError("Stop loss price must be below entry price for longs")
            orders["stop_loss"] = self.stop_loss(symbol, size, stop_loss_price)

        if take_profit_price:
            if take_profit_price <= reference_price:
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
        slippage: Optional[float] = None,
    ) -> Dict[str, Order]:
        """Open a short position with optional stop loss and take profit orders.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Position size
            stop_loss_price (Optional[float]): Stop loss price level
            take_profit_price (Optional[float]): Take profit price level
            limit_price (Optional[float]): Limit price for entry, None for market order
            slippage (Optional[float]): Slippage for market entry. Overrides default_slippage if set.

        Returns:
            Dict[str, Order]: Dictionary containing entry order and optional sl/tp orders
        """
        orders = {"entry": self.sell(symbol, size, limit_price, slippage=slippage)}

        reference_price = limit_price or float(orders["entry"].limit_price or 0) or self.get_price(symbol)
        if stop_loss_price:
            if stop_loss_price <= reference_price:
                raise ValueError("Stop loss price must be above entry price for shorts")
            orders["stop_loss"] = self.stop_loss(symbol, size, stop_loss_price)

        if take_profit_price:
            if take_profit_price >= reference_price:
                raise ValueError("Take profit price must be below entry price for shorts")
            orders["take_profit"] = self.take_profit(symbol, size, take_profit_price)

        return orders

    def _check_order_filled(self, order_id: int) -> bool:
        """Check if an order has been filled via the order status API."""
        try:
            status = self.get_order_status(order_id)
            order_info = status.get("order", {})
            return order_info.get("status") == "filled"
        except Exception as e:
            logger.debug(f"Error checking order status {order_id}: {e}")
            return False

    def _make_filled_order(self, order_id: int, symbol: str, is_buy: bool, size: float,
                           order: Order, avg_fill_price: Optional[Decimal] = None) -> Order:
        """Create a filled Order object from a completed maker chase."""
        logger.info(f"Maker order filled: {symbol} {'buy' if is_buy else 'sell'} {size}"
                    f"{f' @ {avg_fill_price}' if avg_fill_price else ''}")
        return Order(
            order_id=str(order_id),
            symbol=symbol,
            is_buy=is_buy,
            size=Decimal(str(size)),
            filled_size=Decimal(str(size)),
            order_type=order.order_type,
            reduce_only=order.reduce_only,
            status="filled",
            created_at=order.created_at,
            limit_price=order.limit_price,
            average_fill_price=avg_fill_price,
        )

    def _auto_reprice_interval(self, symbol: str) -> tuple:
        """Auto-detect timeout and reprice_interval based on order book spread.

        Returns:
            (timeout, reprice_interval) tuple
        """
        try:
            book = self.get_order_book(symbol)
            mid = book.get("mid_price")
            spread = book.get("spread")
            if mid and spread and mid > 0:
                spread_bps = (spread / mid) * 10000
                if spread_bps < 2:       # Tight spread: BTC, ETH, SOL
                    return (30.0, 3.0)
                elif spread_bps < 20:     # Medium: kPEPE, DOGE, LINK
                    return (45.0, 5.0)
                else:                     # Wide spread: DYM, low-cap
                    return (60.0, 8.0)
        except Exception as e:
            logger.debug(f"Auto-detect spread failed for {symbol}: {e}")
        return (60.0, 5.0)  # Safe defaults

    def maker_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        timeout: Optional[float] = None,
        reprice_interval: Optional[float] = None,
        fallback: str = "ioc",
        reduce_only: bool = False,
    ) -> Order:
        """Place a maker (post_only) limit order and chase the best price until filled.

        Places a post_only order at the best bid (buy) or best ask (sell), then
        re-prices every `reprice_interval` seconds to follow the market. If the order
        is not fully filled within `timeout` seconds, falls back to the specified
        strategy.

        When timeout and reprice_interval are None (default), they are auto-detected
        from the order book spread:
        - Tight spread (<2 bps, e.g. BTC/ETH): timeout=30s, reprice=3s
        - Medium spread (<20 bps, e.g. kPEPE): timeout=45s, reprice=5s
        - Wide spread (>20 bps, e.g. DYM): timeout=60s, reprice=8s

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            is_buy (bool): True for buy, False for sell
            size (float): Order size in base currency
            timeout (Optional[float]): Max seconds to chase before fallback. None for auto-detect.
            reprice_interval (Optional[float]): Seconds between re-pricing. None for auto-detect.
            fallback (str): What to do on timeout. One of:
                - "ioc": Place an IOC market order for remaining size (default)
                - "market": Place a market order for remaining size
                - "cancel": Give up, return last order as-is
            reduce_only (bool): Whether the order should only reduce position

        Returns:
            Order: The final order. Check order.status and order.filled_size for results.

        Raises:
            RuntimeError: If client is not authenticated
            ValueError: If parameters are invalid
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        if fallback not in ("ioc", "market", "cancel"):
            raise ValueError("fallback must be 'ioc', 'market', or 'cancel'")

        # Auto-detect timing from spread if not specified
        if timeout is None or reprice_interval is None:
            auto_timeout, auto_reprice = self._auto_reprice_interval(symbol)
            if timeout is None:
                timeout = auto_timeout
            if reprice_interval is None:
                reprice_interval = auto_reprice
            logger.info(f"Maker order {symbol}: auto-detected timeout={timeout}s, reprice={reprice_interval}s")

        remaining_size = size
        last_order = None
        attempts = 0
        start_time = time.time()
        deadline = start_time + timeout
        last_spread_bps = None

        def _annotate(order: Order, is_maker: bool) -> Order:
            """Attach execution metadata to the order."""
            order.is_maker = is_maker
            order.attempts = attempts
            order.elapsed = round(time.time() - start_time, 2)
            order.spread_bps = last_spread_bps
            return order

        while time.time() < deadline and remaining_size > 0:
            # Get best price for our side
            book = self.get_order_book(symbol)
            mid = book.get("mid_price")
            spread = book.get("spread")
            if mid and spread and mid > 0:
                last_spread_bps = round((spread / mid) * 10000, 2)

            if is_buy:
                best_price = book.get("best_bid")
            else:
                best_price = book.get("best_ask")

            if best_price is None:
                logger.warning(f"No {'bid' if is_buy else 'ask'} in order book for {symbol}, retrying...")
                time.sleep(min(reprice_interval, max(0.1, deadline - time.time())))
                continue

            limit_price = self._format_price(symbol, best_price)
            formatted_size = self._format_size(symbol, remaining_size)
            attempts += 1

            # Place post_only order
            try:
                order = self.create_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=formatted_size,
                    limit_price=limit_price,
                    reduce_only=reduce_only,
                    post_only=True,
                    time_in_force="Gtc",
                )
                last_order = order
            except ValueError as e:
                # post_only rejected (would cross spread) — retry at next interval
                logger.debug(f"Post-only rejected for {symbol}: {e}")
                time.sleep(min(reprice_interval, max(0.1, deadline - time.time())))
                continue

            # If filled immediately
            if order.status == "filled":
                logger.info(f"Maker order filled immediately: {symbol} {'buy' if is_buy else 'sell'} "
                           f"{order.filled_size} @ {order.average_fill_price}")
                return _annotate(order, is_maker=True)

            # Sleep for reprice interval, then cancel and check result
            order_id = int(order.order_id)
            sleep_time = min(reprice_interval, max(0.1, deadline - time.time()))
            time.sleep(sleep_time)

            # Try to cancel — if it fails, order was likely filled
            cancelled = self.cancel_order(order_id, symbol)

            # Single status check: detect fill or partial fill
            try:
                status = self.get_order_status(order_id)
                order_info = status.get("order", {})
                order_detail = order_info.get("order", {})

                # Extract fill price from status response
                avg_px_str = order_detail.get("avgPx") or order_detail.get("limitPx")
                avg_px = Decimal(str(avg_px_str)) if avg_px_str else None

                if order_info.get("status") == "filled":
                    return _annotate(
                        self._make_filled_order(order_id, symbol, is_buy, remaining_size, order, avg_px),
                        is_maker=True,
                    )

                # Check for partial fills
                orig_sz = Decimal(str(order_detail.get("origSz", formatted_size)))
                current_sz = Decimal(str(order_detail.get("sz", formatted_size)))
                partial_filled = orig_sz - current_sz
                if partial_filled > 0:
                    remaining_size = float(Decimal(str(remaining_size)) - partial_filled)
                    remaining_size = max(0, remaining_size)
                    logger.info(f"Partial fill detected: {partial_filled} filled, {remaining_size} remaining")
                    if remaining_size <= 0:
                        return _annotate(
                            self._make_filled_order(order_id, symbol, is_buy, size, order, avg_px),
                            is_maker=True,
                        )
            except Exception as e:
                logger.debug(f"Error checking order status after cancel: {e}")

        # Timeout reached — apply fallback for remaining size
        if remaining_size > 0 and fallback != "cancel":
            formatted_remaining = self._format_size(symbol, remaining_size)
            if formatted_remaining <= 0:
                if last_order is not None:
                    return _annotate(last_order, is_maker=True)
            logger.info(f"Maker chase timeout for {symbol}, falling back to {fallback} "
                       f"for remaining {formatted_remaining}")
            if fallback == "ioc":
                fallback_order = self.create_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=formatted_remaining,
                    limit_price=None,
                    reduce_only=reduce_only,
                    time_in_force="Ioc",
                )
                return _annotate(fallback_order, is_maker=False)
            elif fallback == "market":
                if is_buy:
                    fallback_order = self.buy(symbol, formatted_remaining, reduce_only=reduce_only)
                else:
                    fallback_order = self.sell(symbol, formatted_remaining, reduce_only=reduce_only)
                return _annotate(fallback_order, is_maker=False)

        if last_order is not None:
            return _annotate(last_order, is_maker=False)

        raise RuntimeError(f"maker_order failed: no order placed for {symbol}")

    def maker_buy(
        self,
        symbol: str,
        size: float,
        timeout: Optional[float] = None,
        reprice_interval: Optional[float] = None,
        fallback: str = "ioc",
        reduce_only: bool = False,
    ) -> Order:
        """Place a maker buy order. Convenience wrapper for maker_order().

        See maker_order() for full documentation.
        """
        return self.maker_order(symbol, True, size, timeout, reprice_interval, fallback, reduce_only)

    def maker_sell(
        self,
        symbol: str,
        size: float,
        timeout: Optional[float] = None,
        reprice_interval: Optional[float] = None,
        fallback: str = "ioc",
        reduce_only: bool = False,
    ) -> Order:
        """Place a maker sell order. Convenience wrapper for maker_order().

        See maker_order() for full documentation.
        """
        return self.maker_order(symbol, False, size, timeout, reprice_interval, fallback, reduce_only)

    def close(
        self,
        symbol: str,
        position: Optional[Position] = None,
        slippage: Optional[float] = None,
    ) -> Order:
        """Close an existing position.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            position (Optional[Position]): Position object, if None will fetch current position
            slippage (Optional[float]): Slippage for market close. Overrides default_slippage if set.

        Returns:
            Order: Order response for the closing order

        Raises:
            ValueError: If no position exists for the symbol
        """
        if position is None:
            positions = self.get_positions()
            position = next((p for p in positions if p.symbol == symbol), None)

        if not position:
            raise PositionNotFoundException(f"No open position found for {symbol}")

        size = abs(float(position.size))
        is_buy = float(position.size) < 0  # Buy to close shorts, sell to close longs

        order = self.create_order(
            symbol=symbol,
            size=size,
            is_buy=is_buy,
            reduce_only=True,
            time_in_force="Ioc",  # Market order
            slippage=slippage,
        )

        if order.status != "filled":
            logger.warning(
                f"close({symbol}) IOC order {order.order_id} was NOT filled "
                f"(status={order.status}). Position may still be open!"
            )

        return order

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        """Cancel all open orders, optionally filtered by symbol.

        Args:
            symbol (Optional[str]): If provided, only cancels orders for this symbol

        Raises:
            RuntimeError: If client is not authenticated
            Exception: If fetching orders fails or if any cancellation fails
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        # Let this propagate on failure - trader MUST know if we can't fetch orders
        open_orders = []
        for dex in self.perp_dexs:
            open_orders.extend(self._with_retry(self.info.open_orders, self.account.public_address, dex=dex))

        if symbol:
            open_orders = [order for order in open_orders if order["coin"] == symbol]

        if not open_orders:
            return

        # Bulk cancel in a single API call
        cancel_requests = [{"coin": order["coin"], "oid": order["oid"]} for order in open_orders]
        self._with_retry(self.exchange.bulk_cancel, cancel_requests)

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

        # Get open orders from the API - let errors propagate
        open_orders_response = []
        for dex in self.perp_dexs:
            open_orders_response.extend(self._with_retry(
                self.info.frontend_open_orders, self.account.public_address, dex=dex
            ))

        # Filter by symbol if provided
        if symbol:
            open_orders_response = [order for order in open_orders_response if order["coin"] == symbol]

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
                else:
                    order_type["limit"] = {
                        "tif": order_data.get("tif", "Gtc"),
                        "px": order_data.get("limitPx")
                    }
                    order_type_str = "limit"

                is_buy = order_data.get("side") == "B"

                order_dict = {
                    "order_id": str(order_data.get("oid", "")),
                    "symbol": order_data.get("coin", ""),
                    "is_buy": is_buy,
                    "size": Decimal(str(order_data.get("sz", 0))),
                    "order_type": order_type,
                    "reduce_only": order_data.get("reduceOnly", False),
                    "status": "open",
                    "time_in_force": order_data.get("tif", "Gtc") or "Gtc",
                    "created_at": order_data.get("timestamp", 0),
                    "filled_size": Decimal(str(order_data.get("origSz", 0))) - Decimal(str(order_data.get("sz", 0))),
                    "type": order_type_str,
                    "children": order_data.get("children") or None,
                    "is_position_tpsl": order_data.get("isPositionTpsl"),
                    "trigger_condition": order_data.get("triggerCondition"),
                }

                if "limitPx" in order_data:
                    order_dict["limit_price"] = Decimal(str(order_data["limitPx"]))
                if "triggerPx" in order_data:
                    order_dict["trigger_price"] = Decimal(str(order_data["triggerPx"]))

                order = from_dict(data_class=Order, data=order_dict, config=DACITE_CONFIG)
                orders.append(order)
            except Exception as e:
                logger.error(f"Error processing order data: {e}, order_data: {order_data}")

        return orders

    def get_price(self, symbol: Optional[str] = None) -> Union[float, Dict[str, float]]:
        """Get current price(s). No authentication required."""
        prices: Dict[str, float] = {}
        for dex in self.perp_dexs:
            response = self._with_retry(self.info.all_mids, dex=dex)
            prices.update({sym: float(price) for sym, price in response.items()})

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

    def get_spot_balance(self, address: Optional[str] = None, simple: bool = True,
                         prices: Optional[Dict[str, float]] = None) -> Union[Decimal, SpotState]:
        """Get spot trading balance for an address.

        Args:
            address (Optional[str]): The address to get balance for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the total balance. If False, returns detailed information.
            prices (Optional[Dict[str, float]]): Pre-fetched price dict from get_price(). If None, fetches fresh
                prices internally. Pass this when calling get_spot_balance for multiple wallets to avoid
                redundant API calls.

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
        response = self._with_retry(self.info.spot_user_state, address)

        # Get current prices for all tokens (use provided prices or fetch fresh)
        if prices is None:
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
                    
                # Get token price (stablecoins default to $1 since they have no perp market)
                token_price = Decimal(str(prices.get(token, '0')))
                if token_price == 0 and token.upper() in ('USDC', 'USDT', 'USDC.E', 'USDBC'):
                    token_price = Decimal('1')
                
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
                
            except Exception as e:
                logger.warning(f"Error processing balance for token {token}: {str(e)}")
                continue
                
        if simple:
            return total_balance
            
        return SpotState(
            total_balance=total_balance,
            tokens=token_balances,
            raw_state=response
        )

    # ── Spot trading ─────────────────────────────────────────────────────

    def _resolve_spot_pair(self, token: str) -> str:
        """Resolve a token name to its spot pair name recognized by the SDK.

        Args:
            token: Token name (e.g., "PURR", "HYPE")

        Returns:
            The pair name (e.g., "PURR/USDC") that exists in info.name_to_coin.

        Raises:
            ValueError: If no spot pair found for the token.
        """
        pair = f"{token}/USDC"
        if pair in self.info.name_to_coin:
            return pair
        raise ValueError(
            f"No spot pair found for '{token}'. "
            f"Try the full pair name (e.g., 'PURR/USDC') or check available spot pairs."
        )

    def _get_spot_sz_decimals(self, pair_name: str) -> int:
        """Get szDecimals for a spot pair from the SDK's cached data."""
        coin = self.info.name_to_coin[pair_name]
        asset = self.info.coin_to_asset[coin]
        return self.info.asset_to_sz_decimals.get(asset, 0)

    def _format_spot_price(self, pair_name: str, price: float) -> float:
        """Format a price for a spot order (8 decimal places, matching SDK)."""
        sz_decimals = self._get_spot_sz_decimals(pair_name)
        return round(float(f"{price:.5g}"), max(0, 8 - sz_decimals))

    def _format_spot_size(self, pair_name: str, size: float) -> float:
        """Format a size for a spot order."""
        sz_decimals = self._get_spot_sz_decimals(pair_name)
        return round(size, sz_decimals)

    def transfer_to_spot(self, amount: float) -> dict:
        """Transfer USDC from perp wallet to spot wallet.

        Note: This does not work with API wallets. Use the main wallet's private key.

        Args:
            amount (float): Amount of USDC to transfer.

        Returns:
            dict: API response.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        if amount <= 0:
            raise ValueError("Amount must be positive")
        result = self._with_retry(self.exchange.usd_class_transfer, amount, False)
        if result.get("status") == "err":
            msg = result.get("response", "unknown error")
            if "Must deposit" in msg:
                raise ValueError("Transfer failed: API wallets cannot transfer funds. Use the main wallet's private key.")
            raise ValueError(f"Transfer failed: {msg}")
        return result

    def transfer_to_perp(self, amount: float) -> dict:
        """Transfer USDC from spot wallet to perp wallet.

        Note: This does not work with API wallets. Use the main wallet's private key.

        Args:
            amount (float): Amount of USDC to transfer.

        Returns:
            dict: API response.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        if amount <= 0:
            raise ValueError("Amount must be positive")
        result = self._with_retry(self.exchange.usd_class_transfer, amount, True)
        if result.get("status") == "err":
            msg = result.get("response", "unknown error")
            if "Must deposit" in msg:
                raise ValueError("Transfer failed: API wallets cannot transfer funds. Use the main wallet's private key.")
            raise ValueError(f"Transfer failed: {msg}")
        return result

    def get_spot_price(self, token: str) -> float:
        """Get current mid price for a spot token.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").

        Returns:
            float: Current mid price.
        """
        pair_name = self._resolve_spot_pair(token)
        coin = self.info.name_to_coin[pair_name]
        mids = self._with_retry(self.info.all_mids)
        # Spot mids are keyed by the internal coin name
        price_str = mids.get(coin)
        if price_str is None:
            raise ValueError(f"No price found for {token} (pair: {pair_name}, coin: {coin})")
        return float(price_str)

    def create_spot_order(
        self,
        token: str,
        size: float,
        is_buy: bool,
        limit_price: Optional[float] = None,
        post_only: bool = False,
        time_in_force: Literal["Gtc", "Ioc", "Alo"] = "Gtc",
        slippage: Optional[float] = None,
    ) -> Order:
        """Create a spot order.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").
            size (float): Order size in token units.
            is_buy (bool): True for buy, False for sell.
            limit_price (Optional[float]): Price for limit orders. None for market.
            post_only (bool): Maker only (limit orders only).
            time_in_force (str): "Gtc", "Ioc", or "Alo".
            slippage (Optional[float]): Slippage for market orders.

        Returns:
            Order: Order response.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        pair_name = self._resolve_spot_pair(token)

        # Market order: get price and apply slippage
        if limit_price is None:
            current_price = self.get_spot_price(token)
            if current_price <= 0:
                raise ValueError(f"Invalid current price for {token}: {current_price}")
            effective_slippage = slippage if slippage is not None else self.default_slippage
            if not 0 < effective_slippage <= 0.5:
                raise ValueError("slippage must be between 0 (exclusive) and 0.5 (inclusive)")
            limit_price = current_price * (1 + effective_slippage) if is_buy else current_price * (1 - effective_slippage)

        # Format size and price for spot
        size = self._format_spot_size(pair_name, size)
        limit_price = self._format_spot_price(pair_name, limit_price)

        sz_decimals = self._get_spot_sz_decimals(pair_name)
        min_size = 1.0 / (10 ** sz_decimals) if sz_decimals > 0 else 1
        if size < min_size:
            raise ValueError(f"Minimum size for {token} is {min_size} (szDecimals={sz_decimals})")

        order_type = {"limit": {"tif": time_in_force}}
        if post_only:
            if time_in_force == "Ioc":
                raise ValueError("post_only cannot be used with IOC orders")
            order_type["limit"]["postOnly"] = True

        logger.debug(f"Spot order: pair={pair_name}, size={size}, price={limit_price}, buy={is_buy}")

        try:
            response = self._with_retry(
                self.exchange.order,
                name=pair_name,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type=order_type,
                reduce_only=False,
            )

            logger.debug(f"Spot order response: {response}")

            if isinstance(response, dict):
                if response.get("status") != "ok":
                    raise ValueError(f"Order failed with status: {response.get('status')}")

                if "response" in response and "data" in response["response"]:
                    statuses = response["response"]["data"].get("statuses", [])
                    if statuses and "error" in statuses[0]:
                        raise ValueError(f"Order error: {statuses[0]['error']}")

                    if statuses and "resting" in statuses[0]:
                        order_data = {
                            "order_id": str(statuses[0]["resting"]["oid"]),
                            "symbol": token,
                            "is_buy": is_buy,
                            "size": str(size),
                            "order_type": order_type,
                            "reduce_only": False,
                            "status": "open",
                            "time_in_force": time_in_force,
                            "created_at": int(time.time() * 1000),
                            "limit_price": str(limit_price),
                        }
                        return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
                    elif statuses and "filled" in statuses[0]:
                        filled_sz = statuses[0]["filled"].get("totalSz", size)
                        order_data = {
                            "order_id": str(statuses[0]["filled"]["oid"]),
                            "symbol": token,
                            "is_buy": is_buy,
                            "size": str(size),
                            "filled_size": str(filled_sz),
                            "average_fill_price": str(statuses[0]["filled"]["avgPx"]),
                            "order_type": order_type,
                            "reduce_only": False,
                            "status": "filled",
                            "time_in_force": time_in_force,
                            "created_at": int(time.time() * 1000),
                            "limit_price": str(limit_price),
                        }
                        return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)
                else:
                    raise ValueError(f"Unexpected response structure: {response}")

            raise ValueError(f"Unexpected response format: {type(response)} - {response}")

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Spot order failed for {token}: {str(e)}")
            raise

    def spot_buy(
        self,
        token: str,
        size: float,
        limit_price: Optional[float] = None,
        post_only: bool = False,
        slippage: Optional[float] = None,
    ) -> Order:
        """Buy a spot token.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").
            size (float): Amount of tokens to buy.
            limit_price (Optional[float]): Price for limit order. None for market order.
            post_only (bool): Maker only (limit orders only).
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage.

        Returns:
            Order: Order response.
        """
        time_in_force = "Gtc" if limit_price is not None else "Ioc"
        return self.create_spot_order(
            token=token,
            size=size,
            is_buy=True,
            limit_price=limit_price,
            post_only=post_only,
            time_in_force=time_in_force,
            slippage=slippage,
        )

    def spot_sell(
        self,
        token: str,
        size: float,
        limit_price: Optional[float] = None,
        post_only: bool = False,
        slippage: Optional[float] = None,
    ) -> Order:
        """Sell a spot token.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").
            size (float): Amount of tokens to sell.
            limit_price (Optional[float]): Price for limit order. None for market order.
            post_only (bool): Maker only (limit orders only).
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage.

        Returns:
            Order: Order response.
        """
        time_in_force = "Gtc" if limit_price is not None else "Ioc"
        return self.create_spot_order(
            token=token,
            size=size,
            is_buy=False,
            limit_price=limit_price,
            post_only=post_only,
            time_in_force=time_in_force,
            slippage=slippage,
        )

    def spot_cancel_order(self, order_id: Union[str, int], token: str) -> bool:
        """Cancel a specific spot order.

        Args:
            order_id (Union[str, int]): The order ID to cancel.
            token (str): Token name (e.g., "PURR").

        Returns:
            bool: True if cancelled, False if not found.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        pair_name = self._resolve_spot_pair(token)
        try:
            self._with_retry(self.exchange.cancel, pair_name, int(order_id))
            return True
        except Exception as e:
            err = str(e).lower()
            if "not found" in err or "already" in err or "no order" in err:
                return False
            raise

    def spot_cancel_all_orders(self, token: Optional[str] = None) -> None:
        """Cancel all open spot orders.

        Args:
            token (Optional[str]): If provided, only cancels orders for this token.
                                   If None, cancels all spot orders.

        Raises:
            RuntimeError: If any cancellation fails.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        # Build set of spot coin names
        spot_coins = {coin for coin, asset in self.info.coin_to_asset.items() if asset >= 10000}

        # Filter to specific token if requested
        if token:
            pair_name = self._resolve_spot_pair(token)
            target_coin = self.info.name_to_coin[pair_name]
            spot_coins = {target_coin}

        open_orders = self._with_retry(self.info.open_orders, self.account.public_address)
        spot_orders = [o for o in open_orders if o["coin"] in spot_coins]

        if not spot_orders:
            return

        # Bulk cancel in a single API call
        cancel_requests = [{"coin": order["coin"], "oid": order["oid"]} for order in spot_orders]
        self._with_retry(self.exchange.bulk_cancel, cancel_requests)

    def get_spot_open_orders(self, token: Optional[str] = None) -> List[Order]:
        """Get open spot orders.

        Args:
            token (Optional[str]): If provided, only returns orders for this token.

        Returns:
            List[Order]: List of open spot orders.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        # Build set of spot coin names and reverse mapping (coin -> token name)
        spot_coin_to_token: Dict[str, str] = {}
        meta = self.info.spot_meta()
        for pair in meta["universe"]:
            base_idx = pair["tokens"][0]
            base_name = meta["tokens"][base_idx]["name"]
            coin = self.info.name_to_coin.get(f"{base_name}/USDC")
            if coin:
                spot_coin_to_token[coin] = base_name

        # Filter to specific token if requested
        if token:
            pair_name = self._resolve_spot_pair(token)
            target_coin = self.info.name_to_coin[pair_name]
            spot_coin_to_token = {target_coin: token}

        response = self._with_retry(
            self.info.frontend_open_orders, self.account.public_address
        )

        orders = []
        for order_data in response:
            coin = order_data.get("coin", "")
            if coin not in spot_coin_to_token:
                continue

            token_name = spot_coin_to_token[coin]
            try:
                order_type = {}
                if order_data.get("orderType") == "Limit":
                    order_type["limit"] = {
                        "tif": order_data.get("tif", "Gtc"),
                    }

                order = Order(
                    order_id=str(order_data["oid"]),
                    symbol=token_name,
                    is_buy=order_data["side"] == "B",
                    size=Decimal(str(order_data.get("sz", "0"))),
                    order_type=from_dict(data_class=OrderType, data=order_type, config=DACITE_CONFIG),
                    status="open",
                    time_in_force=order_data.get("tif", "Gtc"),
                    created_at=order_data.get("timestamp", 0),
                    limit_price=Decimal(str(order_data.get("limitPx", "0"))),
                    filled_size=Decimal(str(order_data.get("origSz", "0"))) - Decimal(str(order_data.get("sz", "0"))),
                    type="limit",
                )
                orders.append(order)
            except Exception as e:
                logger.warning(f"Failed to parse spot order: {e}")
                continue

        return orders

    def get_spot_order_book(self, token: str) -> Dict[str, Any]:
        """Get the order book for a spot token.

        Args:
            token (str): Token name (e.g., "FRAC", "HYPE").

        Returns:
            Dict[str, Any]: Order book data (same format as get_order_book).
        """
        pair_name = self._resolve_spot_pair(token)
        return self.get_order_book(pair_name)

    def get_spot_fills(self, token: Optional[str] = None) -> list:
        """Get recent spot fills for the authenticated user.

        Args:
            token (Optional[str]): If provided, only returns fills for this token.

        Returns:
            List[Fill]: List of spot fill objects.
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        # Build reverse mapping: internal coin name -> token name
        spot_coin_to_token: Dict[str, str] = {}
        meta = self.info.spot_meta()
        for pair in meta["universe"]:
            base_idx = pair["tokens"][0]
            base_name = meta["tokens"][base_idx]["name"]
            coin = self.info.name_to_coin.get(f"{base_name}/USDC")
            if coin:
                spot_coin_to_token[coin] = base_name
                # Also map the human-readable pair name
                spot_coin_to_token[f"{base_name}/USDC"] = base_name

        raw = self._with_retry(self.info.user_fills, self.public_address)
        fills = []
        for f in raw:
            coin = f.get("coin", "")
            if coin not in spot_coin_to_token:
                continue
            fill = self._parse_fill(f)
            # Replace internal coin name with token name
            fill.symbol = spot_coin_to_token[coin]
            if token and fill.symbol != token:
                continue
            fills.append(fill)
        return fills

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
        response = self._with_retry(self.info.evm_state, address)
        
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
            # Get all markets
            markets = client.get_market_info()

            # Get specific market
            btc_info = client.get_market_info("BTC")
        """
        markets = []
        for dex in self.perp_dexs:
            response = self._with_retry(self.info.meta, dex=dex)
            markets.extend(response['universe'])

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
        url = f"{self.base_url}/info"
        
        payload = json.dumps({
            "type": "predictedFundings"
        })
        headers = {
            'Content-Type': 'application/json'
        }
        
        try:
            response = self._with_retry(requests.post, url, headers=headers, data=payload)
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
        """Cancel all open orders across all symbols.

        Alias for cancel_all_orders(). Prefer cancel_all_orders() for new code.
        """
        return self.cancel_all_orders()

    def _fetch_market_specs(self) -> Dict[str, Dict]:
        """Fetch current market specifications from the API using meta_and_asset_ctxs for richer data."""
        specs = {}

        for dex in self.perp_dexs:
            try:
                response = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex})
                meta = response[0]
                ctxs = response[1] if len(response) > 1 else []
            except Exception:
                try:
                    meta = self.info.meta(dex=dex)
                    ctxs = []
                except Exception:
                    continue

            universe = meta.get('universe', meta) if isinstance(meta, dict) else meta

            for i, market in enumerate(universe):
                spec = {
                    "size_decimals": market.get('szDecimals', 3),
                    "price_decimals": market.get('px_dps', 1),
                    "max_leverage": market.get('maxLeverage', 50),
                    "only_isolated": market.get('onlyIsolated', False),
                }
                if i < len(ctxs) and isinstance(ctxs[i], dict):
                    spec["funding"] = ctxs[i].get("funding")
                    spec["open_interest"] = ctxs[i].get("openInterest")
                    spec["mark_price"] = ctxs[i].get("markPx")
                specs[market['name']] = spec

        if not specs:
            raise RuntimeError("Failed to fetch market specs from any dex")
        return specs

    def refresh_market_specs(self) -> Dict[str, Dict]:
        """Refresh market specifications from the API.

        Returns:
            Dict[str, Dict]: Updated market specs.
        """
        try:
            self.market_specs = self._fetch_market_specs()
            self._market_specs_fetched_at = time.time()
            HyperliquidClient._cached_market_specs = self.market_specs
            HyperliquidClient._cached_market_specs_at = self._market_specs_fetched_at
        except Exception as e:
            logger.warning(f"Failed to refresh market specs: {e}. Keeping existing specs.")
        return self.market_specs

    def _ensure_fresh_market_specs(self) -> None:
        """Auto-refresh market specs if older than 1 hour."""
        if time.time() - self._market_specs_fetched_at > self._CACHE_TTL:
            try:
                self.market_specs = self._fetch_market_specs()
                self._market_specs_fetched_at = time.time()
                HyperliquidClient._cached_market_specs = self.market_specs
                HyperliquidClient._cached_market_specs_at = self._market_specs_fetched_at
                logger.debug("Auto-refreshed stale market specs")
            except Exception as e:
                logger.warning(f"Failed to auto-refresh market specs: {e}")

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
            response = self._with_retry(
                self.exchange.modify_order,
                oid=oid,
                name=symbol,
                is_buy=is_buy,
                sz=size,
                limit_px=price,
                order_type=order_type,
                reduce_only=reduce_only,
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

        Tries to modify the existing stop loss order in place. If modification
        fails (e.g. exchange rejects it), falls back to cancelling the old
        order and creating a new one.

        If no stop loss order exists yet, creates one automatically using the
        current position size and direction.

        Args:
            symbol (str): The trading symbol
            new_price (float): The new stop loss trigger price

        Returns:
            Optional[Order]: The updated or newly created stop loss order

        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to update stop loss")

        position_size = abs(float(self.get_position_size(symbol)))

        open_orders = self.get_open_orders(symbol)
        sl_orders = [order for order in open_orders if order.type == "stop_loss"]

        if not sl_orders:
            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.stop_loss(symbol, position_size, new_price, is_buy=is_buy)

        sl_order = sl_orders[0]

        order_type = {
            "trigger": {
                "triggerPx": new_price,
                "isMarket": True,
                "tpsl": "sl"
            }
        }

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

            for order in sl_orders:
                try:
                    self._with_retry(self.exchange.cancel, symbol, int(order.order_id))
                    time.sleep(0.5)
                except Exception as e:
                    logging.warning(f"Failed to cancel stop loss order {order.order_id}: {str(e)}")

            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.stop_loss(symbol, position_size, new_price, is_buy=is_buy)
        
    def update_take_profit(self, symbol: str, new_price: float) -> Optional[Order]:
        """Update the take profit price for an existing position.

        Tries to modify the existing take profit order in place. If modification
        fails (e.g. exchange rejects it), falls back to cancelling the old
        order and creating a new one.

        If no take profit order exists yet, creates one automatically using the
        current position size and direction.

        Args:
            symbol (str): The trading symbol
            new_price (float): The new take profit trigger price

        Returns:
            Optional[Order]: The updated or newly created take profit order

        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client must be authenticated to update take profit")

        position_size = abs(float(self.get_position_size(symbol)))

        open_orders = self.get_open_orders(symbol)
        tp_orders = [order for order in open_orders if order.type == "take_profit"]

        if not tp_orders:
            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.take_profit(symbol, position_size, new_price, is_buy=is_buy)

        tp_order = tp_orders[0]

        order_type = {
            "trigger": {
                "triggerPx": new_price,
                "isMarket": True,
                "tpsl": "tp"
            }
        }

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

            for order in tp_orders:
                try:
                    self._with_retry(self.exchange.cancel, symbol, int(order.order_id))
                    time.sleep(0.5)
                except Exception as e:
                    logging.warning(f"Failed to cancel take profit order {order.order_id}: {str(e)}")

            position_direction = self.get_position_direction(symbol)
            is_buy = position_direction == "short"
            return self.take_profit(symbol, position_size, new_price, is_buy=is_buy)
        
    def trailing_stop(self, symbol: str, trail_percent: float) -> Optional[Order]:
        """Set a stop loss at a percentage distance from the current price.

        This is a one-shot update, not a live trailing stop. It calculates the
        stop price based on the current market price and trail_percent, then
        calls update_stop_loss(). To simulate a live trailing stop, call this
        method repeatedly from a loop or scheduler (e.g. every 30 seconds).

        For long positions, the stop is placed below the current price.
        For short positions, the stop is placed above the current price.

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

    def get_open_order_by_id(self, symbol: str, order_id: Union[str, int]) -> Optional[Order]:
        """Find an open order by its ID.

        Only searches currently open orders. Returns None if the order is
        already filled, cancelled, or doesn't exist. Use get_order_status()
        to query any order regardless of state.

        Args:
            symbol (str): The trading symbol
            order_id (Union[str, int]): The order ID to find

        Returns:
            Optional[Order]: The open order if found, None otherwise

        Raises:
            RuntimeError: If the client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("Client is not authenticated")

        oid = str(order_id)
        orders = self.get_open_orders(symbol)
        for order in orders:
            if order.order_id == oid:
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

    def cancel_order(self, order_id: Union[str, int], symbol: str) -> bool:
        """Cancel a specific order by order ID and symbol.

        Args:
            order_id (Union[str, int]): The order ID to cancel
            symbol (str): The symbol the order is for

        Returns:
            bool: True if order was successfully cancelled, False if order was
                  not found or already processed.

        Raises:
            RuntimeError: If client is not authenticated
            Exception: On network errors, auth failures, or other unexpected errors
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        oid = int(order_id)
        try:
            self._with_retry(self.exchange.cancel, symbol, oid)
            logger.info(f"Successfully cancelled order {order_id} for {symbol}")
            return True
        except Exception as e:
            error_str = str(e).lower()
            # Only return False for "order doesn't exist" type errors
            if any(phrase in error_str for phrase in [
                "not found", "unknown oid", "already", "no order"
            ]):
                logger.info(f"Order {order_id} for {symbol} not found or already processed")
                return False
            # Re-raise real errors (network, auth, etc.)
            raise

    def get_order_book(self, symbol: str) -> Dict[str, Any]:
        """Get the current order book (L2 snapshot) for a symbol.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")

        Returns:
            Dict[str, Any]: Order book data with the following structure:
                {
                    "symbol": str,
                    "bids": List[Dict],   # [{price, size, num_orders}, ...] descending by price
                    "asks": List[Dict],   # [{price, size, num_orders}, ...] ascending by price
                    "timestamp": int,
                    "best_bid": Optional[float],   # None if no bids
                    "best_ask": Optional[float],   # None if no asks
                    "spread": Optional[float],     # None if missing bid or ask
                    "mid_price": Optional[float]   # None if missing bid or ask
                }

        Raises:
            ValueError: If order book response structure is invalid
        """
        l2_data = self._with_retry(self.info.l2_snapshot, symbol)

        if not isinstance(l2_data, dict) or "levels" not in l2_data:
            raise ValueError(f"Invalid order book response for {symbol}")

        levels = l2_data.get("levels", [])
        if len(levels) < 2:
            raise ValueError(f"Insufficient order book levels for {symbol}")

        bids_raw = levels[0]
        asks_raw = levels[1]

        bids = []
        for bid in bids_raw:
            if isinstance(bid, dict) and "px" in bid and "sz" in bid:
                bids.append({
                    "price": float(bid["px"]),
                    "size": float(bid["sz"]),
                    "num_orders": bid.get("n", 0),
                })

        asks = []
        for ask in asks_raw:
            if isinstance(ask, dict) and "px" in ask and "sz" in ask:
                asks.append({
                    "price": float(ask["px"]),
                    "size": float(ask["sz"]),
                    "num_orders": ask.get("n", 0),
                })

        bids.sort(key=lambda x: x["price"], reverse=True)
        asks.sort(key=lambda x: x["price"])

        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else None
        spread = best_ask - best_bid if best_bid and best_ask else None

        return {
            "symbol": symbol,
            "bids": bids,
            "asks": asks,
            "timestamp": l2_data.get("time", int(time.time() * 1000)),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid_price": mid_price,
        }

    def get_optimal_limit_price(
        self, 
        symbol: str, 
        side: str, 
        urgency_factor: float = 0.5
    ) -> float:
        """Get optimal limit price by analyzing order book and urgency factor.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            side (str): 'buy' or 'sell'
            urgency_factor (float): Urgency factor from 0.0 to 1.0. 
                                  0.0 = very patient (far from market), 1.0 = very aggressive (close to market)
            
        Returns:
            float: Optimal limit price
            
        Raises:
            ValueError: If parameters are invalid or order book data is unavailable
        """
        if side not in ["buy", "sell"]:
            raise ValueError("side must be 'buy' or 'sell'")

        if not 0.0 <= urgency_factor <= 1.0:
            raise ValueError("urgency_factor must be between 0.0 and 1.0")

        order_book = self.get_order_book(symbol)
        current_mid = self.get_price(symbol)

        if side == "buy":
            if not order_book["bids"]:
                return self._format_price(symbol, current_mid)

            best_bid = order_book["best_bid"]

            # At urgency 0.0: place at best bid (patient, maker)
            # At urgency 1.0: place at best ask (aggressive, crosses spread)
            if order_book["best_ask"]:
                optimal_price = best_bid + (order_book["best_ask"] - best_bid) * urgency_factor
            else:
                optimal_price = best_bid

        else:
            if not order_book["asks"]:
                return self._format_price(symbol, current_mid)

            best_ask = order_book["best_ask"]

            # At urgency 0.0: place at best ask (patient, maker)
            # At urgency 1.0: place at best bid (aggressive, crosses spread)
            if order_book["best_bid"]:
                optimal_price = best_ask - (best_ask - order_book["best_bid"]) * urgency_factor
            else:
                optimal_price = best_ask

        formatted_price = self._format_price(symbol, optimal_price)

        logger.debug(f"Optimal {side} price for {symbol}: {formatted_price} "
                    f"(urgency: {urgency_factor:.3f}, mid: {current_mid})")

        return formatted_price

    def set_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> dict:
        """Set leverage for a symbol.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            leverage (int): Leverage multiplier (e.g., 10)
            is_cross (bool): True for cross margin, False for isolated. Defaults to True.

        Returns:
            dict: API response

        Raises:
            RuntimeError: If client is not authenticated
            ValueError: If leverage is invalid
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        if leverage < 1:
            raise ValueError("Leverage must be >= 1")
        max_lev = self.market_specs.get(symbol, {}).get("max_leverage")
        if max_lev and leverage > int(max_lev):
            raise ValueError(f"Max leverage for {symbol} is {max_lev}")
        return self._with_retry(self.exchange.update_leverage, leverage, symbol, is_cross)

    def add_isolated_margin(self, symbol: str, amount: float) -> dict:
        """Add margin to an isolated position.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            amount (float): USD amount to add to the isolated margin

        Returns:
            dict: API response

        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        return self._with_retry(self.exchange.update_isolated_margin, amount, symbol)

    def _parse_fill(self, raw: dict) -> 'Fill':
        """Convert a raw fill dict from the API into a Fill model."""
        from .models import Fill
        return Fill(
            symbol=raw["coin"],
            side=raw.get("side", ""),
            price=Decimal(str(raw["px"])),
            size=Decimal(str(raw["sz"])),
            closed_pnl=Decimal(str(raw.get("closedPnl", "0"))),
            direction=raw.get("dir", ""),
            order_id=raw.get("oid", 0),
            crossed=raw.get("crossed", False),
            time=raw.get("time", 0),
            hash=raw.get("hash", ""),
            fee=Decimal(str(raw["fee"])) if "fee" in raw else None,
            start_position=Decimal(str(raw["startPosition"])) if "startPosition" in raw else None,
            fee_token=raw.get("feeToken"),
        )

    def get_fills(self, symbol: Optional[str] = None) -> list:
        """Get recent fills for the authenticated user.

        Args:
            symbol (Optional[str]): If provided, only returns fills for this symbol.

        Returns:
            List[Fill]: List of fill objects.

        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        raw = self._with_retry(self.info.user_fills, self.public_address)
        fills = [self._parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return fills

    def get_fills_by_time(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        symbol: Optional[str] = None,
    ) -> list:
        """Get fills within a time range.

        Args:
            start_time (int): Start time in milliseconds since epoch.
            end_time (Optional[int]): End time in ms. Defaults to current time.
            symbol (Optional[str]): If provided, only returns fills for this symbol.

        Returns:
            List[Fill]: List of fill objects.

        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        raw = self._with_retry(self.info.user_fills_by_time, self.public_address, start_time, end_time)
        fills = [self._parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return fills

    def get_order_status(self, order_id: int) -> dict:
        """Query the status of a specific order by order ID.

        Args:
            order_id (int): The order ID to query.

        Returns:
            dict: Order status from the API.

        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")
        return self._with_retry(self.info.query_order_by_oid, self.public_address, order_id)

    def bulk_order(self, orders: List[dict]) -> list:
        """Place multiple orders atomically.

        Args:
            orders: List of order dicts, each with keys:
                - symbol (str): Trading pair symbol
                - is_buy (bool): True for buy, False for sell
                - size (float): Order size
                - limit_price (float): Limit price
                - reduce_only (bool, optional): Defaults to False
                - time_in_force (str, optional): "Gtc", "Ioc", or "Alo". Defaults to "Gtc"

        Returns:
            list: API response with order statuses

        Raises:
            RuntimeError: If client is not authenticated
            ValueError: If any order is invalid
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        order_requests = []
        for o in orders:
            size, price = self._validate_and_format_order(o["symbol"], o["size"], o["limit_price"])
            order_requests.append({
                "coin": o["symbol"],
                "is_buy": o["is_buy"],
                "sz": size,
                "limit_px": price,
                "order_type": {"limit": {"tif": o.get("time_in_force", "Gtc")}},
                "reduce_only": o.get("reduce_only", False),
            })

        return self._with_retry(self.exchange.bulk_orders, order_requests)

    def bulk_cancel(self, cancels: List[dict]) -> dict:
        """Cancel multiple orders atomically.

        Args:
            cancels: List of dicts, each with keys:
                - symbol (str): Trading pair symbol
                - order_id (str or int): The order ID to cancel

        Returns:
            dict: API response

        Raises:
            RuntimeError: If client is not authenticated
        """
        if not self.is_authenticated():
            raise RuntimeError("This method requires authentication")

        cancel_requests = [{"coin": c["symbol"], "oid": int(c["order_id"])} for c in cancels]
        return self._with_retry(self.exchange.bulk_cancel, cancel_requests)

    def get_funding_history(
        self,
        symbol: str,
        start_time: int,
        end_time: Optional[int] = None,
    ) -> List[dict]:
        """Get historical funding rates for a symbol.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            start_time (int): Start time in milliseconds since epoch.
            end_time (Optional[int]): End time in ms. Defaults to current time.

        Returns:
            List[dict]: List of dicts with keys: time, funding_rate, premium
        """
        raw = self._with_retry(self.info.funding_history, symbol, start_time, end_time)
        return [
            {
                "time": r["time"],
                "funding_rate": float(r["fundingRate"]),
                "premium": float(r["premium"]),
            }
            for r in raw
        ]

    def get_portfolio(self, address: Optional[str] = None) -> dict:
        """Get portfolio performance metrics.

        Args:
            address (Optional[str]): User address. Defaults to authenticated user.

        Returns:
            dict: Portfolio data from the API.

        Raises:
            RuntimeError: If no address and not authenticated
            ValueError: If the API call fails
        """
        if address is None:
            if not self.is_authenticated():
                raise RuntimeError("Address or authentication required")
            address = self.public_address

        url = f"{self.base_url}/info"
        try:
            resp = self._with_retry(
                requests.post, url,
                headers={"Content-Type": "application/json"},
                json={"type": "portfolio", "user": address},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise ValueError(f"Failed to fetch portfolio: {str(e)}")

