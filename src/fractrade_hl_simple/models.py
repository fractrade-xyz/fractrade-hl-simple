from typing import List, TypedDict, Optional, Dict, Union, Literal, Any
from dataclasses import MISSING, dataclass, fields
import os
from dotenv import load_dotenv
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from dacite import Config as DaciteConfig
import eth_account
import logging
import re

def load_env_file() -> None:
    """Load a .env file into os.environ (variables already set win). Called when credentials
    or the network are read from the environment, never at import: importing the library
    must not change the environment."""
    load_dotenv()

@dataclass(slots=True, kw_only=True)
class HyperliquidAccount:
    private_key: str
    public_address: Optional[str] = None
    is_vault: bool = False
    
    @classmethod
    def from_key(cls, private_key: str, public_address: Optional[str] = None, is_vault: bool = False) -> "HyperliquidAccount":
        """Create a HyperliquidAccount from a private key.

        Args:
            private_key (str): The private key to use
            public_address (Optional[str]): The public address (needed for API wallets and vaults)
            is_vault (bool): If True, treat public_address as a vault/sub-account address

        Returns:
            HyperliquidAccount: The account instance

        Raises:
            ValueError: If the private key is invalid
        """
        if not private_key:
            raise ValueError("private_key is required")

        # Get public address from private key
        # if public address is provided, use it, public and private key dont need to match when its an api wallet
        if public_address is None:
            try:
                account = eth_account.Account.from_key(private_key)
                public_address = account.address
            except Exception as e:
                raise ValueError(f"Invalid private key: {str(e)}")

        return cls(
            private_key=private_key,
            public_address=public_address,
            is_vault=is_vault,
        )
    
    @classmethod
    def from_env(cls) -> "HyperliquidAccount":
        load_env_file()
        private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
        if not private_key:
            raise ValueError("HYPERLIQUID_PRIVATE_KEY environment variable is required")
            
        public_address = os.getenv("HYPERLIQUID_PUBLIC_ADDRESS")
        if not public_address:
            raise ValueError("HYPERLIQUID_PUBLIC_ADDRESS environment variable is required")

        is_vault = os.getenv("HYPERLIQUID_IS_VAULT", "").lower() in ("1", "true", "yes")

        return cls(
            private_key=private_key,
            public_address=public_address,
            is_vault=is_vault,
        )
    
    def to_dict(self) -> dict:
        return {"private_key": self.private_key}
        
    def __str__(self) -> str:
        return f"HyperliquidAccount(public_address={self.public_address})"

    def __repr__(self) -> str:
        return f"HyperliquidAccount(public_address={self.public_address})"

@dataclass(slots=True, kw_only=True)
class Leverage:
    """Leverage configuration for a position."""
    type: Literal["cross", "isolated"]
    value: Decimal

@dataclass(slots=True, kw_only=True)
class FeeRates:
    """The fee rates a wallet pays on a perp market (get_fee_rates), as fractions of an
    order's value: 0.00045 is 0.045 %. A negative maker rate is a rebate."""
    symbol: str
    taker: Decimal                       # an order that takes liquidity: market orders, triggered stops
    maker: Decimal                       # an order that rests and adds liquidity
    dex: Optional[str] = None            # the HIP-3 dex, None for Hyperliquid's own perps
    growth_mode: bool = False            # HIP-3 growth mode: protocol fees cut to a tenth
    deployer_fee_scale: Optional[Decimal] = None  # HIP-3: the deployer's share of the protocol fee


@dataclass(slots=True, kw_only=True)
class CumFunding:
    """Cumulative funding payments for a position."""
    all_time: Decimal
    since_open: Decimal
    since_change: Decimal

@dataclass(slots=True, kw_only=True)
class Position:
    """An open perpetual futures position."""
    symbol: str
    entry_price: Optional[Decimal]
    leverage: Leverage
    liquidation_price: Optional[Decimal]
    margin_used: Decimal
    position_value: Decimal
    return_on_equity: Decimal
    size: Decimal
    unrealized_pnl: Decimal
    max_trade_sizes: Optional[List[Decimal]] = None
    cum_funding: Optional[CumFunding] = None
    max_leverage: Optional[int] = None
    
    @property
    def is_long(self) -> bool:
        return self.size > 0

    @property
    def is_short(self) -> bool:
        return self.size < 0

    def __repr__(self) -> str:
        direction = "LONG" if self.is_long else "SHORT"
        return (
            f"Position({self.symbol} {direction} {self.size} "
            f"@ {self.entry_price} pnl={self.unrealized_pnl})"
        )

@dataclass(slots=True, kw_only=True)
class AssetPosition:
    """Wrapper around a Position with its margin mode."""
    position: Position
    type: Literal["oneWay"]

@dataclass(slots=True, kw_only=True)
class MarginSummary:
    """Account margin summary with balances and usage."""
    account_value: Decimal
    total_margin_used: Decimal
    total_ntl_pos: Decimal
    total_raw_usd: Decimal

@dataclass(slots=True, kw_only=True)
class SpotMarket:
    """A spot pair with its current figures (Info spotMetaAndAssetCtxs). Prices and the day
    volume are in the quote token, as Hyperliquid reports them; a figure Hyperliquid leaves out
    (e.g. the mid price of a pair without orders) is None."""
    symbol: str                      # as the spot methods name it: HYPE (quoted in USDC), HYPE/USDT0
    coin: str                        # Hyperliquid's name of the pair: @107, PURR/USDC
    base: str                        # the token traded: HYPE
    quote: str                       # the token it is priced in: USDC, USDT0, USDH, USDE
    size_decimals: int               # of the base token
    mark_price: Optional[Decimal]
    mid_price: Optional[Decimal]
    previous_day_price: Optional[Decimal]
    day_volume: Optional[Decimal]    # in the quote token


@dataclass(slots=True, kw_only=True)
class Market:
    """A perp market with its current figures (Info metaAndAssetCtxs and
    perpsAtOpenInterestCap). Prices and amounts are as Hyperliquid reports them; a figure
    Hyperliquid leaves out (e.g. the mid price of a market without orders) is None."""
    symbol: str                      # as Hyperliquid names it: BTC, xyz:NVDA
    dex: Optional[str]               # the HIP-3 dex ("xyz"), None for the default dex
    size_decimals: int
    max_leverage: int
    margin_mode: str                 # "cross", "no_cross", "strict_isolated" or "unknown"
    delisted: bool
    at_open_interest_cap: bool
    mark_price: Optional[Decimal]
    mid_price: Optional[Decimal]
    oracle_price: Optional[Decimal]
    previous_day_price: Optional[Decimal]
    funding: Optional[Decimal]       # the current hourly funding rate
    premium: Optional[Decimal]
    open_interest: Optional[Decimal]  # in base units
    day_volume_usd: Optional[Decimal]

@dataclass(slots=True, kw_only=True)
class SpotTokenBalance:
    """Represents the balance of a single token in spot trading."""
    token: str
    amount: Decimal
    usd_value: Decimal
    price: Decimal
    hold: Decimal
    entry_ntl: Decimal

@dataclass(slots=True, kw_only=True)
class SpotState:
    """Represents the complete spot trading state."""
    total_balance: Decimal
    tokens: Dict[str, SpotTokenBalance]
    raw_state: Dict[str, Any]  # Store the original API response

@dataclass(slots=True, kw_only=True)
class CollateralBalance:
    """One token balance of the spot account (in unified and portfolio-margin accounts, the
    collateral of every perp dex too)."""
    token: str
    market: str                  # "spot", or "outcome" for HIP-4 outcome balances (+12, o12)
    total: Decimal
    hold: Decimal                # held by open orders and, in unified accounts, by positions
    available: Decimal           # total - hold
    price: Optional[Decimal]     # USD; None if unpriced (outcome balances, no market)
    usd_value: Optional[Decimal]


@dataclass(slots=True, kw_only=True)
class DexAccount:
    """One perp dex (clearinghouse) of an account."""
    dex: Optional[str]                # None for the default dex, "xyz"
    account_value: Optional[Decimal]  # its own collateral and PnL: standard accounts only
    withdrawable: Optional[Decimal]   # standard accounts only
    margin_used: Decimal              # marginSummary.totalMarginUsed: cross and isolated
    positions: List[Position]
    cross_maintenance_margin_used: Optional[Decimal] = None  # as Hyperliquid reports it


@dataclass(slots=True, kw_only=True)
class AccountSummary:
    """An account's balances, by its account mode. A figure that is not
    established for the mode is None; `sources` says how each one was computed."""
    mode: str                                # "standard", "unified", "portfolio_margin", "unknown"
    account_value: Optional[Decimal]         # USD, PnL of open positions included
    available_to_trade: Optional[Decimal]    # USDC free to back new orders (unified accounts)
    margin_used: Optional[Decimal]
    balances: List[CollateralBalance]
    dexes: List[DexAccount]
    sources: Dict[str, str]


@dataclass(slots=True, kw_only=True)
class UserState:
    asset_positions: List[AssetPosition]
    margin_summary: MarginSummary
    cross_margin_summary: MarginSummary
    withdrawable: Decimal
    cross_maintenance_margin_used: Optional[Decimal] = None
    spot_state: Optional[SpotState] = None

_PERP_NAME = re.compile(r"[A-Za-z0-9]+")                   # BTC, kPEPE
_HIP3_NAME = re.compile(r"[a-z0-9]+:[A-Za-z0-9]+")         # xyz:NVDA
_SPOT_NAME = re.compile(r"@\d+|[^/\s]+/[^/\s]+")           # @107, PURR/USDC
_OUTCOME_NAME = re.compile(r"#\d+|\+\d+|o\d+")             # #12 (mids, orders), +12 / o12 (balances)

MARKETS = ("perp", "hip3_perp", "spot", "outcome", "unknown")


def _restore_with_market(obj: Any, state: Any) -> None:
    """__setstate__ of Order and Fill. Their pickled state is (None, {field: value}) (slots);
    one from before the market field lacks it, and its raw coin is not known any more (spot
    symbols are renamed), so market is "unknown". An Order from before its order_id was a
    number has it as text, which is converted."""
    attributes, values = state  # attributes: the __dict__ of a subclass without slots, else None
    restored = {f.name: f.default for f in fields(obj) if f.default is not MISSING}  # fields added since
    restored.update(values)
    restored.setdefault("market", "unknown")
    restored["market"] = restored["market"] or "unknown"
    order_id = restored.get("order_id")
    if isinstance(order_id, str) and order_id.isascii() and order_id.isdigit():  # an Order pickled with a text id
        restored["order_id"] = int(order_id)
    for name, value in restored.items():
        object.__setattr__(obj, name, value)
    if attributes:
        obj.__dict__.update(attributes)


def coin_market(coin: str) -> str:
    """The kind of market of a coin as Hyperliquid names it: "perp" (BTC,
    kPEPE), "hip3_perp" (xyz:NVDA), "spot" (@107, PURR/USDC), "outcome" (HIP-4: #12, and +12
    or o12 as balances) or "unknown". Applied to the raw coin, before any renaming."""
    for market, pattern in (("outcome", _OUTCOME_NAME), ("spot", _SPOT_NAME), ("hip3_perp", _HIP3_NAME),
                            ("perp", _PERP_NAME)):
        if pattern.fullmatch(coin or ""):
            return market
    return "unknown"


PERP_MARKETS = ("perp", "hip3_perp")  # coin_market() of a perp on any dex


@dataclass(slots=True, kw_only=True)
class Fill:
    """Represents a trade fill."""
    symbol: str
    side: str
    price: Decimal
    size: Decimal
    closed_pnl: Decimal
    direction: str
    order_id: int
    crossed: bool
    time: int
    hash: str
    fee: Optional[Decimal] = None
    start_position: Optional[Decimal] = None  # Position size before this fill
    fee_token: Optional[str] = None           # Token the fee was paid in
    market: str = ""  # coin_market() of the raw coin; filled in from symbol when not given
    trade_id: Optional[int] = None            # Hyperliquid's tid: one trade, one id

    def __post_init__(self) -> None:
        if not self.market:
            self.market = coin_market(self.symbol)

    __setstate__ = _restore_with_market

    def __repr__(self) -> str:
        return (
            f"Fill({self.symbol} {self.direction} {self.size} "
            f"@ {self.price} pnl={self.closed_pnl})"
        )

@dataclass(slots=True, kw_only=True)
class FundingPayment:
    """One funding payment of an account (Info userFunding): positive usdc was received,
    negative was paid."""
    time: int                  # ms since the epoch
    symbol: str                # as Hyperliquid names it: BTC, xyz:NVDA
    market: str                # coin_market() of the symbol
    size: Decimal              # the position at the time: positive long, negative short
    funding_rate: Decimal      # the hourly rate applied
    usdc: Decimal


@dataclass(slots=True, kw_only=True)
class FillHistory:
    """The fills of a time range, oldest first, and whether Hyperliquid's limits cut it short.

    truncated_reason: "retention_window" (Hyperliquid keeps only the 10000 most recent fills
    of an account, and older ones in the range are gone) or "same_millisecond_overflow" (more
    than one response of fills share one millisecond; the rest of the range is not reachable).
    """
    fills: List[Fill]
    truncated: bool
    truncated_reason: Optional[str] = None


@dataclass(slots=True, kw_only=True)
class OrderType:
    """Order type specification. Only one of limit, market, or trigger is set."""
    limit: Optional[Dict[str, Union[Decimal, bool, str]]] = None      # e.g. {"tif": "Gtc"}
    market: Optional[Dict] = None
    trigger: Optional[Dict[str, Union[Decimal, bool, str]]] = None    # triggerPx, isMarket, tpsl

@dataclass(slots=True, kw_only=True)
class Order:
    """An order on the exchange (open, filled, or cancelled)."""
    order_id: int                  # Hyperliquid's order id (oid)
    cloid: Optional[str] = None    # the client order id it was placed with (0x + 32 hex), if any
    symbol: str
    is_buy: bool
    size: Decimal
    order_type: OrderType
    reduce_only: bool = False
    status: str
    time_in_force: str = "GTC"
    created_at: int
    filled_size: Decimal = Decimal(0)
    average_fill_price: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    trigger_price: Optional[Decimal] = None
    fee: Optional[Decimal] = None
    type: str = "unknown"  # Can be "limit", "market", "take_profit", "stop_loss"
    is_maker: Optional[bool] = None      # True if filled as maker, False if taker/IOC fallback
    attempts: int = 1                     # Number of order placement attempts
    elapsed: Optional[float] = None       # Seconds from first attempt to fill
    spread_bps: Optional[float] = None    # Spread in basis points at time of execution
    children: Optional[List[Dict[str, Any]]] = None       # Nested child orders (bracket orders)
    is_position_tpsl: Optional[bool] = None               # Whether this is a position TP/SL order
    trigger_condition: Optional[str] = None                # Trigger condition: "mark", "index", "last"
    market: str = ""  # coin_market() of the raw coin; filled in from symbol when not given

    def __post_init__(self) -> None:
        if not self.market:
            self.market = coin_market(self.symbol)

    __setstate__ = _restore_with_market

    @property
    def remaining_size(self) -> Decimal:
        return self.size - self.filled_size
    
    @property
    def is_filled(self) -> bool:
        return self.status == "filled"
    
    @property
    def is_active(self) -> bool:
        return self.status == "open"

    def __repr__(self) -> str:
        side = "BUY" if self.is_buy else "SELL"
        price = self.limit_price or self.trigger_price or "MKT"
        return (
            f"Order({self.symbol} {side} {self.size} @ {price} "
            f"[{self.status}] id={self.order_id})"
        )

def _spec_value(value: Any) -> Any:
    """A value of an order type's limit or trigger spec: numbers and numeric strings as
    Decimal, other strings and bools as they are."""
    if value is None or isinstance(value, (bool, Decimal)):
        return value
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return value
    return number if number.is_finite() else value


def _order_type(data: Any) -> Any:
    """An OrderType from its API form ({"limit": {"tif": "Gtc"}} or {"trigger": {...}}).

    Built here, not by dacite: its int cast also applies to bool (a subclass of int), so any
    string in these specs became True (tif "Alo", tpsl "sl"). Ordering the Union differently
    does not help: typing caches Dict[str, Union[...]] regardless of the members' order."""
    if not isinstance(data, Mapping):
        return data
    def spec(values: Optional[Mapping]) -> Optional[Dict[str, Any]]:
        return None if values is None else {key: _spec_value(v) for key, v in values.items()}
    return OrderType(limit=spec(data.get("limit")), market=data.get("market"), trigger=spec(data.get("trigger")))


DACITE_CONFIG = DaciteConfig(
    cast=[Decimal, int],
    type_hooks={
        Decimal: lambda x: Decimal(str(x)) if x != "NaN" else None,
        OrderType: _order_type,
    }
)

# Field mappings for converting between API and our model names
API_TO_MODEL_FIELDS = {
    "orderId": "order_id",
    "coin": "symbol",
    "isBuy": "is_buy",
    "sz": "size",
    "filledSz": "filled_size",
    "avgFillPx": "average_fill_price",
    "entryPx": "entry_price",
    "liquidationPx": "liquidation_price",
    "maxTradeSzs": "max_trade_sizes",
    "szi": "size",
    "orderType": "order_type",
    "reduceOnly": "reduce_only",
    "timeInForce": "time_in_force",
    "createdAt": "created_at",
    "px": "price",
    "postOnly": "post_only"
}

MODEL_TO_API_FIELDS = {v: k for k, v in API_TO_MODEL_FIELDS.items()}

def convert_api_response(response: dict) -> dict:
    """Convert API response keys to model field names."""
    converted = {}
    for api_key, value in response.items():
        model_key = API_TO_MODEL_FIELDS.get(api_key, api_key)
        if isinstance(value, dict):
            converted[model_key] = convert_api_response(value)
        elif isinstance(value, list):
            converted[model_key] = [
                convert_api_response(item) if isinstance(item, dict) else item 
                for item in value
            ]
        else:
            converted[model_key] = value
    return converted
