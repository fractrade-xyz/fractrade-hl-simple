from typing import List, TypedDict, Optional, Dict, Union, Literal
from dataclasses import dataclass
from eip712_structs import EIP712Struct, Address, Uint, Boolean
import os
from dotenv import load_dotenv
from decimal import Decimal
from dacite import Config as DaciteConfig

# Load environment variables from .env file
load_dotenv()

@dataclass(slots=True, kw_only=True)
class HyperliquidAccount:
    private_key: str
    env: str = "testnet"
    public_address: Optional[str] = None
    
    @classmethod
    def from_env(cls) -> "HyperliquidAccount":
        private_key = os.getenv("HYPERLIQUID_PRIVATE_KEY")
        if not private_key:
            raise ValueError("HYPERLIQUID_PRIVATE_KEY environment variable is required")
            
        env = os.getenv("HYPERLIQUID_ENV", "testnet")
        if env not in ["mainnet", "testnet"]:
            raise ValueError("HYPERLIQUID_ENV must be either 'mainnet' or 'testnet'")
            
        public_address = os.getenv("HYPERLIQUID_PUBLIC_ADDRESS")
        if not public_address:
            raise ValueError("HYPERLIQUID_PUBLIC_ADDRESS environment variable is required")
            
        return cls(
            private_key=private_key, 
            env=env,
            public_address=public_address
        )
    
    def to_dict(self) -> dict:
        return {"private_key": self.private_key}
        
    def __str__(self) -> str:
        return f"HyperliquidAccount(env={self.env}, public_address={self.public_address})"

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
    fee: Optional[Decimal] = None
    
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
    "AAVE": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ACE": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ADA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "AI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "AI16Z": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "AIXBT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ALGO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ALT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ANIME": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "APE": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "APT": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "AR": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ARB": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "ARK": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ATOM": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "AVAX": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "BADGER": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "BANANA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "BCH": {"size_decimals": 3, "price_decimals": 1, "min_size": 0.001},
    "BERA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "BIGTIME": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BIO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BLAST": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BLUR": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BLZ": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BNB": {"size_decimals": 3, "price_decimals": 1, "min_size": 0.001},
    "BNT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BOME": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BRETT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "BSV": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "BTC": {
        "size_decimals": 5,
        "price_decimals": 1,
        "min_size": 0.001,
        "tick_size": 0.1  # $0.1 minimum price increment
    },
    "CAKE": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "CANTO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "CATI": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "CELO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "CFX": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "CHILLGUY": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "COMP": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "CRV": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "CYBER": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "DOGE": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "DOT": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "DYDX": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "DYM": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "EIGEN": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ENA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ENS": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ETC": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ETH": {
        "size_decimals": 4,
        "price_decimals": 1,
        "min_size": 0.001,
        "tick_size": 0.1
    },
    "ETHFI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "FARTCOIN": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "FET": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "FIL": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "FRIEND": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "FTM": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "FTT": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "FXS": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "GALA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "GAS": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "GMT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "GMX": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "GOAT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "GRASS": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "GRIFFAIN": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "HBAR": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "HMSTR": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "HPOS": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "HYPE": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ILV": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "IMX": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "INJ": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "IO": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "IOTA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "IP": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "JELLY": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "JTO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "JUP": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "KAITO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "KAS": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "LAYER": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "LDO": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "LINK": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "LISTA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "LOOM": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "LTC": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "MANTA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "MATIC": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "MAV": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MAVIA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "ME": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "MELANIA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "MEME": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MERL": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MEW": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MINA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MKR": {"size_decimals": 4, "price_decimals": 1, "min_size": 0.001},
    "MNT": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "MOODENG": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MORPHO": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "MOVE": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "MYRO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "NEAR": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "NEIROETH": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "NEO": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "NFTI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "NOT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "NTRN": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "OGN": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "OM": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "OMNI": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ONDO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "OP": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "ORBS": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ORDI": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "OX": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PANDORA": {"size_decimals": 5, "price_decimals": 1, "min_size": 0.001},
    "PENDLE": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PENGU": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PEOPLE": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PIXEL": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PNUT": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "POL": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "POLYX": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "POPCAT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PURR": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "PYTH": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "RDNT": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "RENDER": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "REQ": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "REZ": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "RLB": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "RNDR": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "RSR": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "RUNE": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "S": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "SAGA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "SAND": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "SCR": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "SEI": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "SHIA": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "SNX": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "SOL": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "SPX": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "STG": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "STRAX": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "STRK": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "STX": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "SUI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "SUPER": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "SUSHI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "TAO": {"size_decimals": 3, "price_decimals": 1, "min_size": 0.001},
    "TIA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "TNSR": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "TON": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "TRB": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "TRUMP": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "TRX": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "TST": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "TURBO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "UMA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "UNI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "UNIBOT": {"size_decimals": 3, "price_decimals": 1, "min_size": 0.001},
    "USTC": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "USUAL": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "VINE": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "VIRTUAL": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "VVV": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "W": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "WIF": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "WLD": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "XAI": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "XLM": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "XRP": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "YGG": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ZEN": {"size_decimals": 2, "price_decimals": 1, "min_size": 0.001},
    "ZEREBRO": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ZETA": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "ZK": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "ZRO": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "kBONK": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "kDOGS": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "kFLOKI": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "kLUNC": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "kNEIRO": {"size_decimals": 1, "price_decimals": 1, "min_size": 0.001},
    "kPEPE": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
    "kSHIB": {"size_decimals": 0, "price_decimals": 1, "min_size": 0.001},
}

def get_current_market_specs(info_client) -> Dict[str, Dict]:
    """Get current market specifications from the API.
    
    Use this function to verify or update MARKET_SPECS.
    """
    response = info_client.meta()
    current_specs = {}
    
    for market in response['universe']:
        current_specs[market['name']] = {
            "size_decimals": market.get('szDecimals', 3),  # Default to 3 if not found
            "price_decimals": market.get('px_dps', 1),     # Using 'px_dps' instead of 'priceDecimals'
            "min_size": float(market.get('minSz', '0.001')),  # Default to 0.001 if not specified
        }
    
    return current_specs

def print_market_specs_diff(current_specs: Dict, stored_specs: Dict = MARKET_SPECS):
    """Print differences between current and stored market specifications."""
    all_symbols = set(current_specs.keys()) | set(stored_specs.keys())
    
    for symbol in sorted(all_symbols):
        if symbol not in stored_specs:
            print(f"New market {symbol}: {current_specs[symbol]}")
            continue
            
        if symbol not in current_specs:
            print(f"Removed market {symbol}")
            continue
            
        current = current_specs[symbol]
        stored = stored_specs[symbol]
        
        if current != stored:
            print(f"Changed market {symbol}:")
            for key in current.keys():
                if current[key] != stored.get(key):
                    print(f"  {key}: {stored.get(key)} -> {current[key]}")