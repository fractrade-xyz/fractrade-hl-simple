from typing import List, TypedDict, Optional, Dict, Union, Literal, Any
from dataclasses import dataclass
import os
from dotenv import load_dotenv
from decimal import Decimal
from dacite import Config as DaciteConfig
import eth_account
import logging

# Load environment variables from .env file
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
class UserState:
    asset_positions: List[AssetPosition]
    margin_summary: MarginSummary
    cross_margin_summary: MarginSummary
    withdrawable: Decimal
    spot_state: Optional[SpotState] = None

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

    def __repr__(self) -> str:
        return (
            f"Fill({self.symbol} {self.direction} {self.size} "
            f"@ {self.price} pnl={self.closed_pnl})"
        )

@dataclass(slots=True, kw_only=True)
class OrderType:
    """Order type specification. Only one of limit, market, or trigger is set."""
    limit: Optional[Dict[str, Union[Decimal, bool]]] = None
    market: Optional[Dict] = None
    trigger: Optional[Dict[str, Union[Decimal, bool, str]]] = None

@dataclass(slots=True, kw_only=True)
class Order:
    """An order on the exchange (open, filled, or cancelled)."""
    order_id: str
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

DACITE_CONFIG = DaciteConfig(
    cast=[Decimal, int],
    type_hooks={
        Decimal: lambda x: Decimal(str(x)) if x != "NaN" else None,
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

# Fallback market specifications, used when the API is unreachable at init.
# Updated 2026-03-18 (229 markets). Live specs are fetched from the API on client init.
MARKET_SPECS = {
    "0G": {"size_decimals": 0, "max_leverage": 3},
    "2Z": {"size_decimals": 0, "max_leverage": 3},
    "AAVE": {"size_decimals": 2, "max_leverage": 10},
    "ACE": {"size_decimals": 2, "max_leverage": 3},
    "ADA": {"size_decimals": 0, "max_leverage": 10},
    "AERO": {"size_decimals": 0, "max_leverage": 3},
    "AI": {"size_decimals": 1, "max_leverage": 3},
    "AI16Z": {"size_decimals": 1, "max_leverage": 5},
    "AIXBT": {"size_decimals": 0, "max_leverage": 3},
    "ALGO": {"size_decimals": 0, "max_leverage": 5},
    "ALT": {"size_decimals": 0, "max_leverage": 3},
    "ANIME": {"size_decimals": 0, "max_leverage": 3},
    "APE": {"size_decimals": 1, "max_leverage": 5},
    "APEX": {"size_decimals": 0, "max_leverage": 3},
    "APT": {"size_decimals": 2, "max_leverage": 10},
    "AR": {"size_decimals": 2, "max_leverage": 5},
    "ARB": {"size_decimals": 1, "max_leverage": 10},
    "ARK": {"size_decimals": 0, "max_leverage": 3},
    "ASTER": {"size_decimals": 0, "max_leverage": 5},
    "ATOM": {"size_decimals": 2, "max_leverage": 5},
    "AVAX": {"size_decimals": 2, "max_leverage": 10},
    "AVNT": {"size_decimals": 0, "max_leverage": 5},
    "AXS": {"size_decimals": 1, "max_leverage": 5},
    "AZTEC": {"size_decimals": 0, "max_leverage": 3},
    "BABY": {"size_decimals": 0, "max_leverage": 3},
    "BADGER": {"size_decimals": 1, "max_leverage": 5},
    "BANANA": {"size_decimals": 1, "max_leverage": 3},
    "BCH": {"size_decimals": 3, "max_leverage": 10},
    "BERA": {"size_decimals": 1, "max_leverage": 5},
    "BIGTIME": {"size_decimals": 0, "max_leverage": 3},
    "BIO": {"size_decimals": 0, "max_leverage": 3},
    "BLAST": {"size_decimals": 0, "max_leverage": 3},
    "BLUR": {"size_decimals": 0, "max_leverage": 3},
    "BLZ": {"size_decimals": 0, "max_leverage": 5},
    "BNB": {"size_decimals": 3, "max_leverage": 10},
    "BNT": {"size_decimals": 0, "max_leverage": 3},
    "BOME": {"size_decimals": 0, "max_leverage": 3},
    "BRETT": {"size_decimals": 0, "max_leverage": 3},
    "BSV": {"size_decimals": 2, "max_leverage": 3},
    "BTC": {"size_decimals": 5, "max_leverage": 40},
    "CAKE": {"size_decimals": 1, "max_leverage": 3},
    "CANTO": {"size_decimals": 0, "max_leverage": 5},
    "CATI": {"size_decimals": 0, "max_leverage": 3},
    "CC": {"size_decimals": 0, "max_leverage": 3},
    "CELO": {"size_decimals": 0, "max_leverage": 3},
    "CFX": {"size_decimals": 0, "max_leverage": 5},
    "CHILLGUY": {"size_decimals": 0, "max_leverage": 3},
    "COMP": {"size_decimals": 2, "max_leverage": 5},
    "CRV": {"size_decimals": 1, "max_leverage": 10},
    "CYBER": {"size_decimals": 1, "max_leverage": 3},
    "DASH": {"size_decimals": 2, "max_leverage": 5},
    "DOGE": {"size_decimals": 0, "max_leverage": 10},
    "DOOD": {"size_decimals": 0, "max_leverage": 3},
    "DOT": {"size_decimals": 1, "max_leverage": 10},
    "DYDX": {"size_decimals": 1, "max_leverage": 5},
    "DYM": {"size_decimals": 1, "max_leverage": 3},
    "EIGEN": {"size_decimals": 2, "max_leverage": 5},
    "ENA": {"size_decimals": 0, "max_leverage": 10},
    "ENS": {"size_decimals": 2, "max_leverage": 5},
    "ETC": {"size_decimals": 2, "max_leverage": 5},
    "ETH": {"size_decimals": 4, "max_leverage": 25},
    "ETHFI": {"size_decimals": 1, "max_leverage": 5},
    "FARTCOIN": {"size_decimals": 1, "max_leverage": 10},
    "FET": {"size_decimals": 0, "max_leverage": 5},
    "FIL": {"size_decimals": 1, "max_leverage": 5},
    "FOGO": {"size_decimals": 0, "max_leverage": 3},
    "FRIEND": {"size_decimals": 1, "max_leverage": 3},
    "FTM": {"size_decimals": 0, "max_leverage": 10},
    "FTT": {"size_decimals": 1, "max_leverage": 3},
    "FXS": {"size_decimals": 1, "max_leverage": 5},
    "GALA": {"size_decimals": 0, "max_leverage": 3},
    "GAS": {"size_decimals": 1, "max_leverage": 3},
    "GMT": {"size_decimals": 0, "max_leverage": 3},
    "GMX": {"size_decimals": 2, "max_leverage": 3},
    "GOAT": {"size_decimals": 0, "max_leverage": 3},
    "GRASS": {"size_decimals": 1, "max_leverage": 3},
    "GRIFFAIN": {"size_decimals": 0, "max_leverage": 3},
    "HBAR": {"size_decimals": 0, "max_leverage": 5},
    "HEMI": {"size_decimals": 0, "max_leverage": 3},
    "HMSTR": {"size_decimals": 0, "max_leverage": 3},
    "HPOS": {"size_decimals": 0, "max_leverage": 3},
    "HYPE": {"size_decimals": 2, "max_leverage": 10},
    "HYPER": {"size_decimals": 0, "max_leverage": 3},
    "ICP": {"size_decimals": 1, "max_leverage": 5},
    "ILV": {"size_decimals": 2, "max_leverage": 3},
    "IMX": {"size_decimals": 1, "max_leverage": 5},
    "INIT": {"size_decimals": 0, "max_leverage": 3},
    "INJ": {"size_decimals": 1, "max_leverage": 10},
    "IO": {"size_decimals": 1, "max_leverage": 3},
    "IOTA": {"size_decimals": 0, "max_leverage": 3},
    "IP": {"size_decimals": 1, "max_leverage": 3},
    "JELLY": {"size_decimals": 0, "max_leverage": 3},
    "JTO": {"size_decimals": 0, "max_leverage": 5},
    "JUP": {"size_decimals": 0, "max_leverage": 10},
    "KAITO": {"size_decimals": 0, "max_leverage": 5},
    "KAS": {"size_decimals": 0, "max_leverage": 3},
    "LAUNCHCOIN": {"size_decimals": 0, "max_leverage": 3},
    "LAYER": {"size_decimals": 0, "max_leverage": 3},
    "LDO": {"size_decimals": 1, "max_leverage": 10},
    "LINEA": {"size_decimals": 0, "max_leverage": 3},
    "LINK": {"size_decimals": 1, "max_leverage": 10},
    "LISTA": {"size_decimals": 0, "max_leverage": 3},
    "LIT": {"size_decimals": 0, "max_leverage": 5},
    "LOOM": {"size_decimals": 0, "max_leverage": 10},
    "LTC": {"size_decimals": 2, "max_leverage": 10},
    "MANTA": {"size_decimals": 1, "max_leverage": 3},
    "MATIC": {"size_decimals": 1, "max_leverage": 20},
    "MAV": {"size_decimals": 0, "max_leverage": 3},
    "MAVIA": {"size_decimals": 1, "max_leverage": 3},
    "ME": {"size_decimals": 1, "max_leverage": 3},
    "MEGA": {"size_decimals": 0, "max_leverage": 3},
    "MELANIA": {"size_decimals": 1, "max_leverage": 3},
    "MEME": {"size_decimals": 0, "max_leverage": 3},
    "MERL": {"size_decimals": 0, "max_leverage": 3},
    "MET": {"size_decimals": 0, "max_leverage": 3},
    "MEW": {"size_decimals": 0, "max_leverage": 3},
    "MINA": {"size_decimals": 0, "max_leverage": 3},
    "MKR": {"size_decimals": 4, "max_leverage": 10},
    "MNT": {"size_decimals": 1, "max_leverage": 5},
    "MON": {"size_decimals": 0, "max_leverage": 5},
    "MOODENG": {"size_decimals": 0, "max_leverage": 3},
    "MORPHO": {"size_decimals": 1, "max_leverage": 5},
    "MOVE": {"size_decimals": 0, "max_leverage": 3},
    "MYRO": {"size_decimals": 0, "max_leverage": 3},
    "NEAR": {"size_decimals": 1, "max_leverage": 10},
    "NEIROETH": {"size_decimals": 0, "max_leverage": 5},
    "NEO": {"size_decimals": 2, "max_leverage": 5},
    "NFTI": {"size_decimals": 1, "max_leverage": 3},
    "NIL": {"size_decimals": 0, "max_leverage": 3},
    "NOT": {"size_decimals": 0, "max_leverage": 3},
    "NTRN": {"size_decimals": 0, "max_leverage": 3},
    "NXPC": {"size_decimals": 0, "max_leverage": 3},
    "OGN": {"size_decimals": 0, "max_leverage": 3},
    "OM": {"size_decimals": 1, "max_leverage": 3},
    "OMNI": {"size_decimals": 2, "max_leverage": 3},
    "ONDO": {"size_decimals": 0, "max_leverage": 10},
    "OP": {"size_decimals": 1, "max_leverage": 10},
    "ORBS": {"size_decimals": 0, "max_leverage": 3},
    "ORDI": {"size_decimals": 2, "max_leverage": 3},
    "OX": {"size_decimals": 0, "max_leverage": 3},
    "PANDORA": {"size_decimals": 5, "max_leverage": 3},
    "PAXG": {"size_decimals": 3, "max_leverage": 10},
    "PENDLE": {"size_decimals": 0, "max_leverage": 5},
    "PENGU": {"size_decimals": 0, "max_leverage": 5},
    "PEOPLE": {"size_decimals": 0, "max_leverage": 3},
    "PIXEL": {"size_decimals": 0, "max_leverage": 3},
    "PNUT": {"size_decimals": 1, "max_leverage": 3},
    "POL": {"size_decimals": 0, "max_leverage": 5},
    "POLYX": {"size_decimals": 0, "max_leverage": 3},
    "POPCAT": {"size_decimals": 0, "max_leverage": 3},
    "PROMPT": {"size_decimals": 0, "max_leverage": 3},
    "PROVE": {"size_decimals": 0, "max_leverage": 3},
    "PUMP": {"size_decimals": 0, "max_leverage": 10},
    "PURR": {"size_decimals": 0, "max_leverage": 3},
    "PYTH": {"size_decimals": 0, "max_leverage": 5},
    "RDNT": {"size_decimals": 0, "max_leverage": 5},
    "RENDER": {"size_decimals": 1, "max_leverage": 5},
    "REQ": {"size_decimals": 0, "max_leverage": 3},
    "RESOLV": {"size_decimals": 0, "max_leverage": 3},
    "REZ": {"size_decimals": 0, "max_leverage": 3},
    "RLB": {"size_decimals": 0, "max_leverage": 3},
    "RNDR": {"size_decimals": 1, "max_leverage": 20},
    "RSR": {"size_decimals": 0, "max_leverage": 3},
    "RUNE": {"size_decimals": 1, "max_leverage": 5},
    "S": {"size_decimals": 0, "max_leverage": 5},
    "SAGA": {"size_decimals": 1, "max_leverage": 3},
    "SAND": {"size_decimals": 0, "max_leverage": 5},
    "SCR": {"size_decimals": 1, "max_leverage": 3},
    "SEI": {"size_decimals": 0, "max_leverage": 10},
    "SHIA": {"size_decimals": 0, "max_leverage": 3},
    "SKR": {"size_decimals": 0, "max_leverage": 3},
    "SKY": {"size_decimals": 0, "max_leverage": 3},
    "SNX": {"size_decimals": 1, "max_leverage": 3},
    "SOL": {"size_decimals": 2, "max_leverage": 20},
    "SOPH": {"size_decimals": 0, "max_leverage": 3},
    "SPX": {"size_decimals": 1, "max_leverage": 5},
    "STABLE": {"size_decimals": 0, "max_leverage": 3},
    "STBL": {"size_decimals": 0, "max_leverage": 3},
    "STG": {"size_decimals": 0, "max_leverage": 3},
    "STRAX": {"size_decimals": 0, "max_leverage": 10},
    "STRK": {"size_decimals": 1, "max_leverage": 5},
    "STX": {"size_decimals": 1, "max_leverage": 5},
    "SUI": {"size_decimals": 1, "max_leverage": 10},
    "SUPER": {"size_decimals": 0, "max_leverage": 3},
    "SUSHI": {"size_decimals": 1, "max_leverage": 3},
    "SYRUP": {"size_decimals": 0, "max_leverage": 3},
    "TAO": {"size_decimals": 3, "max_leverage": 5},
    "TIA": {"size_decimals": 1, "max_leverage": 10},
    "TNSR": {"size_decimals": 1, "max_leverage": 3},
    "TON": {"size_decimals": 1, "max_leverage": 10},
    "TRB": {"size_decimals": 2, "max_leverage": 3},
    "TRUMP": {"size_decimals": 1, "max_leverage": 10},
    "TRX": {"size_decimals": 0, "max_leverage": 10},
    "TST": {"size_decimals": 0, "max_leverage": 3},
    "TURBO": {"size_decimals": 0, "max_leverage": 3},
    "UMA": {"size_decimals": 1, "max_leverage": 3},
    "UNI": {"size_decimals": 1, "max_leverage": 10},
    "UNIBOT": {"size_decimals": 3, "max_leverage": 3},
    "USTC": {"size_decimals": 0, "max_leverage": 3},
    "USUAL": {"size_decimals": 1, "max_leverage": 3},
    "VINE": {"size_decimals": 0, "max_leverage": 3},
    "VIRTUAL": {"size_decimals": 1, "max_leverage": 5},
    "VVV": {"size_decimals": 2, "max_leverage": 3},
    "W": {"size_decimals": 1, "max_leverage": 5},
    "WCT": {"size_decimals": 0, "max_leverage": 3},
    "WIF": {"size_decimals": 0, "max_leverage": 5},
    "WLD": {"size_decimals": 1, "max_leverage": 10},
    "WLFI": {"size_decimals": 0, "max_leverage": 5},
    "XAI": {"size_decimals": 1, "max_leverage": 3},
    "XLM": {"size_decimals": 0, "max_leverage": 5},
    "XMR": {"size_decimals": 3, "max_leverage": 5},
    "XPL": {"size_decimals": 0, "max_leverage": 10},
    "XRP": {"size_decimals": 0, "max_leverage": 20},
    "YGG": {"size_decimals": 0, "max_leverage": 3},
    "YZY": {"size_decimals": 0, "max_leverage": 3},
    "ZEC": {"size_decimals": 2, "max_leverage": 10},
    "ZEN": {"size_decimals": 2, "max_leverage": 5},
    "ZEREBRO": {"size_decimals": 0, "max_leverage": 3},
    "ZETA": {"size_decimals": 1, "max_leverage": 3},
    "ZK": {"size_decimals": 0, "max_leverage": 5},
    "ZORA": {"size_decimals": 0, "max_leverage": 3},
    "ZRO": {"size_decimals": 1, "max_leverage": 5},
    "kBONK": {"size_decimals": 0, "max_leverage": 10},
    "kDOGS": {"size_decimals": 0, "max_leverage": 3},
    "kFLOKI": {"size_decimals": 0, "max_leverage": 5},
    "kLUNC": {"size_decimals": 0, "max_leverage": 3},
    "kNEIRO": {"size_decimals": 1, "max_leverage": 3},
    "kPEPE": {"size_decimals": 0, "max_leverage": 10},
    "kSHIB": {"size_decimals": 0, "max_leverage": 10},
}