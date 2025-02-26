from typing import List, TypedDict, Optional, Dict, Union, Literal
from dataclasses import dataclass
from eip712_structs import EIP712Struct, Address, Uint, Boolean
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
    
    @classmethod
    def from_key(cls, private_key: str, public_address: Optional[str] = None) -> "HyperliquidAccount":
        """Create a HyperliquidAccount from a private key.
        
        Args:
            private_key (str): The private key to use
            
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
            public_address=public_address
        )
    
    @classmethod
    def from_env(cls) -> "HyperliquidAccount":
        private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
        if not private_key:
            raise ValueError("HYPERLIQUID_PRIVATE_KEY environment variable is required")
            
        public_address = os.getenv("HYPERLIQUID_PUBLIC_ADDRESS")
        if not public_address:
            raise ValueError("HYPERLIQUID_PUBLIC_ADDRESS environment variable is required")
            
        return cls(
            private_key=private_key,
            public_address=public_address
        )
    
    def to_dict(self) -> dict:
        return {"private_key": self.private_key}
        
    def __str__(self) -> str:
        return f"HyperliquidAccount(public_address={self.public_address})"

@dataclass(slots=True, kw_only=True)
class Leverage:
    type: Literal["cross", "isolated"]
    value: Decimal

@dataclass(slots=True, kw_only=True)
class Position:
    symbol: str
    entry_price: Optional[Decimal]
    leverage: Leverage
    liquidation_price: Optional[Decimal]
    margin_used: Decimal
    max_trade_sizes: Optional[List[Decimal]] = None
    position_value: Decimal
    return_on_equity: Decimal
    size: Decimal
    unrealized_pnl: Decimal
    
    @property
    def is_long(self) -> bool:
        return self.size > 0
    
    @property
    def is_short(self) -> bool:
        return self.size < 0

@dataclass(slots=True, kw_only=True)
class AssetPosition:
    position: Position
    type: Literal["oneWay"]

@dataclass(slots=True, kw_only=True)
class MarginSummary:
    account_value: Decimal
    total_margin_used: Decimal
    total_ntl_pos: Decimal
    total_raw_usd: Decimal

@dataclass(slots=True, kw_only=True)
class UserState:
    asset_positions: List[AssetPosition]
    margin_summary: MarginSummary
    cross_margin_summary: MarginSummary
    withdrawable: Decimal

@dataclass(slots=True, kw_only=True)
class OrderType:
    limit: Optional[Dict[str, Union[Decimal, bool]]]
    market: Optional[Dict]
    trigger: Optional[Dict[str, Union[Decimal, bool, str]]]

@dataclass(slots=True, kw_only=True)
class Order:
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

# Market specifications for all pairs
MARKET_SPECS = {
    "AAVE": {"size_decimals": 2, "price_decimals": 1},
    "ACE": {"size_decimals": 2, "price_decimals": 1},
    "ADA": {"size_decimals": 0, "price_decimals": 1},
    "AI": {"size_decimals": 1, "price_decimals": 1},
    "AI16Z": {"size_decimals": 1, "price_decimals": 1},
    "AIXBT": {"size_decimals": 0, "price_decimals": 1},
    "ALGO": {"size_decimals": 0, "price_decimals": 1},
    "ALT": {"size_decimals": 0, "price_decimals": 1},
    "ANIME": {"size_decimals": 0, "price_decimals": 1},
    "APE": {"size_decimals": 1, "price_decimals": 1},
    "APT": {"size_decimals": 2, "price_decimals": 1},
    "AR": {"size_decimals": 2, "price_decimals": 1},
    "ARB": {"size_decimals": 1, "price_decimals": 1},
    "ARK": {"size_decimals": 0, "price_decimals": 1},
    "ATOM": {"size_decimals": 2, "price_decimals": 1},
    "AVAX": {"size_decimals": 2, "price_decimals": 1},
    "BADGER": {"size_decimals": 1, "price_decimals": 1},
    "BANANA": {"size_decimals": 1, "price_decimals": 1},
    "BCH": {"size_decimals": 3, "price_decimals": 1},
    "BERA": {"size_decimals": 1, "price_decimals": 1},
    "BIGTIME": {"size_decimals": 0, "price_decimals": 1},
    "BIO": {"size_decimals": 0, "price_decimals": 1},
    "BLAST": {"size_decimals": 0, "price_decimals": 1},
    "BLUR": {"size_decimals": 0, "price_decimals": 1},
    "BLZ": {"size_decimals": 0, "price_decimals": 1},
    "BNB": {"size_decimals": 3, "price_decimals": 1},
    "BNT": {"size_decimals": 0, "price_decimals": 1},
    "BOME": {"size_decimals": 0, "price_decimals": 1},
    "BRETT": {"size_decimals": 0, "price_decimals": 1},
    "BSV": {"size_decimals": 2, "price_decimals": 1},
    "BTC": {
        "size_decimals": 5,
        "price_decimals": 1,
        "tick_size": 0.1  # $0.1 minimum price increment
    },
    "CAKE": {"size_decimals": 1, "price_decimals": 1},
    "CANTO": {"size_decimals": 0, "price_decimals": 1},
    "CATI": {"size_decimals": 0, "price_decimals": 1},
    "CELO": {"size_decimals": 0, "price_decimals": 1},
    "CFX": {"size_decimals": 0, "price_decimals": 1},
    "CHILLGUY": {"size_decimals": 0, "price_decimals": 1},
    "COMP": {"size_decimals": 2, "price_decimals": 1},
    "CRV": {"size_decimals": 1, "price_decimals": 1},
    "CYBER": {"size_decimals": 1, "price_decimals": 1},
    "DOGE": {"size_decimals": 0, "price_decimals": 1},
    "DOT": {"size_decimals": 1, "price_decimals": 1},
    "DYDX": {"size_decimals": 1, "price_decimals": 1},
    "DYM": {"size_decimals": 1, "price_decimals": 1},
    "EIGEN": {"size_decimals": 2, "price_decimals": 1},
    "ENA": {"size_decimals": 0, "price_decimals": 1},
    "ENS": {"size_decimals": 2, "price_decimals": 1},
    "ETC": {"size_decimals": 2, "price_decimals": 1},
    "ETH": {
        "size_decimals": 4,
        "price_decimals": 1,
        "tick_size": 0.1
    },
    "ETHFI": {"size_decimals": 1, "price_decimals": 1},
    "FARTCOIN": {"size_decimals": 1, "price_decimals": 1},
    "FET": {"size_decimals": 0, "price_decimals": 1},
    "FIL": {"size_decimals": 1, "price_decimals": 1},
    "FRIEND": {"size_decimals": 1, "price_decimals": 1},
    "FTM": {"size_decimals": 0, "price_decimals": 1},
    "FTT": {"size_decimals": 1, "price_decimals": 1},
    "FXS": {"size_decimals": 1, "price_decimals": 1},
    "GALA": {"size_decimals": 0, "price_decimals": 1},
    "GAS": {"size_decimals": 1, "price_decimals": 1},
    "GMT": {"size_decimals": 0, "price_decimals": 1},
    "GMX": {"size_decimals": 2, "price_decimals": 1},
    "GOAT": {"size_decimals": 0, "price_decimals": 1},
    "GRASS": {"size_decimals": 1, "price_decimals": 1},
    "GRIFFAIN": {"size_decimals": 0, "price_decimals": 1},
    "HBAR": {"size_decimals": 0, "price_decimals": 1},
    "HMSTR": {"size_decimals": 0, "price_decimals": 1},
    "HPOS": {"size_decimals": 0, "price_decimals": 1},
    "HYPE": {"size_decimals": 2, "price_decimals": 1},
    "ILV": {"size_decimals": 2, "price_decimals": 1},
    "IMX": {"size_decimals": 1, "price_decimals": 1},
    "INJ": {"size_decimals": 1, "price_decimals": 1},
    "IO": {"size_decimals": 1, "price_decimals": 1},
    "IOTA": {"size_decimals": 0, "price_decimals": 1},
    "IP": {"size_decimals": 1, "price_decimals": 1},
    "JELLY": {"size_decimals": 0, "price_decimals": 1},
    "JTO": {"size_decimals": 0, "price_decimals": 1},
    "JUP": {"size_decimals": 0, "price_decimals": 1},
    "KAITO": {"size_decimals": 0, "price_decimals": 1},
    "KAS": {"size_decimals": 0, "price_decimals": 1},
    "LAYER": {"size_decimals": 0, "price_decimals": 1},
    "LDO": {"size_decimals": 1, "price_decimals": 1},
    "LINK": {"size_decimals": 1, "price_decimals": 1},
    "LISTA": {"size_decimals": 0, "price_decimals": 1},
    "LOOM": {"size_decimals": 0, "price_decimals": 1},
    "LTC": {"size_decimals": 2, "price_decimals": 1},
    "MANTA": {"size_decimals": 1, "price_decimals": 1},
    "MATIC": {"size_decimals": 1, "price_decimals": 1},
    "MAV": {"size_decimals": 0, "price_decimals": 1},
    "MAVIA": {"size_decimals": 1, "price_decimals": 1},
    "ME": {"size_decimals": 1, "price_decimals": 1},
    "MELANIA": {"size_decimals": 1, "price_decimals": 1},
    "MEME": {"size_decimals": 0, "price_decimals": 1},
    "MERL": {"size_decimals": 0, "price_decimals": 1},
    "MEW": {"size_decimals": 0, "price_decimals": 1},
    "MINA": {"size_decimals": 0, "price_decimals": 1},
    "MKR": {"size_decimals": 4, "price_decimals": 1},
    "MNT": {"size_decimals": 1, "price_decimals": 1},
    "MOODENG": {"size_decimals": 0, "price_decimals": 1},
    "MORPHO": {"size_decimals": 1, "price_decimals": 1},
    "MOVE": {"size_decimals": 0, "price_decimals": 1},
    "MYRO": {"size_decimals": 0, "price_decimals": 1},
    "NEAR": {"size_decimals": 1, "price_decimals": 1},
    "NEIROETH": {"size_decimals": 0, "price_decimals": 1},
    "NEO": {"size_decimals": 2, "price_decimals": 1},
    "NFTI": {"size_decimals": 1, "price_decimals": 1},
    "NOT": {"size_decimals": 0, "price_decimals": 1},
    "NTRN": {"size_decimals": 0, "price_decimals": 1},
    "OGN": {"size_decimals": 0, "price_decimals": 1},
    "OM": {"size_decimals": 1, "price_decimals": 1},
    "OMNI": {"size_decimals": 2, "price_decimals": 1},
    "ONDO": {"size_decimals": 0, "price_decimals": 1},
    "OP": {"size_decimals": 1, "price_decimals": 1},
    "ORBS": {"size_decimals": 0, "price_decimals": 1},
    "ORDI": {"size_decimals": 2, "price_decimals": 1},
    "OX": {"size_decimals": 0, "price_decimals": 1},
    "PANDORA": {"size_decimals": 5, "price_decimals": 1},
    "PENDLE": {"size_decimals": 0, "price_decimals": 1},
    "PENGU": {"size_decimals": 0, "price_decimals": 1},
    "PEOPLE": {"size_decimals": 0, "price_decimals": 1},
    "PIXEL": {"size_decimals": 0, "price_decimals": 1},
    "PNUT": {"size_decimals": 1, "price_decimals": 1},
    "POL": {"size_decimals": 0, "price_decimals": 1},
    "POLYX": {"size_decimals": 0, "price_decimals": 1},
    "POPCAT": {"size_decimals": 0, "price_decimals": 1},
    "PURR": {"size_decimals": 0, "price_decimals": 1},
    "PYTH": {"size_decimals": 0, "price_decimals": 1},
    "RDNT": {"size_decimals": 0, "price_decimals": 1},
    "RENDER": {"size_decimals": 1, "price_decimals": 1},
    "REQ": {"size_decimals": 0, "price_decimals": 1},
    "REZ": {"size_decimals": 0, "price_decimals": 1},
    "RLB": {"size_decimals": 0, "price_decimals": 1},
    "RNDR": {"size_decimals": 1, "price_decimals": 1},
    "RSR": {"size_decimals": 0, "price_decimals": 1},
    "RUNE": {"size_decimals": 1, "price_decimals": 1},
    "S": {"size_decimals": 0, "price_decimals": 1},
    "SAGA": {"size_decimals": 1, "price_decimals": 1},
    "SAND": {"size_decimals": 0, "price_decimals": 1},
    "SCR": {"size_decimals": 1, "price_decimals": 1},
    "SEI": {"size_decimals": 0, "price_decimals": 1},
    "SHIA": {"size_decimals": 0, "price_decimals": 1},
    "SNX": {"size_decimals": 1, "price_decimals": 1},
    "SOL": {"size_decimals": 2, "price_decimals": 1},
    "SPX": {"size_decimals": 1, "price_decimals": 1},
    "STG": {"size_decimals": 0, "price_decimals": 1},
    "STRAX": {"size_decimals": 0, "price_decimals": 1},
    "STRK": {"size_decimals": 1, "price_decimals": 1},
    "STX": {"size_decimals": 1, "price_decimals": 1},
    "SUI": {"size_decimals": 1, "price_decimals": 1},
    "SUPER": {"size_decimals": 0, "price_decimals": 1},
    "SUSHI": {"size_decimals": 1, "price_decimals": 1},
    "TAO": {"size_decimals": 3, "price_decimals": 1},
    "TIA": {"size_decimals": 1, "price_decimals": 1},
    "TNSR": {"size_decimals": 1, "price_decimals": 1},
    "TON": {"size_decimals": 1, "price_decimals": 1},
    "TRB": {"size_decimals": 2, "price_decimals": 1},
    "TRUMP": {"size_decimals": 1, "price_decimals": 1},
    "TRX": {"size_decimals": 0, "price_decimals": 1},
    "TST": {"size_decimals": 0, "price_decimals": 1},
    "TURBO": {"size_decimals": 0, "price_decimals": 1},
    "UMA": {"size_decimals": 1, "price_decimals": 1},
    "UNI": {"size_decimals": 1, "price_decimals": 1},
    "UNIBOT": {"size_decimals": 3, "price_decimals": 1},
    "USTC": {"size_decimals": 0, "price_decimals": 1},
    "USUAL": {"size_decimals": 1, "price_decimals": 1},
    "VINE": {"size_decimals": 0, "price_decimals": 1},
    "VIRTUAL": {"size_decimals": 1, "price_decimals": 1},
    "VVV": {"size_decimals": 2, "price_decimals": 1},
    "W": {"size_decimals": 1, "price_decimals": 1},
    "WIF": {"size_decimals": 0, "price_decimals": 1},
    "WLD": {"size_decimals": 1, "price_decimals": 1},
    "XAI": {"size_decimals": 1, "price_decimals": 1},
    "XLM": {"size_decimals": 0, "price_decimals": 1},
    "XRP": {"size_decimals": 0, "price_decimals": 1},
    "YGG": {"size_decimals": 0, "price_decimals": 1},
    "ZEN": {"size_decimals": 2, "price_decimals": 1},
    "ZEREBRO": {"size_decimals": 0, "price_decimals": 1},
    "ZETA": {"size_decimals": 1, "price_decimals": 1},
    "ZK": {"size_decimals": 0, "price_decimals": 1},
    "ZRO": {"size_decimals": 1, "price_decimals": 1},
    "kBONK": {"size_decimals": 0, "price_decimals": 1},
    "kDOGS": {"size_decimals": 0, "price_decimals": 1},
    "kFLOKI": {"size_decimals": 0, "price_decimals": 1},
    "kLUNC": {"size_decimals": 0, "price_decimals": 1},
    "kNEIRO": {"size_decimals": 1, "price_decimals": 1},
    "kPEPE": {"size_decimals": 0, "price_decimals": 1},
    "kSHIB": {"size_decimals": 0, "price_decimals": 1},
}

def get_current_market_specs() -> Dict[str, Dict]:
    """Get current market specifications from the API."""
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    
    info_client = Info(constants.MAINNET_API_URL, skip_ws=True)
    response = info_client.meta()
    current_specs = {}
    
    for market in response['universe']:
        current_specs[market['name']] = {
            "size_decimals": market.get('szDecimals', 3),  # Default to 3 if not found
            "price_decimals": market.get('px_dps', 1),     # Using 'px_dps' instead of 'priceDecimals'
        }
    
    return current_specs

def print_market_specs_diff(current_specs: Dict, stored_specs: Dict = MARKET_SPECS):
    """Print differences between current and stored market specifications."""
    logger = logging.getLogger("fractrade_hl_simple")
    
    all_symbols = set(current_specs.keys()) | set(stored_specs.keys())
    
    for symbol in sorted(all_symbols):
        if symbol not in stored_specs:
            logger.info(f"New market {symbol}: {current_specs[symbol]}")
            continue
            
        if symbol not in current_specs:
            logger.info(f"Removed market {symbol}")
            continue
            
        current = current_specs[symbol]
        stored = stored_specs[symbol]
        
        if current != stored:
            logger.info(f"Changed market {symbol}:")
            for key in current.keys():
                if key in stored and current[key] != stored.get(key):
                    logger.info(f"  {key}: {stored.get(key)} -> {current[key]}")