import logging
import math
import operator
import os
import re
import secrets
import threading
import time
import warnings
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Union

import eth_account
import requests
from dacite import from_dict
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info as _SdkInfo
from hyperliquid.utils import constants
from hyperliquid.utils.error import ClientError, ServerError
from hyperliquid.utils.types import Cloid

from .exceptions import (
    AddressRequiredException,
    ActionOutcomeUnknownException,
    OrderException,
    OrdersNotCanceledException,
    OrderOutcomeUnknownException,
    ActionRejectedException,
    AuthenticationRequiredException,
    ClosePositionsIncompleteException,
    OrderRejectedException,
    PositionNotClosedException,
    MakerOrderIncompleteException,
    PositionNotFoundException,
    ProtectionOrderException,
    ProtectionReplacementIncompleteException,
    RateLimitException,
    ServerErrorException,
    UnauthorizedException,
    UnsupportedAccountModeException,
    ConfigurationException,
    ClientErrorException,
    HyperliquidException,
    InsufficientMarginException,
    InvalidArgumentException,
    NetworkException,
    TimeoutException,
    UnknownSymbolException,
)
from .models import (
    DACITE_CONFIG,
    AccountSummary,
    CollateralBalance,
    DexAccount,
    FeeRates,
    FillHistory,
    FundingPayment,
    coin_market,
    HyperliquidAccount,
    Leverage,
    Market,
    MarginSummary,
    SpotMarket,
    load_env_file,
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


class _SignerState:
    """Locks and last nonce of one signing key, shared by every client in the process.

    `lock` serializes single signed actions; `operation_lock` serializes multi-step
    operations (stop loss / take profit replacement) and is held around `lock`, never inside.
    """

    __slots__ = ("lock", "operation_lock", "last_nonce_ms")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.operation_lock = threading.RLock()
        self.last_nonce_ms = 0


_signers: Dict[str, _SignerState] = {}
_signers_lock = threading.Lock()


def _signer_state(address: str) -> _SignerState:
    with _signers_lock:
        return _signers.setdefault(str(address).lower(), _SignerState())


def _round_down(value: float, decimals: int) -> float:
    """`value` rounded toward zero to `decimals` decimals, so an order is never larger than
    requested (float round() can round up)."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN))


_MARGIN_MODES = {"noCross": "no_cross", "strictIsolated": "strict_isolated"}


def _margin_mode(market: Dict[str, Any]) -> str:
    """Margin mode of a perp market: "cross" (cross allowed), "no_cross" or "strict_isolated"
    (isolated only), or "unknown" for a value this library does not know.

    Markets without `marginMode` allow cross margin, unless the older `onlyIsolated` is set.
    """
    mode = market.get("marginMode")
    if mode is None:
        return "no_cross" if market.get("onlyIsolated") else "cross"
    return _MARGIN_MODES.get(mode, "unknown")


# Info `userAbstraction` values. "default" is undocumented; the account checked
# with it (2026-11) kept its collateral in the perp account, as standard mode does.
_ACCOUNT_MODES = {
    "disabled": "standard",
    "default": "standard",
    "unifiedAccount": "unified",
    "portfolioMargin": "portfolio_margin",
}

# The API URLs a client accepts, with the network each one is. The SDK signs for
# mainnet only when its base_url is exactly MAINNET_API_URL and for testnet otherwise, so any
# other URL, even with a trailing slash, would make mainnet reject every action.
_NETWORKS = {
    constants.MAINNET_API_URL: "mainnet",
    constants.TESTNET_API_URL: "testnet",
    constants.LOCAL_API_URL: None,
}
_NETWORK_URLS = {"mainnet": constants.MAINNET_API_URL, "testnet": constants.TESTNET_API_URL}

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_INSUFFICIENT_MARGIN = "Insufficient margin to place order."  # PerpMargin, API docs "Error responses"
_POST_ONLY_CROSSED = "Post only order would have immediately matched"  # BadAloPx, followed by the bbo
# The error texts of rejected order statuses (info endpoint docs) that callers tell apart: an
# order recovered by its cloid after a lost answer is reported with the same text.
_REJECTION_TEXTS = {"perpMarginRejected": _INSUFFICIENT_MARGIN, "badAloPxRejected": _POST_ONLY_CROSSED}
_PRIVATE_KEY = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
_CLOID = re.compile(r"0x[0-9a-fA-F]{32}")  # a 128 bit hex string (exchange endpoint docs)
_DIGITS = re.compile(r"[0-9]+")


def _perp_dexs(perp_dexs: Any, extended_universe: bool) -> List[str]:
    """The perp dexes of a client, as the SDK's Info takes them: the default dex ("") first,
    then the HIP-3 dexes asked for, each once."""
    if perp_dexs is None:
        return ["", "xyz"] if extended_universe else [""]
    if extended_universe:
        raise InvalidArgumentException("pass extended_universe=True or perp_dexs, not both")
    if isinstance(perp_dexs, (str, bytes)) or not isinstance(perp_dexs, (list, tuple)) \
            or not all(isinstance(dex, str) for dex in perp_dexs):
        raise InvalidArgumentException(f"perp_dexs must be a list of dex names such as ['xyz'], got {perp_dexs!r}")
    return list(dict.fromkeys(["", *perp_dexs]))


def _sdk_dex(coin: str) -> str:
    """The perp dex of a market name, as the SDK finds it: "xyz" for xyz:NVDA, "" otherwise."""
    return coin.split(":")[0] if ":" in coin else ""


def _order_id(order_id: Any) -> int:
    """An order id (oid) as the SDK sends it, a number: an integer of any type (int, numpy's),
    or the text of one. A float is refused: int(10.9) would be another order."""
    if isinstance(order_id, str):
        if _DIGITS.fullmatch(order_id.strip()):
            return int(order_id)
    elif not isinstance(order_id, bool):
        try:
            oid = operator.index(order_id)  # integer types only, no float or Decimal
        except TypeError:
            pass
        else:
            if oid >= 0:
                return oid
    raise InvalidArgumentException(f"order_id must be a whole number, got {order_id!r}")


MIN_ORDER_USD = Decimal(10)  # Hyperliquid's minimum order value
_TIMES_IN_FORCE = ("Gtc", "Ioc", "Alo")  # of a limit order (exchange endpoint docs)


def _protection_legs(kind: str, price: Optional[float],
                     levels: Optional[Sequence[Tuple[float, float]]]) -> List[Tuple[str, str, float, Decimal]]:
    """(name, kind, price, fraction) per stop loss or take profit of an open_*_position call:
    the single price (the whole position, named `kind`) or the list (named kind_1, kind_2,
    ...). Raises InvalidArgumentException for both, a price that is not a positive number, a
    fraction outside (0, 1], or fractions adding up to more than 1."""
    if price is not None and levels is not None:
        raise InvalidArgumentException(f"pass {kind}_price or {kind}s, not both")
    if price is not None:
        if not _positive_number(price):
            raise InvalidArgumentException(f"{kind}_price must be a positive number, got {price!r}")
        return [(kind, kind, price, Decimal(1))]
    if levels is None:
        return []
    legs: List[Tuple[str, str, float, Decimal]] = []
    for number, level in enumerate(levels, start=1):
        try:
            level_price, fraction_value = level
        except (TypeError, ValueError):
            raise InvalidArgumentException(f"{kind}s: each is (price, fraction), got {level!r}") from None
        if not _positive_number(level_price):
            raise InvalidArgumentException(f"{kind}s: price must be a positive number, got {level_price!r}")
        fraction = Decimal(str(fraction_value)) if isinstance(fraction_value, (int, float)) and \
            not isinstance(fraction_value, bool) else Decimal("NaN")
        if not fraction.is_finite() or not 0 < fraction <= 1:
            raise InvalidArgumentException(f"{kind}s: fraction must be above 0 and at most 1, got {fraction_value!r}")
        legs.append((f"{kind}_{number}", kind, level_price, fraction))
    if not legs:
        raise InvalidArgumentException(f"{kind}s is empty: pass at least one, or none")
    if sum(fraction for _, _, _, fraction in legs) > 1:
        raise InvalidArgumentException(f"{kind}s: the fractions add up to more than 1")
    return legs


def split_size(size: Decimal, legs: Sequence[Tuple[str, str, Any, Decimal]], size_decimals: int,
               is_long: bool) -> List[Decimal]:
    """The size of each leg (name, kind, price, fraction) of a long or short position: its
    fraction of `size`, rounded down to the lot size; per kind, the leg nearest the entry (the
    first the price reaches: a long's highest stop loss and lowest take profit) takes what
    rounding left, so the legs of a kind cover their share (their fractions' sum of `size`,
    rounded down) exactly. Given to the nearest stop, the cheapest per unit, the loss if
    every stop fills (moves and fees, and slippage estimated for all the stops together)
    only grows with `size`: a partial fill never loses more at its stops than the full size."""
    step = Decimal(1).scaleb(-size_decimals)
    sizes = [(size * fraction).quantize(step, ROUND_DOWN) for _, _, _, fraction in legs]
    for kind in ("stop_loss", "take_profit"):
        indexes = [i for i, leg in enumerate(legs) if leg[1] == kind]
        if indexes:
            share = (size * sum(legs[i][3] for i in indexes)).quantize(step, ROUND_DOWN)
            highest = (kind == "stop_loss") == is_long  # a long's stops, a short's take profits: below the price
            nearest = max(indexes, key=lambda i: Decimal(str(legs[i][2]))) if highest else \
                min(indexes, key=lambda i: Decimal(str(legs[i][2])))
            sizes[nearest] += share - sum(sizes[i] for i in indexes)
    return sizes


def _average_price(parts: Sequence[Tuple[Decimal, Optional[Decimal]]]) -> Optional[Decimal]:
    """Average price of (size, price) parts; None if a price is unknown or nothing filled."""
    qty = sum((q for q, _ in parts), Decimal(0))
    if not qty or any(px is None for _, px in parts):
        return None
    return sum(q * px for q, px in parts) / qty


def _missing(legs: Sequence[Tuple[str, str, float, Decimal]], sizes: Sequence[Decimal]) -> List[Dict[str, Any]]:
    """The legs not placed, for ProtectionOrderException: name, kind, price, size."""
    return [{"leg": name, "kind": kind, "price": price, "size": leg_size.normalize(), "outcome_unknown": False,
             "cloid": None} for (name, kind, price, _), leg_size in zip(legs, sizes) if leg_size > 0]


def _positive_number(value: Any) -> bool:
    """True for a finite number above 0 (int, float, Decimal, numpy numbers); not for text or bool."""
    if isinstance(value, (bool, str, bytes)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def _cloid(cloid: Any) -> Cloid:
    """A client order id as the SDK sends it, from its text: "0x" and 32 hex digits. The SDK's
    Cloid checks only the prefix and the length, not that it is hex."""
    if not (isinstance(cloid, str) and _CLOID.fullmatch(cloid)):
        raise InvalidArgumentException(f"cloid must be 0x followed by 32 hex digits, got {cloid!r}")
    return Cloid(cloid)


def _order_cloid(cloid: Any) -> Cloid:
    """The client order id to place an order with: the caller's, else a new random one."""
    return Cloid("0x" + secrets.token_hex(16)) if cloid is None else _cloid(cloid)


def _is_unparsed(response: Any) -> bool:
    """True for the dict the SDK returns instead of raising when a response body is not JSON."""
    return (isinstance(response, dict) and set(response) == {"error"}
            and str(response["error"]).startswith("Could not parse JSON"))


def _spot_symbol(base: str, quote: str) -> str:
    """The symbol the spot methods use for a pair: the base token for a pair quoted in USDC
    ("PURR"), else "BASE/QUOTE" ("HYPE/USDT0")."""
    return base if quote == "USDC" else f"{base}/{quote}"


def _decimal(value: Any) -> Optional[Decimal]:
    """A number Hyperliquid sends as a string, as Decimal; None stays None."""
    return None if value is None else Decimal(str(value))


_NOT_JSON = "Hyperliquid answered with a body that is not JSON (e.g. an HTML error page)"


class Info(_SdkInfo):
    """The SDK's Info, raising ServerErrorException where the SDK would return its
    {"error": "Could not parse JSON: ..."} dict for a response body that is not JSON. Every
    request of the SDK's Info goes through post(), its own startup requests included, so the
    SDK's parsing never sees that dict. The body is not kept: it is no message.

    `proxies` are set on the session before its first request: the SDK's Info already sends
    requests while it is created, before a caller could set them.

    A dex of perp_dexs that the network does not have raises InvalidArgumentException: the
    SDK fails with a bare KeyError while it is created. Only a dex missing from the network's
    list of dexes (the perpDexs answer the SDK reads then) is reported so; any other KeyError
    is raised as it is.

    `startup_metadata` keeps the default dex's meta ("meta") and the spot meta ("spotMeta")
    as the SDK fetched them while it was created, so they need no second request."""

    def __init__(self, *args: Any, proxies: Optional[Dict[str, str]] = None, **kwargs: Any) -> None:
        self._pending_proxies = proxies  # read by post(), which the SDK's __init__ already calls
        self._listed_dexes: Optional[set] = None  # set by post() from the perpDexs answer
        self.startup_metadata: Dict[str, Any] = {}  # set by post() while the SDK's __init__ runs
        self._starting = True
        try:
            super().__init__(*args, **kwargs)
        except KeyError as e:
            missing = e.args[0] if len(e.args) == 1 else None
            if not (self._listed_dexes is not None and missing in (kwargs.get("perp_dexs") or ())
                    and missing not in self._listed_dexes):
                raise
            network = _NETWORKS.get(self.base_url) or self.base_url
            raise InvalidArgumentException(
                f"{missing!r} is not a perp dex on {network}; get_perp_dexs() lists them") from e
        finally:
            self._starting = False

    def post(self, url_path: str, payload: Any = None) -> Any:
        proxies = self.__dict__.pop("_pending_proxies", None)
        if proxies:
            self.session.proxies.update(proxies)
        response = super().post(url_path, payload)
        if _is_unparsed(response):
            raise ServerErrorException(_NOT_JSON, 200)
        kind = payload.get("type") if isinstance(payload, dict) else None
        if kind == "perpDexs" and isinstance(response, list):
            self._listed_dexes = {dex.get("name") for dex in response if isinstance(dex, dict)}
        elif (self.__dict__.get("_starting") and isinstance(response, dict)
              and (kind == "spotMeta" or (kind == "meta" and payload.get("dex", "") == ""))):
            self.startup_metadata[kind] = response
        return response


def _collateral(balance: SpotTokenBalance) -> CollateralBalance:
    """A spot balance of get_spot_balance as collateral; a price of 0 means unpriced there."""
    market = "outcome" if coin_market(balance.token) == "outcome" else "spot"
    priced = market == "spot" and balance.price > 0
    return CollateralBalance(
        token=balance.token, market=market, total=balance.amount, hold=balance.hold,
        available=balance.amount - balance.hold, price=balance.price if priced else None,
        usd_value=balance.usd_value if priced else None)


def _library_error(error: Exception) -> HyperliquidException:
    """The library exception for a failure requests or the SDK raised."""
    if isinstance(error, HyperliquidException):
        return error
    if isinstance(error, requests.Timeout):
        return TimeoutException(f"Hyperliquid did not answer in time: {error}")
    if isinstance(error, ServerError):
        return ServerErrorException(f"Hyperliquid server error (HTTP {error.status_code})", error.status_code)
    if isinstance(error, ClientError):
        return ClientErrorException(
            f"Hyperliquid rejected the request (HTTP {error.status_code}): {error.error_message}",
            error.status_code, error.error_code, error.error_message, error.header, error.error_data)
    return NetworkException(f"Hyperliquid could not be reached: {error}")


class HyperliquidClient:
    # Metadata cached for the next client of the same network and dex list (_cached_key).
    _cached_key: Optional[tuple] = None
    _cached_market_specs: Optional[Dict[str, Dict]] = None
    _cached_market_specs_at: float = 0
    _cached_meta: Optional[Any] = None
    _cached_spot_meta: Optional[Any] = None
    _cache_lock = threading.Lock()  # read and publish the _cached_* fields together
    _CACHE_TTL: float = 86400  # 24 hours

    def __init__(
        self,
        account: Optional[HyperliquidAccount] = None,
        env: Optional[str] = None,
        default_slippage: float = 0.05,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        cache_market_specs: bool = True,
        extended_universe: bool = False,
        proxy: Optional[Dict[str, str]] = None,
        request_timeout: float = 10.0,
        *,
        base_url: Optional[str] = None,
        trust_env: bool = True,
        perp_dexs: Optional[List[str]] = None,
    ):
        """Initialize HyperliquidClient.

        Args:
            account (Optional[HyperliquidAccount]): Account credentials. If None, tries to load from environment.
            env (Optional[str]): Deprecated, use base_url. "mainnet" or "testnet".
            default_slippage (float): Default slippage for market orders (0.05 = 5%). Defaults to 0.05.
            max_retries (int): Maximum number of retries for transient failures. 0 to disable. Defaults to 3.
            retry_delay (float): Base delay in seconds between retries (exponential backoff). Defaults to 1.0.
            cache_market_specs (bool): If True (default), reuses cached market specs across instances
                within the same process. Cache expires after 24 hours. Set to False to always fetch
                fresh specs on init.
            extended_universe (bool): If True, enables trading of stocks, commodities, indices, and forex
                (xyz: symbols like xyz:TSLA, xyz:GOLD). Off by default to avoid extra API overhead.
                The same as perp_dexs=["xyz"].
            request_timeout (float): Seconds to wait for each HTTP request. Defaults to 10.
            base_url (Optional[str]): The API to use, as in the Hyperliquid SDK: one of the
                constants MAINNET_API_URL, TESTNET_API_URL or LOCAL_API_URL from
                hyperliquid.utils.constants. If None, the network named by the HYPERLIQUID_ENV
                environment variable ("mainnet" or "testnet"), else mainnet.
            perp_dexs (Optional[List[str]]): The HIP-3 dexes (builder-deployed perps) this client
                reads and trades besides the default dex, as the SDK's Info takes them, e.g.
                ["xyz", "para"]; their markets are named with the dex as prefix (para:TOTAL2).
                The default dex is always included. Each dex adds requests to creating the
                client and to every read of positions, of all open orders or of all prices
                (a symbol's price and open orders read only its dex). get_perp_dexs()
                lists the dexes of the network. Not together with extended_universe=True.
            trust_env (bool): Like requests' Session.trust_env, for this library's settings.
                True (default): settings not passed are read from the environment and a .env
                file: the credentials when account is None, the network when base_url is None.
                False: none of them is read; without account the client has no key, and reads
                take the wallet as address=. HTTP settings such as proxy variables are still
                applied by requests itself; pass proxy= to set a proxy explicitly.

        Raises:
            ValueError: If base_url is not one of those constants, env is not 'mainnet' or
                'testnet', both are passed, or another parameter is out of range; a dex of
                perp_dexs that the network does not have (InvalidArgumentException)
        """
        # Validate parameters
        if env is not None:
            warnings.warn("env is deprecated; pass base_url=constants.MAINNET_API_URL or "
                          "constants.TESTNET_API_URL (from hyperliquid.utils import constants)",
                          DeprecationWarning, stacklevel=2)
            if base_url is not None:
                raise InvalidArgumentException("pass base_url or env, not both")
        if trust_env and ((base_url is None and env is None) or account is None):
            load_env_file()  # configured from the environment: honour a .env file, as before
        if base_url is None:
            if env is None:
                env = (os.environ.get("HYPERLIQUID_ENV") if trust_env else None) or "mainnet"
                env = env.strip().lower()
            if env not in _NETWORK_URLS:
                raise InvalidArgumentException("env and HYPERLIQUID_ENV must be either 'mainnet' or 'testnet'")
            base_url = _NETWORK_URLS[env]
        elif not isinstance(base_url, str) or base_url not in _NETWORKS:
            raise InvalidArgumentException(
                f"base_url must be one of the constants MAINNET_API_URL ({constants.MAINNET_API_URL}), "
                f"TESTNET_API_URL ({constants.TESTNET_API_URL}) or LOCAL_API_URL "
                f"({constants.LOCAL_API_URL}) from hyperliquid.utils.constants, got {base_url!r}; "
                "with any other URL the SDK would sign for testnet")
        if not 0 < default_slippage <= 0.5:
            raise InvalidArgumentException("default_slippage must be between 0 (exclusive) and 0.5 (inclusive)")
        if not 0 < request_timeout <= 120:
            raise InvalidArgumentException("request_timeout must be between 0 (exclusive) and 120 seconds")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise InvalidArgumentException(f"max_retries must be a whole number of at least 0, got {max_retries!r}")
        if isinstance(retry_delay, bool) or not (_positive_number(retry_delay) or retry_delay == 0):
            raise InvalidArgumentException(f"retry_delay must be a number of seconds of at least 0, got {retry_delay!r}")
        self.request_timeout = request_timeout

        # Set up environment
        self.base_url = base_url
        self.env = _NETWORKS[base_url]  # "mainnet", "testnet", or None for a local node
        self.default_slippage = default_slippage
        self.max_retries = max_retries
        self.retry_delay = float(retry_delay)  # time.sleep takes no Decimal
        self.perp_dexs = _perp_dexs(perp_dexs, extended_universe)
        self._proxy = proxy

        # Initialize market specs. Cached metadata is reused only by a client for the same
        # network and dex list: the SDK derives asset ids from it, so metadata of another
        # network or dex list would send orders to the wrong market.
        self._cache_key = (self.base_url, tuple(self.perp_dexs))
        with HyperliquidClient._cache_lock:
            cached = (HyperliquidClient._cached_key, HyperliquidClient._cached_meta,
                      HyperliquidClient._cached_spot_meta, HyperliquidClient._cached_market_specs,
                      HyperliquidClient._cached_market_specs_at)
        cached_key, cached_meta, cached_spot_meta, cached_specs, cached_specs_at = cached
        cache_hit = (
            cache_market_specs
            and cached_key == self._cache_key
            and cached_specs is not None
            and cached_meta is not None
            and time.time() - cached_specs_at < self._CACHE_TTL
        )
        if cache_hit:
            self._meta, self._spot_meta = cached_meta, cached_spot_meta
            self.info = self._make_info(meta=self._meta, spot_meta=self._spot_meta)
            self.market_specs, self._market_specs_fetched_at = cached_specs, cached_specs_at
        else:
            self.info = self._new_info()
            self._meta, self._spot_meta = self._startup_metadata(self.info)
            # As in the SDK, a client is not created without its markets: a failure (after the
            # retries) is raised as it is.
            self.market_specs = self._fetch_market_specs()
            self._market_specs_fetched_at = time.time()
            with HyperliquidClient._cache_lock:
                HyperliquidClient._cached_key = self._cache_key
                HyperliquidClient._cached_meta = self._meta
                HyperliquidClient._cached_spot_meta = self._spot_meta
                HyperliquidClient._cached_market_specs = self.market_specs
                HyperliquidClient._cached_market_specs_at = self._market_specs_fetched_at

        # A key that is given must work, as in the SDK (whose signing account cannot be created
        # from a bad key): an unusable account raises here, never a silent read-only client.
        # Without a key in the environment, the client reads public data only.
        if account is not None:
            self._setup_authenticated_client(account)
        elif trust_env and os.environ.get("HYPERLIQUID_PRIVATE_KEY"):
            try:
                self._setup_authenticated_client(HyperliquidAccount.from_env())
            except ValueError as e:  # the account's problem, or from_env's missing address
                raise ConfigurationException(
                    f"HYPERLIQUID_PRIVATE_KEY is set, but the account in the environment is unusable: {e}") from None

    def _setup_authenticated_client(self, account: HyperliquidAccount):
        """Set up the account the client signs with.

        Raises:
            InvalidArgumentException: A ValueError, for an account that cannot sign. The message
                never contains the key.
        """
        if not isinstance(account, HyperliquidAccount):
            raise InvalidArgumentException("account must be a HyperliquidAccount")
        if not account.public_address:
            raise InvalidArgumentException("public_address is required")
        if not _ADDRESS.fullmatch(str(account.public_address)):
            raise InvalidArgumentException("public_address must be 0x followed by 40 hex characters")
        if not account.private_key:
            raise InvalidArgumentException("private_key is required")
        # Checked here: eth_account accepts 63 hex characters (a key missing one) as another key.
        if not isinstance(account.private_key, str) or not _PRIVATE_KEY.fullmatch(account.private_key):
            raise InvalidArgumentException("private_key must be 64 hex characters, with or without 0x")
        try:
            exchange_account = eth_account.Account.from_key(account.private_key)
        except ValueError:  # e.g. zero: 64 hex characters, but no private key
            raise InvalidArgumentException("private_key is not a valid private key") from None
        self.account = account
        self.exchange_account = exchange_account
        self.public_address = account.public_address

        # Initialize exchange
        # With an API wallet the signing key's address differs from the trading account's.
        # The exchange routes an action by its signer (an API wallet's actions go to the
        # account that approved it); the SDK uses account_address only for its own lookups
        # and never sends it. A vault or sub-account is named in each signed action
        # (vault_address).
        account_address = None
        vault_address = None
        if self.exchange_account.address.lower() != account.public_address.lower():
            if account.is_vault:
                vault_address = account.public_address
            else:
                account_address = account.public_address
        self.exchange = Exchange(
            self.exchange_account, self.base_url,
            meta=self._meta,
            spot_meta=self._spot_meta,
            account_address=account_address,
            vault_address=vault_address,
            timeout=self.request_timeout,
        )
        # Without perp_dexs, the SDK's Exchange builds an Info from the metadata passed, without
        # any request; it then uses the client's Info, which knows every market of this client
        # (those of its HIP-3 dexes too) and reports failures as library exceptions.
        self.exchange.info.session.close()  # the SDK's temporary Info, never used for a request
        self.exchange.info = self.info
        self._apply_proxy(self.exchange)

    def _new_info(self) -> Info:
        """A new SDK Info for this client's network and dexes, with their current markets. The SDK
        builds its symbol tables while it is created (requests, retried like any other)."""
        return self._make_info()

    def _startup_metadata(self, info: Info) -> Tuple[Any, Any]:
        """The default dex's meta and the spot meta of a new Info: those the SDK fetched while
        creating it, else (an SDK that fetches them otherwise) requested now."""
        fetched = info.startup_metadata
        meta = fetched["meta"] if "meta" in fetched else self._with_retry(info.meta)
        spot_meta = fetched["spotMeta"] if "spotMeta" in fetched else self._with_retry(info.spot_meta)
        return meta, spot_meta

    def _make_info(self, **metadata: Any) -> Info:
        """An SDK Info for this client's network and dexes (`metadata`: the default dex's meta
        and spot_meta, if known). A dex the network does not have raises
        InvalidArgumentException (see Info)."""
        return self._with_retry(Info, self.base_url, skip_ws=True, perp_dexs=self.perp_dexs,
                                timeout=self.request_timeout, proxies=self._proxy, **metadata)

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

    def _require_symbol(self, symbol: str) -> None:
        """Raise UnknownSymbolException unless `symbol` is a market of this client."""
        if symbol not in self.info.name_to_coin:
            raise self._unknown_symbol(symbol)

    def _unknown_symbol(self, symbol: str) -> UnknownSymbolException:
        dex = _sdk_dex(str(symbol))
        hint = (f"; markets of the {dex} dex need perp_dexs including {dex!r}"
                if dex and dex not in self.perp_dexs else "")
        return UnknownSymbolException(f"Unknown symbol {symbol!r}{hint}")

    def _dexes_of(self, symbol: Optional[str]) -> List[str]:
        """The dexes to read for `symbol`: its own (none if the client does not cover it), or
        every dex of the client without a symbol."""
        if not symbol:
            return self.perp_dexs
        dex = _sdk_dex(str(symbol))
        return [dex] if dex in self.perp_dexs else []

    def _address(self, address: Optional[str]) -> str:
        """The wallet a read is about: `address`, else the client's own account.

        Raises:
            AddressRequiredException: Without address on a client without an account.
            InvalidArgumentException: If address is not 0x followed by 40 hex characters.
        """
        if address is None:
            if not self.is_authenticated():
                raise AddressRequiredException()
            address = self.public_address
        if not isinstance(address, str) or not _ADDRESS.fullmatch(address):
            raise InvalidArgumentException("Invalid address format")
        return address

    def _apply_proxy(self, sdk_instance):
        """Apply proxy settings to an SDK instance's requests session."""
        if self._proxy and hasattr(sdk_instance, 'session'):
            sdk_instance.session.proxies.update(self._proxy)

    def _with_retry(self, fn, *args, **kwargs):
        """Execute a function with exponential backoff on transient failures.

        For reads and idempotent actions only. Orders, modifies, transfers and margin changes
        go through _signed(), which never retries a request the exchange may have executed.

        Args:
            fn: The function to call.
            *args: Positional arguments to pass to fn.
            **kwargs: Keyword arguments to pass to fn.

        Returns:
            The return value of fn.

        Raises:
            RateLimitException, ServerErrorException, NetworkException or TimeoutException
            when all retries are exhausted; ClientErrorException for any other HTTP 4xx;
            any other error at once.
        """
        last_exception = None
        attempts = self.max_retries + 1  # first attempt + retries

        for attempt in range(attempts):
            try:
                result = fn(*args, **kwargs)
                if _is_unparsed(result):  # the Exchange's requests (Info raises it itself)
                    raise ServerErrorException(_NOT_JSON, 200)
                return result
            except ClientError as e:
                if e.status_code == 429:
                    last_exception = e
                    if attempt < self.max_retries:
                        wait = self.retry_delay * (2 ** attempt)
                        logger.warning(f"Rate limited, retry {attempt + 1}/{self.max_retries} after {wait:.1f}s")
                        time.sleep(wait)
                    else:
                        raise RateLimitException(f"Rate limited after {self.max_retries} retries") from e
                else:
                    raise _library_error(e) from e
            except (requests.ConnectionError, requests.Timeout, ServerError,
                    RateLimitException, ServerErrorException) as e:
                last_exception = e
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** attempt)
                    logger.warning(f"Retry {attempt + 1}/{self.max_retries} after {wait:.1f}s: {e}")
                    time.sleep(wait)
                else:
                    error = _library_error(e)
                    if error is e:
                        raise
                    raise error from e
            except requests.RequestException as e:  # other transport errors: not retried
                raise _library_error(e) from e
            except (ValueError, TypeError, UnauthorizedException,
                    ConfigurationException, RuntimeError):
                raise

        raise last_exception  # pragma: no cover – safety fallback

    def _signed(self, action: str, fn, *args, idempotent: bool = False, **kwargs):
        """Send a signed action.

        Idempotent actions (cancel, leverage) are retried like reads. Any other action is
        retried only when the request provably was not processed: HTTP 429 or a connect
        timeout. Every other failure after sending, including a response that is not valid
        JSON, raises ActionOutcomeUnknownException instead: the exchange may have executed the
        action, and a retry would sign a new one that executes again.
        """
        if idempotent:
            return self._with_retry(self._attempt, fn, *args, **kwargs)
        return self._signed_once(action, fn, *args, **kwargs)

    def _attempt(self, fn, *args, **kwargs):
        """One attempt of a signed action. Signed actions wait while a stop loss / take
        profit replacement of the same key runs (it holds this reentrant lock around all its
        steps, see _replace_protection, so they also wait through the replacement's own
        retries). The wait before an action's own retry is outside the lock."""
        with _signer_state(self.exchange_account.address).operation_lock:
            return self._with_fresh_nonce(fn, *args, **kwargs)

    def _signed_once(self, action: str, fn, *args, **kwargs):
        """The retry loop of _signed() for non-idempotent actions."""
        for attempt in range(self.max_retries + 1):
            try:
                response = self._attempt(fn, *args, **kwargs)
            except requests.ConnectTimeout as e:
                if attempt == self.max_retries:
                    raise _library_error(e) from e  # TimeoutException: not processed, can be sent again
            except ClientError as e:
                if e.status_code != 429:
                    raise _library_error(e) from e  # ClientErrorException: the request was refused
                if attempt == self.max_retries:
                    raise RateLimitException(f"Rate limited after {self.max_retries} retries") from e
            except (requests.RequestException, ServerError) as e:
                raise ActionOutcomeUnknownException(action, str(e)) from e
            else:
                if _is_unparsed(response):  # the SDK turns a body that is not JSON into this dict
                    raise ActionOutcomeUnknownException(action, response["error"])
                return response
            wait = self.retry_delay * (2 ** attempt)
            logger.warning(f"{action} was not processed, retry {attempt + 1}/{self.max_retries} after {wait:.1f}s")
            time.sleep(wait)

    @staticmethod
    def _action_result(result: Any, action: str) -> Any:
        """The exchange's answer to an action other than an order (a transfer, a leverage or
        margin change): "ok" is done, "err" is refused (ActionRejectedException with its
        text); any other answer leaves open whether it was done (ActionOutcomeUnknownException)."""
        if isinstance(result, dict) and result.get("status") == "ok":
            return result
        if isinstance(result, dict) and result.get("status") == "err":
            raise ActionRejectedException(action, str(result.get("response")))
        raise ActionOutcomeUnknownException(action, f"answered in no documented format: {result}")

    def _with_fresh_nonce(self, fn, *args, **kwargs):
        """Call a signing SDK method with this signer's lock held and an unused nonce.

        The SDK uses the current time in milliseconds as the nonce, so two actions from one
        signer in the same millisecond collide. This waits until the clock has moved past the
        last action of the signer, across all clients in the process.
        """
        signer = _signer_state(self.exchange_account.address)
        with signer.lock:
            # If the clock was set back, stop waiting after a second: the exchange accepts a
            # lower nonce that was never used and is above the smallest of the signer's 100
            # highest nonces (nonce docs).
            give_up = time.monotonic() + 1.0
            while time.time_ns() // 1_000_000 <= signer.last_nonce_ms and time.monotonic() < give_up:
                time.sleep(0.0005)
            try:
                return fn(*args, **kwargs)
            finally:
                signer.last_nonce_ms = max(signer.last_nonce_ms, time.time_ns() // 1_000_000)

    def _place_order(self, symbol: str, cloid: Cloid, **order) -> Any:
        """Send one order with its client order id and return the exchange response.

        If the outcome is unknown after sending, the order is looked up by its client order
        id and the response is rebuilt from what the exchange reports, so callers parse it the
        same way. The order is never sent twice.
        """
        try:
            response = self._signed(f"{symbol} order", self.exchange.order, name=symbol, cloid=cloid, **order)
        except ActionOutcomeUnknownException as exc:
            cause: Exception = exc
        else:
            if self._is_definite_order_response(response):
                return response
            cause = ValueError(f"unexpected order response: {response}")
        status = self._reconcile_order(symbol, cloid, cause)
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}

    @staticmethod
    def _statuses(response: Any) -> Optional[List[Any]]:
        """The order statuses of an "ok" answer to an order action, else None."""
        try:
            statuses = response["response"]["data"]["statuses"] if response.get("status") == "ok" else None
        except (KeyError, TypeError, AttributeError):
            return None
        return statuses if isinstance(statuses, list) else None

    @classmethod
    def _documented_status(cls, response: Any) -> Optional[Dict[str, Any]]:
        """The first order status of an "ok" answer if it is in the documented format, else None."""
        statuses = cls._statuses(response)
        status = statuses[0] if statuses else None
        return status if cls._is_documented_status(status) else None

    @staticmethod
    def _is_documented_status(status: Any) -> bool:
        """True for an order status in the documented format: {"resting": {"oid"}},
        {"filled": {"oid", "totalSz", "avgPx"}} or {"error": text}."""
        if not isinstance(status, dict):
            return False
        def order_id(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        def amount(value: Any) -> bool:  # a decimal string or number, as the docs show them
            try:
                return not isinstance(value, bool) and Decimal(str(value)).is_finite()
            except (InvalidOperation, ValueError, TypeError):
                return False

        if isinstance(status.get("error"), str):
            return True
        resting, filled = status.get("resting"), status.get("filled")
        if isinstance(resting, dict) and order_id(resting.get("oid")):
            return True
        return (isinstance(filled, dict) and order_id(filled.get("oid"))
                and amount(filled.get("totalSz")) and amount(filled.get("avgPx")))

    @classmethod
    def _is_definite_order_response(cls, response: Any) -> bool:
        """True if the exchange answered with the order's status: resting, filled, or an
        explicit rejection. Anything else leaves open whether the order was placed."""
        if isinstance(response, dict) and response.get("status") == "err":
            return True
        return cls._documented_status(response) is not None

    def _reconcile_order(self, symbol: str, cloid: Cloid, cause: Exception) -> Dict[str, Any]:
        """Status entry ({"resting" | "filled" | "error": ...}) for an order whose placement
        response was lost, from the order status looked up by client order id.

        Raises OrderOutcomeUnknownException if the order is not found, or its status is not
        one of the documented ones.
        """
        detail = str(cause)
        for delay in (0.5, 1.0, 2.0):
            time.sleep(delay)
            try:
                result = self.info.query_order_by_cloid(self.public_address, cloid)
                if result.get("status") != "order":  # "unknownOid": not known (yet)
                    detail = f"order not found ({result.get('status')})"
                    continue
                order = result["order"]["order"]
                oid = order["oid"]
                status = result["order"]["status"]
                if status == "open":
                    return {"resting": {"oid": oid}}
                if status == "triggered":  # a trigger order that fired and is executing
                    detail = "trigger order triggered, its outcome is not known yet"
                    continue
                if status == "rejected" or status.endswith("Rejected"):
                    text = _REJECTION_TEXTS.get(status)
                    return {"error": f"{text} (order {cloid}: {status})" if text
                            else f"Order {cloid} was rejected ({status})"}
                if status == "filled" or status in ("canceled", "scheduledCancel") or status.endswith("Canceled"):
                    # "sz" is what was left when the order ended; the rest was filled.
                    executed = Decimal(str(order["origSz"])) - Decimal(str(order["sz"]))
                    if executed <= 0:
                        return {"error": f"Order {cloid} was {status} without a fill"}
                    fills = [f for f in self.info.user_fills(self.public_address) if f.get("oid") == oid]
                    total = sum((Decimal(str(f["sz"])) for f in fills), Decimal(0))
                    if total < executed:
                        detail = f"order {status}, {executed} filled, but only {total} of it is in the fills yet"
                        continue
                    notional = sum(Decimal(str(f["px"])) * Decimal(str(f["sz"])) for f in fills)
                    return {"filled": {"oid": oid, "totalSz": str(total), "avgPx": str(notional / total)}}
                detail = f"unrecognized order status {status!r}"
                break
            except Exception as exc:
                detail = f"order status lookup failed: {exc}"
        raise OrderOutcomeUnknownException(symbol, str(cloid), detail) from cause

    # Per-order cancel error when the order is no longer open (exchange endpoint docs).
    _ORDER_GONE = "Order was never placed, already canceled, or filled"

    def _cancel_outcomes(self, response: Any, count: int) -> List[str]:
        """Outcome of each order in a cancel response, in request order: "canceled", "gone"
        (never placed, already canceled or filled) or the exchange's error text.

        Raises OrderException if the response does not confirm the request at all (error
        status, a body that is not JSON, or a status list of the wrong length).
        """
        try:
            statuses = response["response"]["data"]["statuses"] if response["status"] == "ok" else None
        except (KeyError, TypeError):
            statuses = None
        if not isinstance(statuses, list) or len(statuses) != count:
            raise OrderException(f"Cancel was not confirmed by the exchange: {response}")
        outcomes = []
        for status in statuses:
            error = status.get("error") if isinstance(status, dict) else None
            if status == "success":
                outcomes.append("canceled")
            elif isinstance(error, str) and error.startswith(self._ORDER_GONE):
                outcomes.append("gone")
            else:
                outcomes.append(str(error if error is not None else status))
        return outcomes

    def _cancel_all(self, cancels: List[Dict[str, Any]]) -> None:
        """Bulk-cancel orders ({"coin", "oid"}); raise OrdersNotCanceledException naming each
        one that the exchange did not cancel. Orders that are already gone count as done."""
        response = self._signed("cancel", self.exchange.bulk_cancel, cancels, idempotent=True)
        failed = [
            {"symbol": cancel["coin"], "order_id": cancel["oid"], "reason": outcome}
            for cancel, outcome in zip(cancels, self._cancel_outcomes(response, len(cancels)))
            if outcome not in ("canceled", "gone")
        ]
        if failed:
            raise OrdersNotCanceledException(failed)

    def _cancel_one(self, coin: str, order: Union[int, Cloid]) -> bool:
        """Cancel one order by its order id or its client order id: True if canceled, False
        if it was already gone (never placed, already canceled or filled). Raises
        OrderException for any other result.

        A cancel is sent again when its answer was lost (it is idempotent). The exchange then
        answers that the order is gone, although the first attempt may have canceled it: the
        order's status tells which ("canceled", or filled).
        """
        self._require_symbol(coin)  # else the SDK fails with a bare KeyError while signing
        cancel = self.exchange.cancel_by_cloid if isinstance(order, Cloid) else self.exchange.cancel
        attempts = 0

        def send(*args: Any) -> Any:
            nonlocal attempts
            attempts += 1
            return cancel(*args)

        response = self._signed("cancel", send, coin, order, idempotent=True)
        outcome = self._cancel_outcomes(response, 1)[0]
        if outcome == "gone" and attempts > 1:
            return self._was_canceled(coin, order)
        if outcome in ("canceled", "gone"):
            return outcome == "canceled"
        raise OrderException(f"Order {order} for {coin} was not canceled: {outcome}")

    def _was_canceled(self, coin: str, order: Union[int, Cloid]) -> bool:
        """True if the order's status is "canceled" (a cancel, not a fill or another end). If
        the status cannot be read, False: the order is not open, how it ended is not known."""
        query = self.info.query_order_by_cloid if isinstance(order, Cloid) else self.info.query_order_by_oid
        try:
            result = self._with_retry(query, self.public_address, order)
            return result["status"] == "order" and result["order"]["status"] == "canceled"
        except Exception as exc:
            logger.warning(f"Order {order} for {coin} is not open; how it ended could not be read: {exc}")
            return False

    _SENTINEL_DEX = object()  # Used to detect "dex not explicitly passed"

    def get_user_state(self, address: Optional[str] = None, dex: object = _SENTINEL_DEX) -> UserState:
        """Get the state of any user by their address.

        When dex is not specified, merges positions and margin summaries across the client's
        perp dexes (perp_dexs: the default dex, plus xyz with extended_universe=True). Pass
        dex="" or dex="xyz" explicitly to query a single clearinghouse.

        Raises:
            AddressRequiredException: Without address on a client without an account
            ValueError: If address is not 0x followed by 40 hex characters
        """
        address = self._address(address)

        # If dex explicitly specified, query single clearinghouse
        if dex is not self._SENTINEL_DEX:
            return self._get_user_state_single(address, dex)

        # Auto-merge across all perp_dexs
        if len(self.perp_dexs) == 1:
            return self._get_user_state_single(address, self.perp_dexs[0])

        # Merge multiple dexes
        merged_positions = []
        merged_margin = {"account_value": Decimal("0"), "total_margin_used": Decimal("0"),
                         "total_ntl_pos": Decimal("0"), "total_raw_usd": Decimal("0")}
        merged_cross = {"account_value": Decimal("0"), "total_margin_used": Decimal("0"),
                        "total_ntl_pos": Decimal("0"), "total_raw_usd": Decimal("0")}
        merged_withdrawable = Decimal("0")
        merged_cross_maint = Decimal("0")

        for d in self.perp_dexs:
            state = self._get_user_state_single(address, d)
            merged_positions.extend(state.asset_positions)
            for field in merged_margin:
                merged_margin[field] += getattr(state.margin_summary, field)
                merged_cross[field] += getattr(state.cross_margin_summary, field)
            merged_withdrawable += state.withdrawable
            if state.cross_maintenance_margin_used:
                merged_cross_maint += state.cross_maintenance_margin_used

        return UserState(
            asset_positions=merged_positions,
            margin_summary=MarginSummary(**merged_margin),
            cross_margin_summary=MarginSummary(**merged_cross),
            withdrawable=merged_withdrawable,
            cross_maintenance_margin_used=merged_cross_maint or None,
        )

    def _get_user_state_single(self, address: str, dex: str = "") -> UserState:
        """Get user state from a single clearinghouse."""
        response = self._with_retry(self.info.user_state, address, dex=dex)

        # clearinghouseState as documented: these fields are always there. Read directly, so a
        # response in another shape shows as an error instead of an account with zero balances.
        def summary(raw: Dict[str, Any]) -> Dict[str, Any]:
            return {"account_value": raw["accountValue"], "total_margin_used": raw["totalMarginUsed"],
                    "total_ntl_pos": raw["totalNtlPos"], "total_raw_usd": raw["totalRawUsd"]}

        formatted_response = {
            "asset_positions": [],
            "margin_summary": summary(response["marginSummary"]),
            "cross_margin_summary": summary(response["crossMarginSummary"]),
            "withdrawable": response["withdrawable"],
            "cross_maintenance_margin_used": response.get("crossMaintenanceMarginUsed"),
        }

        # assetPositions is always there (an empty list without positions)
        for pos in response["assetPositions"]:
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
        
        
    def get_positions(self, *, address: Optional[str] = None) -> List[Position]:
        """Get current open positions across all perp dexes.

        Args:
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        state = self.get_user_state(self._address(address))
        return [pos.position for pos in state.asset_positions]
        
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
        return _round_down(size, sz_decimals)

    def _validate_and_format_order(
        self,
        symbol: str,
        size: float,
        limit_price: float
    ) -> tuple[float, float]:
        """Validate and format order size and price using Hyperliquid SDK logic.

        - Sizes are rounded to sz_decimals (from market specs)
        - Prices are rounded to 5 significant figures, then to (6 - sz_decimals) decimal places
        - Validates minimum position sizes based on market specs, and that the price (limit or
          trigger, always required) is a positive number that stays one after rounding: not 0,
          not infinite (InvalidArgumentException)

        Rounded values come back unchanged, so preparing an order twice changes nothing.
        """
        self._check_order_size(symbol, size)

        # Format size to sz_decimals
        size = self._format_size(symbol, size)

        # Format price using SDK-matching logic
        if not _positive_number(limit_price):
            raise InvalidArgumentException(f"price must be a positive number, got {limit_price!r}")
        price = self._format_price(symbol, limit_price)
        if not _positive_number(price):  # rounds to 0, or overflows to infinity
            raise InvalidArgumentException(f"price {limit_price!r} rounds to {price} at {symbol}'s price precision")

        return size, price

    def _check_order_size(self, symbol: str, size: float) -> None:
        """Raise UnknownSymbolException (a ValueError) for an unknown symbol, ValueError for a
        size below the minimum. No request: the markets are those known when the client was
        created or last refreshed."""
        if symbol not in self.market_specs or symbol not in self.info.name_to_coin:
            # Sent anyway, it would have 0 size decimals, or fail inside the SDK (KeyError).
            error = self._unknown_symbol(symbol)
            raise UnknownSymbolException(
                f"{error}; a market listed after the client was created needs refresh_market_specs()")
        if not _positive_number(size):
            raise InvalidArgumentException(f"size must be a positive number, got {size!r}")
        size_decimals = self.market_specs[symbol].get("size_decimals", 0)
        min_size = 1.0 / (10 ** size_decimals) if size_decimals > 0 else 1
        if size < min_size:
            raise InvalidArgumentException(
                f"Minimum position size for {symbol} is {min_size} "
                f"(size_decimals={size_decimals}). Got: {size}"
            )

    @staticmethod
    def _resolve_time_in_force(time_in_force: str, post_only: bool) -> str:
        """Time in force for a limit order. post_only is Hyperliquid's "Alo" (add liquidity only)."""
        if time_in_force not in _TIMES_IN_FORCE:
            raise InvalidArgumentException(f"time_in_force must be Gtc, Ioc or Alo, got {time_in_force!r}")
        if not post_only:
            return time_in_force
        if time_in_force == "Ioc":
            raise InvalidArgumentException("post_only cannot be used with IOC orders")
        return "Alo"

    def _mid_price(self, symbol: str) -> float:
        current_price = self.get_price(symbol)
        logger.debug(f"Current price for {symbol}: {current_price}")
        if current_price <= 0:
            raise HyperliquidException(f"Invalid current price for {symbol}: {current_price}")
        return current_price

    def _with_slippage(self, price: float, is_buy: bool, slippage: Optional[float]) -> float:
        """Worst acceptable price for a market order at `price` (matching SDK logic)."""
        effective_slippage = slippage if slippage is not None else self.default_slippage
        if not 0 < effective_slippage <= 0.5:
            raise InvalidArgumentException("slippage must be between 0 (exclusive) and 0.5 (inclusive)")
        return price * (1 + effective_slippage) if is_buy else price * (1 - effective_slippage)

    def _market_limit_price(self, symbol: str, is_buy: bool, slippage: Optional[float]) -> float:
        """Limit price for a market order: current mid price plus slippage."""
        return self._with_slippage(self._mid_price(symbol), is_buy, slippage)

    @classmethod
    def _order_status(cls, response: Any, action: str) -> Dict[str, Any]:
        """The status of the order in the exchange's answer: {"resting": ...} or {"filled": ...}.

        Raises:
            InsufficientMarginException: The exchange refused the order for lack of margin.
            OrderRejectedException: The exchange refused it for another reason (its text).
            ActionOutcomeUnknownException: The answer is in no documented format: the order
                may still have been placed or changed.
        """
        if isinstance(response, dict) and response.get("status") == "err":  # refused as a whole
            raise OrderRejectedException(action, str(response.get("response")))
        status = cls._documented_status(response)
        if status is None:
            raise ActionOutcomeUnknownException(action, f"answered in no documented format: {response}")
        if "error" in status:
            if status["error"].startswith(_INSUFFICIENT_MARGIN):
                raise InsufficientMarginException(action, status["error"])
            raise OrderRejectedException(action, status["error"])
        return status

    @classmethod
    def _order_from_response(cls, response: Any, symbol: str, is_buy: bool, size: float, limit_price: float,
                             order_type: Dict[str, Any], reduce_only: bool, action: str,
                             market: Optional[str] = None, cloid: Optional[Cloid] = None) -> Order:
        """The Order of the exchange's answer to an order or a modify (the SDK sends a modify
        as batchModify): the first entry of its statuses, as the exchange endpoint documents
        it for orders. Rejections and unknown outcomes raise, see _order_status. `cloid`: the
        client order id the order was sent with."""
        status = cls._order_status(response, action)
        trigger = order_type.get("trigger")
        order_data: Dict[str, Any] = {
            "symbol": symbol,
            "is_buy": is_buy,
            "size": str(size),
            "order_type": order_type,
            "reduce_only": reduce_only,
            "time_in_force": order_type["limit"].get("tif", "Gtc") if "limit" in order_type else "Gtc",
            "created_at": int(time.time() * 1000),
            "limit_price": str(limit_price),
            "type": "limit" if trigger is None else {"tp": "take_profit", "sl": "stop_loss"}.get(trigger.get("tpsl"), "unknown"),
        }
        if trigger is not None and "triggerPx" in trigger:
            order_data["trigger_price"] = str(trigger["triggerPx"])
        if market is not None:
            order_data["market"] = market
        if cloid is not None:
            order_data["cloid"] = cloid.to_raw()
        if "resting" in status:
            order_data.update(order_id=status["resting"]["oid"], status="open")
        else:
            filled = status["filled"]
            order_data.update(order_id=filled["oid"], status="filled", filled_size=str(filled["totalSz"]),
                              average_fill_price=str(filled["avgPx"]))
        return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)

    def prepare_order(
        self,
        symbol: str,
        size: float,
        is_buy: bool,
        limit_price: Optional[float] = None,
        slippage: Optional[float] = None,
    ) -> tuple[float, float]:
        """The size and limit price create_order() sends for these arguments, without sending
        anything: to check an order before it is placed.

        The size is rounded down to the market's lot step, the price rounded as the SDK rounds
        it (5 significant figures, which can round up). Without limit_price it is the price of
        a market order: the mid price plus or minus the slippage (slippage=, else the client's
        default_slippage), read now. create_order() prepares every order with this method, and
        prepared values come back unchanged, so placing the returned size and price (with
        time_in_force="Ioc" for a market order) sends the same order.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            is_buy (bool): True for a buy, False for a sell (the side of a market order's price)
            limit_price (Optional[float]): The limit price; None for a market order
            slippage (Optional[float]): For a market order, overrides default_slippage

        Returns:
            tuple[float, float]: (size, limit_price) as they are signed

        Raises:
            UnknownSymbolException: If the symbol is not a market of the client (no request)
            InvalidArgumentException: If the size or price is not a positive number, the size
                is below the market's minimum, the price rounds to 0, or the slippage is not
                above 0 and at most 0.5
        """
        self._check_order_size(symbol, size)  # before the price is read: no request for bad input
        if limit_price is None:
            limit_price = self._market_limit_price(symbol, is_buy, slippage)
        return self._validate_and_format_order(symbol, size, limit_price)

    def create_order(
        self,
        symbol: str,
        size: float,
        is_buy: bool,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        time_in_force: Optional[Literal["Gtc", "Ioc", "Alo"]] = None,
        slippage: Optional[float] = None,
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Create an order with simplified parameters.
        
        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            is_buy (bool): True for buy orders, False for sell orders
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, a market order: the mid
                price plus or minus the slippage (slippage=, else the client's default_slippage)
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only valid for limit orders).
                Sent as time in force "Alo"; the exchange rejects the order if it would match immediately.
            time_in_force (Optional[str]): "Gtc" (Good till Cancel), "Ioc" (Immediate or Cancel),
                "Alo" (Add Limit Only). Default: "Ioc" without limit_price (a market order, as in
                the SDK's market_open and buy()/sell()), "Gtc" with one.
            slippage (Optional[float]): For a market order, overrides default_slippage.
            cloid (Optional[str]): Client order id to place the order with: "0x" and 32 hex digits,
                unique per order. If None, a random one. It is the order's ``cloid``; look the order
                up or cancel it by it with get_order_status_by_cloid() and cancel_order_by_cloid().

        Returns:
            Order: Order response from the exchange
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        order_cloid = _order_cloid(cloid)
        if time_in_force is None:
            time_in_force = "Ioc" if limit_price is None else "Gtc"
        time_in_force = self._resolve_time_in_force(time_in_force, post_only)

        # The size and price to sign: rounded, and for a market order the mid ± slippage
        size, limit_price = self.prepare_order(symbol, size, is_buy, limit_price, slippage)
        logger.debug(f"Formatted order: symbol={symbol}, size={size}, limit_price={limit_price}")

        # Construct order type (matching SDK)
        order_type = {"limit": {"tif": time_in_force}}

        # Debug logging
        logger.debug(f"Order type structure: {order_type}")
        logger.debug(f"Order parameters: symbol={symbol}, size={size}, limit_price={limit_price}, is_buy={is_buy}")

        try:
            response = self._place_order(
                symbol,
                order_cloid,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type=order_type,
                reduce_only=reduce_only,
            )

            logger.debug(f"Order response: {response}")
            return self._order_from_response(response, symbol, is_buy, size, limit_price, order_type, reduce_only,
                                             f"{symbol} order", cloid=order_cloid)

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
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Simple buy order function.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, creates a market order
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only for limit orders)
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage if set.
            cloid (Optional[str]): Client order id, see create_order().

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
            cloid=cloid,
        )

    def sell(
        self,
        symbol: str,
        size: float,
        limit_price: Optional[float] = None,
        reduce_only: bool = False,
        post_only: bool = False,
        slippage: Optional[float] = None,
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Simple sell order function.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            limit_price (Optional[float]): Price for limit orders. If None, creates a market order
            reduce_only (bool): Whether the order should only reduce position
            post_only (bool): Whether the order should only be maker (only for limit orders)
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage if set.
            cloid (Optional[str]): Client order id, see create_order().

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
            cloid=cloid,
        )

    def _closing_side(self, symbol: str, is_buy: Optional[bool]) -> bool:
        """Side (is_buy) of an order that reduces the current position in `symbol`.

        Raises PositionNotFoundException if there is no position, and ValueError if an
        explicit `is_buy` would not reduce it.
        """
        position = next((p for p in self.get_positions() if p.symbol == symbol), None)
        if position is None or position.size == 0:
            raise PositionNotFoundException(f"No position found for {symbol}")
        closing_is_buy = position.is_short
        if is_buy is not None and is_buy != closing_is_buy:
            side = "short" if position.is_short else "long"
            raise InvalidArgumentException(
                f"is_buy={is_buy} does not close the {side} {symbol} position; "
                "omit is_buy to derive the side from the position"
            )
        return closing_is_buy

    def stop_loss(
        self,
        symbol: str,
        size: float,
        trigger_price: float,
        is_buy: Optional[bool] = None,
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Place a stop loss order.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            trigger_price (float): Price at which the stop loss triggers
            is_buy (Optional[bool]): Side of the order. Omit it to derive the side from the
                position (sell for a long, buy for a short); a value that would not close the
                position raises ValueError.
            cloid (Optional[str]): Client order id, see create_order().
        """
        return self._protection_order("sl", symbol, size, trigger_price, is_buy, cloid)

    def take_profit(
        self,
        symbol: str,
        size: float,
        trigger_price: float,
        is_buy: Optional[bool] = None,
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Place a take profit order.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Order size in base currency
            trigger_price (float): Price at which the take profit triggers
            is_buy (Optional[bool]): Side of the order. Omit it to derive the side from the
                position (sell for a long, buy for a short); a value that would not close the
                position raises ValueError.
            cloid (Optional[str]): Client order id, see create_order().
        """
        return self._protection_order("tp", symbol, size, trigger_price, is_buy, cloid)

    def _protection_order(self, tpsl: str, symbol: str, size: float, trigger_price: float,
                          is_buy: Optional[bool], cloid: Optional[str]) -> Order:
        """Shared implementation of stop_loss ("sl") and take_profit ("tp")."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        order_cloid = _order_cloid(cloid)
        return self._place_trigger(tpsl, symbol, size, trigger_price, self._closing_side(symbol, is_buy), order_cloid)

    def _place_trigger(self, tpsl: str, symbol: str, size: float, trigger_price: float,
                       is_buy: bool, order_cloid: Cloid) -> Order:
        """Place a reduce-only market trigger order ("sl" or "tp") on the side `is_buy`, which
        the caller derived from the position."""
        # Validate and format size and trigger price (handles all rounding)
        size, trigger_price = self._validate_and_format_order(symbol, size, trigger_price)
        order_type = {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": tpsl}}
        response = self._place_order(
            symbol,
            order_cloid,
            is_buy=is_buy,
            sz=size,
            limit_px=trigger_price,
            reduce_only=True,
            order_type=order_type,
        )
        name = "stop loss" if tpsl == "sl" else "take profit"
        return self._order_from_response(response, symbol, is_buy, size, trigger_price, order_type, True,
                                         f"{symbol} {name}", cloid=order_cloid)

    def open_long_position(
        self,
        symbol: str,
        size: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        slippage: Optional[float] = None,
        *,
        stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
        take_profits: Optional[Sequence[Tuple[float, float]]] = None,
        time_in_force: Optional[Literal["Gtc", "Ioc", "Alo"]] = None,
    ) -> Dict[str, Order]:
        """Open a long position with optional stop loss and take profit orders.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Position size
            stop_loss_price (Optional[float]): Stop loss price level, for the whole position
            take_profit_price (Optional[float]): Take profit price level, for the whole position
            limit_price (Optional[float]): Limit price for entry, None for market order
            slippage (Optional[float]): Slippage for market entry. Overrides default_slippage if set.
            stop_losses: Instead of stop_loss_price, several stop losses as (price, fraction):
                [(95, 0.5), (90, 0.5)] closes half the position at each. The fractions add up
                to at most 1.
            take_profits: Instead of take_profit_price, several take profits, the same way.
            time_in_force: With limit_price, "Gtc" (default) rests until filled; "Ioc" fills
                what it can at once and cancels the rest (a market entry with a price limit).

        Each stop loss and take profit is placed once the entry fills, for its fraction of the
        filled size: rounded down to the lot size, the one nearest the entry of the stop losses
        (and of the take profits) taking what rounding left, so together they cover their share
        exactly, and a partial fill never loses more at its stops than the full size would.

        Returns:
            Dict[str, Order]: "entry", then "stop_loss" and "take_profit" with the single
            prices, or "stop_loss_1", "stop_loss_2", ... and "take_profit_1", ... with lists.

        Raises:
            ValueError: If a stop loss or take profit price is on the wrong side of the entry
                price (of a limit entry, or the mid if the limit is above it, as it can fill
                at once; else of the mid), the fractions add up to
                more than 1, or one would be worth less than Hyperliquid's minimum order
                value of 10 USD at its price. Checked before any order is sent.
            ProtectionOrderException: If the entry was placed but a stop loss or take profit
                was not: placing it raised an error (an unknown outcome is marked, with its
                cloid: it may still arrive), or it executed at once because the price had
                already crossed it. It carries the orders placed and those missing.

        A level whose share rounds to 0 at a small partial fill is not placed (its part goes
        to the nearest of its kind, so the coverage stays): compare the returned keys.
        """
        return self._open_position(
            symbol, size, True, stop_loss_price, take_profit_price, limit_price, slippage,
            stop_losses, take_profits, time_in_force
        )

    def open_short_position(
        self,
        symbol: str,
        size: float,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        slippage: Optional[float] = None,
        *,
        stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
        take_profits: Optional[Sequence[Tuple[float, float]]] = None,
        time_in_force: Optional[Literal["Gtc", "Ioc", "Alo"]] = None,
    ) -> Dict[str, Order]:
        """Open a short position with optional stop loss and take profit orders, as
        open_long_position (stop losses above the entry, take profits below it).

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            size (float): Position size
            stop_loss_price (Optional[float]): Stop loss price level, for the whole position
            take_profit_price (Optional[float]): Take profit price level, for the whole position
            limit_price (Optional[float]): Limit price for entry, None for market order
            slippage (Optional[float]): Slippage for market entry. Overrides default_slippage if set.
            stop_losses, take_profits, time_in_force: as in open_long_position.

        Returns:
            Dict[str, Order]: as open_long_position.

        Raises:
            ValueError: If a stop loss or take profit price is on the wrong side of the entry
                price, or as open_long_position. Checked before any order is sent.
            ProtectionOrderException: If the entry was placed but placing a stop loss or take
                profit raised an error; it carries the orders that were placed and those missing.
        """
        return self._open_position(
            symbol, size, False, stop_loss_price, take_profit_price, limit_price, slippage,
            stop_losses, take_profits, time_in_force
        )

    def _open_position(
        self,
        symbol: str,
        size: float,
        is_long: bool,
        stop_loss_price: Optional[float],
        take_profit_price: Optional[float],
        limit_price: Optional[float],
        slippage: Optional[float],
        stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
        take_profits: Optional[Sequence[Tuple[float, float]]] = None,
        time_in_force: Optional[str] = None,
    ) -> Dict[str, Order]:
        """Shared implementation of open_long_position and open_short_position."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        if time_in_force is not None and (limit_price is None or time_in_force not in _TIMES_IN_FORCE):
            raise InvalidArgumentException("time_in_force is Gtc, Ioc or Alo, and only with limit_price")
        legs = (_protection_legs("stop_loss", stop_loss_price, stop_losses)
                + _protection_legs("take_profit", take_profit_price, take_profits))
        self._check_order_size(symbol, size)
        return self._open_position_with_specs(symbol, size, is_long, legs, limit_price, slippage, time_in_force)

    def _open_position_with_specs(
        self,
        symbol: str,
        size: float,
        is_long: bool,
        legs: List[Tuple[str, str, float, Decimal]],
        limit_price: Optional[float],
        slippage: Optional[float],
        time_in_force: Optional[str] = None,
    ) -> Dict[str, Order]:
        """Body of _open_position. `legs`: (name, kind, price, fraction) per stop loss and
        take profit."""
        # Everything is checked before anything is placed, at the prices that will be sent:
        # the stop loss and take profit against a resting limit entry's price, or for an entry
        # that executes at once (market, or a limit with Ioc) against the mid price (the
        # entry's own limit includes the slippage allowance, which is no price it fills at).
        # A limit beyond the mid (a buy above it, a sell below) can fill at once, near the
        # mid (it does once it reaches the other side of the book): the nearer of the two is
        # the reference, so a level is never checked against a price the entry may beat.
        if limit_price is not None and time_in_force != "Ioc":
            entry_price = limit_price
            time_in_force = time_in_force or "Gtc"
            reference = limit_price
            if legs:
                mid = self._mid_price(symbol)
                reference = min(limit_price, mid) if is_long else max(limit_price, mid)
        else:
            reference = self._mid_price(symbol)
            entry_price = limit_price if limit_price is not None else self._with_slippage(reference, is_long, slippage)
            time_in_force = "Ioc"
        entry_px = self._format_price(symbol, reference)
        decimals = self.market_specs[symbol].get("size_decimals", 0)
        # The levels at their prices as sent (rounding can make two equal: the remainder then
        # goes to the same one as the MCP server's plan), and the size as the entry is sent.
        sent = [(name, kind, self._format_price(symbol, price), fraction) for name, kind, price, fraction in legs]
        planned = split_size(Decimal(str(_round_down(size, decimals))), sent, decimals, is_long)
        for (name, kind, price, fraction), leg_size in zip(legs, planned):
            label = name.replace("_", " ").capitalize()
            px = self._format_price(symbol, price)
            if not _positive_number(px):  # rounds to 0, or overflows to infinity
                raise InvalidArgumentException(f"{label} price rounds to {px} at {symbol}'s price precision")
            below = (kind == "stop_loss") == is_long  # a long's stop and a short's take profit: below
            if (px >= entry_px) if below else (px <= entry_px):
                raise InvalidArgumentException(
                    f"{label} price must be {'below' if below else 'above'} entry price for "
                    f"{'longs' if is_long else 'shorts'}")
            # Hyperliquid refuses an order worth less than 10 USD (reduce-only ones closing a
            # whole position may be exempt; nothing here relies on it): refused now, not after
            # the entry filled.
            if leg_size * Decimal(str(px)) < MIN_ORDER_USD:
                raise InvalidArgumentException(
                    f"{label} would be {leg_size} at {px}, worth less than Hyperliquid's minimum order of "
                    f"{MIN_ORDER_USD} USD")

        entry = self.create_order(
            symbol, size, is_long, limit_price=entry_price, time_in_force=time_in_force
        )
        orders = {"entry": entry}
        if not legs:
            return orders
        if entry.status != "filled":
            raise ProtectionOrderException(legs[0][0], orders, _missing(legs, planned)) from ValueError(
                "the entry order is resting, not filled, so there is no position to protect "
                "yet; place the stop loss and take profit after it fills"
            )

        # A market entry can fill only partly: protect the filled size.
        filled = Decimal(str(entry.filled_size)) if entry.filled_size else Decimal(str(size))
        sizes = split_size(filled, sent, decimals, is_long)
        try:
            # One read for all levels: the position must be on the entry's side (an opposite
            # position the entry only reduced has nothing these orders protect).
            closing_is_buy = self._closing_side(symbol, not is_long)
        except Exception as exc:
            raise ProtectionOrderException(legs[0][0], orders, _missing(legs, sizes)) from exc
        for index, ((name, kind, price, _), leg_size) in enumerate(zip(legs, sizes)):
            if leg_size <= 0:  # a fraction too small for the filled size: its part went to the last one
                logger.warning("%s: %s at %s rounds to 0 for the filled size %s and is not placed",
                               symbol, name, price, filled)
                continue
            try:
                placed = self._place_trigger("sl" if kind == "stop_loss" else "tp", symbol, float(leg_size),
                                             price, closing_is_buy, _order_cloid(None))
            except Exception as exc:
                missing = _missing(legs[index:], sizes[index:])
                if isinstance(exc, OrderOutcomeUnknownException) and missing:  # it may still arrive
                    missing[0].update(outcome_unknown=True, cloid=exc.cloid)
                raise ProtectionOrderException(name, orders, missing) from exc
            orders[name] = placed
            if placed.status != "open":  # it executed at once: the price had already crossed it
                left = leg_size - Decimal(str(placed.filled_size or 0))
                rest = _missing(legs[index:index + 1], [left]) + _missing(legs[index + 1:], sizes[index + 1:])
                raise ProtectionOrderException(name, orders, rest) from ValueError(
                    f"the {name.replace('_', ' ')} at {price} executed at once ({placed.status}, "
                    f"{placed.filled_size} of {leg_size}): the price had already crossed it; the rest of the "
                    "position has not all its protection")
        return orders

    def _make_filled_order(self, order_id: int, symbol: str, is_buy: bool, size: float,
                           order: Order, avg_fill_price: Optional[Decimal] = None) -> Order:
        """Create a filled Order object from a completed maker chase."""
        logger.info(f"Maker order filled: {symbol} {'buy' if is_buy else 'sell'} {size}"
                    f"{f' @ {avg_fill_price}' if avg_fill_price else ''}")
        return Order(
            order_id=order_id,
            cloid=order.cloid,
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

    def _auto_reprice_interval(self, symbol: str,
                               bbo_cache: Optional[Dict[str, Dict[str, float]]] = None) -> tuple:
        """Auto-detect timeout and reprice_interval based on order book spread.

        Returns:
            (timeout, reprice_interval) tuple
        """
        try:
            # Try bbo_cache first, fall back to REST
            cached = bbo_cache.get(symbol) if bbo_cache else None
            if cached and "bid" in cached and "ask" in cached:
                mid = (cached["bid"] + cached["ask"]) / 2
                spread = cached["ask"] - cached["bid"]
            else:
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
        bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
        fill_event: Optional[threading.Event] = None,
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
            MakerOrderIncompleteException: If the chase stopped on an error after part of the
                size had filled (e.g. the rest was refused, below Hyperliquid's minimum order
                value); it says how much filled, at which average price. The error is
                ``__cause__``.
            ActionOutcomeUnknownException: If an order's outcome is unknown, also when the
                chase stopped while an attempt was in the book (e.g. its cancel failed: it
                names that order), or an error that is not the library's (e.g. a signal
                handler's) interrupted it after an order was sent. Check the account (earlier
                attempts may have filled too) before trying again.
        """
        maker_fills: List[tuple] = []  # (size, price) filled by each attempt so far
        # "live": the attempt in the book, until it is canceled and its fill is counted;
        # "sent": whether any order of the chase was sent.
        progress: Dict[str, Any] = {"live": None, "sent": False}
        try:
            return self._maker_chase(symbol, is_buy, size, timeout, reprice_interval, fallback, reduce_only,
                                     bbo_cache, fill_event, maker_fills, progress)
        except ActionOutcomeUnknownException:
            raise  # its contract already: check the account before trying again
        except Exception as exc:
            # The library's own errors come from known steps, so `progress` describes them. Any
            # other one (a signal handler's, a bug) can come from any step, e.g. while an order
            # is being sent or its fill counted: once an order was sent, nothing is certain.
            live, foreign = progress["live"], not isinstance(exc, HyperliquidException)
            if live is None and not maker_fills and not (foreign and progress["sent"]):
                raise
            requested = Decimal(str(self._format_size(symbol, size)))
            filled = sum((qty for qty, _ in maker_fills), Decimal(0))
            if live is not None:
                raise ActionOutcomeUnknownException(
                    f"{symbol} maker order {live.order_id} (cloid {live.cloid})",
                    f"the chase stopped ({exc!r}) while it was in the book: it may still be there or have "
                    f"filled; {filled} of {requested} filled by earlier attempts",
                ) from exc
            if foreign:
                raise ActionOutcomeUnknownException(
                    f"{symbol} maker order",
                    f"the chase was interrupted ({exc!r}) after sending orders: one may be in the book or "
                    f"have filled; {filled} of {requested} counted as filled",
                ) from exc
            raise MakerOrderIncompleteException(symbol, filled, _average_price(maker_fills), requested - filled) from exc

    def _maker_chase(self, symbol: str, is_buy: bool, size: float, timeout: Optional[float],
                     reprice_interval: Optional[float], fallback: str, reduce_only: bool,
                     bbo_cache: Optional[Dict[str, Dict[str, float]]], fill_event: Optional[threading.Event],
                     maker_fills: List[tuple], progress: Dict[str, Any]) -> Order:
        """Body of maker_order; appends each attempt's maker fill to `maker_fills`, and keeps
        in `progress` whether an order was sent and which attempt is in the book."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        if fallback not in ("ioc", "market", "cancel"):
            raise InvalidArgumentException("fallback must be 'ioc', 'market', or 'cancel'")
        # Invalid input (unknown symbol, size below the minimum) raises here. Inside the chase
        # a ValueError means the exchange rejected the post-only order, which is retried.
        self._check_order_size(symbol, size)
        size = self._format_size(symbol, size)  # the size that can be traded, e.g. 0.001009 -> 0.001

        # Auto-detect timing from spread if not specified
        if timeout is None or reprice_interval is None:
            auto_timeout, auto_reprice = self._auto_reprice_interval(symbol, bbo_cache)
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

        def _maker_filled() -> Decimal:
            return sum((qty for qty, _ in maker_fills), Decimal(0))

        def _with_maker_fills(order: Order) -> Order:
            """Report the fills of every maker attempt on the order that is returned."""
            if not maker_fills:
                return order
            parts = list(maker_fills)
            if order.filled_size:
                parts.append((Decimal(str(order.filled_size)), order.average_fill_price))
            order.size = Decimal(str(size))
            order.filled_size = sum((q for q, _ in parts), Decimal(0))
            order.average_fill_price = _average_price(parts)
            return order

        while time.time() < deadline and remaining_size > 0:
            # Get best price for our side
            if bbo_cache is not None and symbol in bbo_cache:
                cached = bbo_cache[symbol]
                best_bid = cached.get("bid")
                best_ask = cached.get("ask")
                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
                spread = best_ask - best_bid if best_bid and best_ask else None
                book = {"best_bid": best_bid, "best_ask": best_ask, "mid_price": mid, "spread": spread}
            else:
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

            # Place post_only order (Alo = Add Limit Only = post-only on HL)
            progress["sent"] = True
            try:
                order = self.create_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=formatted_size,
                    limit_price=limit_price,
                    reduce_only=reduce_only,
                    post_only=False,
                    time_in_force="Alo",
                )
                last_order = order
            except OrderRejectedException as e:
                if not e.reason.startswith(_POST_ONLY_CROSSED):
                    raise  # e.g. insufficient margin: no later attempt can succeed
                # post_only rejected (would cross spread) — retry at next interval
                logger.debug(f"Post-only rejected for {symbol}: {e}")
                time.sleep(min(reprice_interval, max(0.1, deadline - time.time())))
                continue

            # If filled immediately
            if order.status == "filled":
                logger.info(f"Maker order filled immediately: {symbol} {'buy' if is_buy else 'sell'} "
                           f"{order.filled_size} @ {order.average_fill_price}")
                return _annotate(_with_maker_fills(order), is_maker=True)

            # Sleep for reprice interval, then cancel and check result
            progress["live"] = order
            order_id = order.order_id
            sleep_time = min(reprice_interval, max(0.1, deadline - time.time()))
            if fill_event is not None:
                fired = fill_event.wait(timeout=sleep_time)
                if fired:
                    fill_event.clear()
            else:
                time.sleep(sleep_time)

            # Cancel, then read how much this attempt filled. Never reprice or fall back
            # without knowing: an unnoticed fill would be bought or sold a second time.
            self.cancel_order(order_id, symbol)
            try:
                status = self.get_order_status(order_id)
                order_info = status["order"]
                order_detail = order_info["order"]
                state = order_info["status"]
                # The attempt must have ended (filled or canceled), and its sizes must say how
                # much of it filled; "sz" is what was left when it ended.
                if state == "filled":
                    attempt_filled = Decimal(str(formatted_size))
                elif state in ("canceled", "scheduledCancel") or state.endswith("Canceled"):
                    attempt_filled = Decimal(str(order_detail["origSz"])) - Decimal(str(order_detail["sz"]))
                else:
                    raise ValueError(f"order status is {state!r}, not ended")
            except Exception as e:
                raise ActionOutcomeUnknownException(
                    f"{symbol} maker order {order_id}",
                    f"canceled, but how much it filled could not be read ({e}); "
                    f"{_maker_filled()} of {size} filled by earlier attempts",
                ) from e

            # A maker (Alo) order fills at its limit price
            avg_px_str = order_detail.get("avgPx") or order_detail.get("limitPx")
            avg_px = Decimal(str(avg_px_str)) if avg_px_str else None
            if attempt_filled > 0:
                maker_fills.append((attempt_filled, avg_px))
            progress["live"] = None  # canceled, and its fill is counted
            if attempt_filled > 0:
                remaining_size = max(0.0, float(Decimal(str(size)) - _maker_filled()))
                logger.info(f"Maker fill: {attempt_filled} filled, {remaining_size} remaining")
                if remaining_size <= 0:
                    return _annotate(
                        self._make_filled_order(order_id, symbol, is_buy, size, order, _average_price(maker_fills)),
                        is_maker=True,
                    )

        # Timeout reached — apply fallback for remaining size
        if remaining_size > 0 and fallback != "cancel":
            formatted_remaining = self._format_size(symbol, remaining_size)
            if formatted_remaining <= 0:
                if last_order is not None:
                    last_order.status = "canceled"  # every attempt was canceled after its fills
                    return _annotate(_with_maker_fills(last_order), is_maker=True)
            logger.info(f"Maker chase timeout for {symbol}, falling back to {fallback} "
                       f"for remaining {formatted_remaining}")
            progress["sent"] = True
            if fallback == "ioc":
                fallback_order = self.create_order(
                    symbol=symbol,
                    is_buy=is_buy,
                    size=formatted_remaining,
                    limit_price=None,
                    reduce_only=reduce_only,
                    time_in_force="Ioc",
                )
                return _annotate(_with_maker_fills(fallback_order), is_maker=False)
            elif fallback == "market":
                if is_buy:
                    fallback_order = self.buy(symbol, formatted_remaining, reduce_only=reduce_only)
                else:
                    fallback_order = self.sell(symbol, formatted_remaining, reduce_only=reduce_only)
                return _annotate(_with_maker_fills(fallback_order), is_maker=False)

        if last_order is not None:
            last_order.status = "canceled"  # the last attempt was canceled when the chase ended
            return _annotate(_with_maker_fills(last_order), is_maker=False)

        raise RuntimeError(f"maker_order failed: no order placed for {symbol}")

    def maker_buy(
        self,
        symbol: str,
        size: float,
        timeout: Optional[float] = None,
        reprice_interval: Optional[float] = None,
        fallback: str = "ioc",
        reduce_only: bool = False,
        bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
        fill_event: Optional[threading.Event] = None,
    ) -> Order:
        """Place a maker buy order. Convenience wrapper for maker_order().

        See maker_order() for full documentation.
        """
        return self.maker_order(symbol, True, size, timeout, reprice_interval, fallback, reduce_only,
                                bbo_cache=bbo_cache, fill_event=fill_event)

    def maker_sell(
        self,
        symbol: str,
        size: float,
        timeout: Optional[float] = None,
        reprice_interval: Optional[float] = None,
        fallback: str = "ioc",
        reduce_only: bool = False,
        bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
        fill_event: Optional[threading.Event] = None,
    ) -> Order:
        """Place a maker sell order. Convenience wrapper for maker_order().

        See maker_order() for full documentation.
        """
        return self.maker_order(symbol, False, size, timeout, reprice_interval, fallback, reduce_only,
                                bbo_cache=bbo_cache, fill_event=fill_event)

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
            Order: The closing order, filled for the whole position.

        Raises:
            PositionNotFoundException: If no position exists for the symbol
            PositionNotClosedException: If the order filled only part of the position (it is
                immediate-or-cancel: the rest was canceled) or was rejected; it carries the order
                (None if rejected; the rejection is __cause__) and the size still open
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        if position is None:
            positions = self.get_positions()
            position = next((p for p in positions if p.symbol == symbol), None)

        if not position:
            raise PositionNotFoundException(f"No open position found for {symbol}")

        size = abs(Decimal(str(position.size)))
        is_buy = position.size < 0  # Buy to close shorts, sell to close longs

        try:
            order = self.create_order(
                symbol=symbol,
                size=float(size),
                is_buy=is_buy,
                reduce_only=True,
                time_in_force="Ioc",  # Market order
                slippage=slippage,
            )
        except OrderRejectedException as rejection:  # e.g. no liquidity within the slippage
            raise PositionNotClosedException(symbol, None, size, rejection.reason) from rejection

        # Hyperliquid reports a partly filled IOC order as filled too, with the smaller size.
        remaining = size - (order.filled_size or Decimal(0))
        if remaining > 0:
            raise PositionNotClosedException(symbol, order, remaining)
        return order

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        """Cancel all open perp orders on the client's dexes (perp_dexs), stop losses and take
        profits included, optionally of one symbol (then only its dex is read). Spot orders are
        left open: spot_cancel_all_orders() cancels those.

        Args:
            symbol (Optional[str]): If provided, only cancels orders for this symbol

        Raises:
            RuntimeError: If client is not authenticated
            UnknownSymbolException: If symbol is not a market of the client (nothing is read)
            OrdersNotCanceledException: Naming each order that is still open: the exchange did
                not cancel it, or its market is unknown to the client (listed since the client
                was created: refresh_market_specs(), then cancel again). The others are canceled.
            Exception: If fetching orders fails
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        # Let this propagate on failure - trader MUST know if we can't fetch orders
        open_orders = []
        if symbol:  # a misspelled one would match no order and cancel nothing, silently
            self._require_symbol(symbol)
        for dex in self._dexes_of(symbol):
            open_orders.extend(self._with_retry(self.info.open_orders, self.account.public_address, dex=dex))

        # The default dex's open orders include the spot and outcome ones, which stay. Every
        # other order counts as perp: one whose name no pattern knows must not stay open here.
        open_orders = [order for order in open_orders if coin_market(order["coin"]) not in ("spot", "outcome")
                       and (not symbol or order["coin"] == symbol)]

        # An order on a market the client does not know cannot be signed: reported, not skipped.
        failed = [{"symbol": order["coin"], "order_id": order["oid"],
                   "reason": "market unknown to the client; call refresh_market_specs() and cancel again"}
                  for order in open_orders if order["coin"] not in self.info.name_to_coin]
        known = [order for order in open_orders if order["coin"] in self.info.name_to_coin]
        if known:
            try:
                self._cancel_all([{"coin": order["coin"], "oid": order["oid"]} for order in known])  # one request
            except OrdersNotCanceledException as exc:
                failed = exc.failed + failed
        if failed:
            raise OrdersNotCanceledException(failed)

    @staticmethod
    def _parse_open_order(order_data: Dict[str, Any], symbol: str, market: Optional[str] = None) -> Order:
        """An order of a frontendOpenOrders response, the same format for perp and spot:
        trigger orders (stop loss, take profit) with their trigger fields, else limit orders.
        `market` overrides the one derived from the symbol (spot orders are named by token)."""
        # Determine order type (limit, market, trigger)
        order_type = {}
        order_type_str = "unknown"

        # Check if it's a trigger order (stop loss or take profit)
        if order_data.get("isTrigger", False):
            tpsl = None
            name = order_data.get("orderType") or ""  # Stop Market, Stop Limit, Take Profit Market/Limit
            if "Take Profit" in order_data.get("orderType", ""):
                tpsl = "tp"
                order_type_str = "take_profit"
            elif "Stop" in order_data.get("orderType", ""):
                tpsl = "sl"
                order_type_str = "stop_loss"

            order_type["trigger"] = {
                "triggerPx": order_data.get("triggerPx"),
                "isMarket": "Limit" not in name,
                "tpsl": tpsl
            }
        else:
            order_type["limit"] = {
                "tif": order_data.get("tif", "Gtc"),
                "px": order_data.get("limitPx")
            }
            order_type_str = "limit"

        is_buy = order_data.get("side") == "B"
        # "sz" is what is still open, "origSz" the size the order was placed with
        remaining = Decimal(str(order_data.get("sz", 0)))
        original = Decimal(str(order_data.get("origSz", remaining)))

        order_dict = {
            "order_id": order_data["oid"],
            "cloid": order_data.get("cloid"),
            "symbol": symbol,
            "is_buy": is_buy,
            "size": original,
            "order_type": order_type,
            "reduce_only": order_data.get("reduceOnly", False),
            "status": "open",
            "time_in_force": order_data.get("tif", "Gtc") or "Gtc",
            "created_at": order_data.get("timestamp", 0),
            "filled_size": original - remaining,
            "type": order_type_str,
            "children": order_data.get("children") or None,
            "is_position_tpsl": order_data.get("isPositionTpsl"),
            "trigger_condition": order_data.get("triggerCondition"),
        }

        if "limitPx" in order_data:
            order_dict["limit_price"] = Decimal(str(order_data["limitPx"]))
        if order_data.get("isTrigger") and "triggerPx" in order_data:  # else "0.0", a placeholder
            order_dict["trigger_price"] = Decimal(str(order_data["triggerPx"]))
        if market is not None:
            order_dict["market"] = market
        return from_dict(data_class=Order, data=order_dict, config=DACITE_CONFIG)

    def get_open_orders(self, symbol: Optional[str] = None, *, address: Optional[str] = None) -> List[Order]:
        """Get all open orders of a wallet, by default the client's account.

        Args:
            symbol (Optional[str]): If provided, only returns orders for this symbol (and reads
                only its dex)
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            List[Order]: List of open orders, on the client's dexes (spot orders come with the
                default dex)

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        # Get open orders from the API - let errors propagate
        open_orders_response = []
        for dex in self._dexes_of(symbol):
            open_orders_response.extend(self._with_retry(
                self.info.frontend_open_orders, address, dex=dex
            ))

        # Filter by symbol if provided
        if symbol:
            open_orders_response = [order for order in open_orders_response if order["coin"] == symbol]

        # Convert API response to our Order model
        return [self._parse_open_order(order_data, order_data.get("coin", "")) for order_data in open_orders_response]

    def get_price(self, symbol: Optional[str] = None) -> Union[float, Dict[str, float]]:
        """Get current price(s), the mids. No authentication required. For one symbol only its
        dex's mids are read, as the SDK reads a market order's price; without one, those of
        every dex of the client (spot mids come with the default dex)."""
        prices: Dict[str, float] = {}
        for dex in self._dexes_of(symbol):
            response = self._with_retry(self.info.all_mids, dex=dex)
            prices.update({sym: float(price) for sym, price in response.items()})

        if symbol is not None:
            if symbol not in prices:
                raise self._unknown_symbol(symbol)
            return prices[symbol]

        return prices

    def get_account_mode(self, address: Optional[str] = None) -> str:
        """How an account holds its collateral.

        Returns:
            "standard": spot and each perp dex have their own balance.
            "unified": one balance per token in the spot account, shared by spot and the perp
                positions of every dex.
            "portfolio_margin": unified, plus borrowing against collateral.
            "unknown": "dexAbstraction" (discontinued by Hyperliquid) or a mode this library
                does not know.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)
        mode = self._with_retry(self.info.query_user_abstraction_state, address)
        return _ACCOUNT_MODES.get(mode, "unknown") if isinstance(mode, str) else "unknown"

    def get_user_role(self, address: Optional[str] = None) -> Dict[str, Any]:
        """What an address is on Hyperliquid (the SDK's user_role), as Hyperliquid answers it:
        {"role": "user"}, {"role": "agent", "data": {"user": <the account it trades for>}},
        {"role": "vault"}, {"role": "subAccount", "data": {"master": <its master>}}, or
        {"role": "missing"} for an address Hyperliquid does not know (e.g. an API wallet that
        expired or was removed).

        An order signed with an API wallet's key trades the account in "data.user": check it
        before trading with a key whose account you did not set up yourself.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        return self._with_retry(self.info.user_role, self._address(address))

    def get_leverage(self, symbol: str, *, address: Optional[str] = None) -> Leverage:
        """The leverage a wallet uses on a perp market: cross or isolated margin and its value,
        as set with set_leverage (or in the Hyperliquid app), else the market's default.
        Hyperliquid's documented activeAssetData request. No authentication required.

        Raises:
            UnknownSymbolException: If the symbol is not a market of the client (no request)
            AddressRequiredException: Without address on a client without an account
            HyperliquidException: If Hyperliquid answers in no documented format
        """
        self._require_symbol(symbol)
        answer = self._with_retry(self.info.post, "/info",
                                  {"type": "activeAssetData", "user": self._address(address), "coin": symbol})
        leverage = answer.get("leverage") if isinstance(answer, dict) else None
        kind = leverage.get("type") if isinstance(leverage, dict) else None
        value = leverage.get("value") if isinstance(leverage, dict) else None
        if kind not in ("cross", "isolated") or isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise HyperliquidException(f"activeAssetData for {symbol} answered in no documented format: {answer!r}")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            number = Decimal("NaN")
        if not number.is_finite() or number <= 0:
            raise HyperliquidException(f"activeAssetData for {symbol} answered a leverage of {value!r}")
        return Leverage(type=kind, value=number)

    def get_fee_rates(self, symbol: str, *, address: Optional[str] = None) -> FeeRates:
        """The fee rates a wallet pays on a perp market, as fractions of an order's value
        (0.00045 is 0.045 %): taker for an order that takes liquidity (a market order, a
        triggered stop), maker for one that rests and adds it. Hyperliquid's documented
        userFees request: userCrossRate and userAddRate, the wallet's tier and discounts.

        On a HIP-3 dex they are scaled as Hyperliquid's fee documentation describes, by the
        market's growthMode and deployerFeeScale s: growth mode cuts the protocol's fees and
        rebates to a tenth; the deployer adds s times the protocol's fee (s at most 1), or,
        above 1, the protocol's fee is raised to the deployer's, so 2 × s times. A maker rebate
        (a negative rate) is only cut by growth mode. No authentication required.

        Raises:
            UnknownSymbolException: If the symbol is not a market of the client (no request)
            AddressRequiredException: Without address on a client without an account
            HyperliquidException: If Hyperliquid answers in no documented format
        """
        self._require_symbol(symbol)
        spec = self.market_specs.get(symbol, {})
        answer = self._with_retry(self.info.post, "/info", {"type": "userFees", "user": self._address(address)})
        try:
            taker, maker = Decimal(str(answer["userCrossRate"])), Decimal(str(answer["userAddRate"]))
        except (KeyError, TypeError, InvalidOperation):
            raise HyperliquidException(f"userFees answered in no documented format: {answer!r}") from None
        if not taker.is_finite() or not maker.is_finite():
            raise HyperliquidException(f"userFees answered rates that are no numbers: {answer!r}")
        dex = _sdk_dex(symbol) or None
        growth = bool(spec.get("growth_mode"))
        scale_text = spec.get("deployer_fee_scale")
        scale = None
        if dex is not None:  # every HIP-3 market has one (2026-11): without it the fee is not established
            try:
                scale = Decimal(str(scale_text)) if scale_text is not None else None
            except InvalidOperation:
                scale = None
            if scale is None or not scale.is_finite() or scale < 0:
                raise HyperliquidException(f"{symbol}: deployerFeeScale {scale_text!r} is no fee scale, so its "
                                           "fee rates are not established")

        def scaled(rate: Decimal) -> Decimal:
            if dex is None:
                return rate
            protocol = rate / 10 if growth else rate
            if rate < 0 or scale is None:  # a rebate: only growth mode applies (scale: set on every HIP-3 dex)
                return protocol
            return protocol * (1 + scale) if scale <= 1 else protocol * 2 * scale

        return FeeRates(symbol=symbol, taker=scaled(taker), maker=scaled(maker), dex=dex,
                        growth_mode=growth if dex is not None else False, deployer_fee_scale=scale)

    def get_perp_dexs(self) -> List[Dict[str, Any]]:
        """The HIP-3 dexes of the network (the SDK's perp_dexs), the names perp_dexs= takes:
        [{"name": "xyz", "full_name": "XYZ"}, ...], in Hyperliquid's order. The default dex is
        not listed. Every dex Hyperliquid lists is in it, also one whose markets are all
        delisted. No authentication required."""
        return [{"name": dex["name"], "full_name": dex.get("fullName")}
                for dex in self._with_retry(self.info.perp_dexs) if dex]

    def _balance_mode(self, address: str) -> str:
        """The account mode, if a single balance is correct for it ("standard" or "unified")."""
        mode = self.get_account_mode(address)
        if mode not in ("standard", "unified"):
            raise UnsupportedAccountModeException(mode)
        return mode

    def _spot_usdc_total(self, address: str) -> Decimal:
        """USDC in the spot account. In unified mode this is the perp collateral, and it
        already includes the unrealized PnL of every perp position."""
        response = self._with_retry(self.info.spot_user_state, address)
        for balance in response.get("balances", []):
            if balance.get("coin") == "USDC":
                return Decimal(str(balance.get("total", "0")))
        return Decimal("0")

    def get_perp_balance(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """Get the balance that perp positions trade against, including their unrealized PnL.

        Standard accounts: the account value of the perp account (summed over the client's
        perp dexes, each holds its own collateral). Unified accounts: the USDC in the spot
        account, which is shared with spot and already includes the PnL; the perp account
        figures are only the part of it backing positions.

        Args:
            address (Optional[str]): The address to get balance for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the balance. If False, returns detailed information.

        Returns:
            Union[Decimal, Dict[str, Any]]: If simple=True (default), returns just the balance.
                                          If simple=False, returns a dictionary containing:
                                          - balance: The balance in USD
                                          - positions: List of open positions
                                          - margin_used: Current margin usage
                                          - state: Full user state data

        Raises:
            UnsupportedAccountModeException: For portfolio-margin accounts and unknown account modes.
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        return self._perp_balance(address, self._balance_mode(address), simple)

    def _perp_balance(self, address: str, mode: str, simple: bool,
                      spot_usdc: Optional[Decimal] = None) -> Union[Decimal, Dict[str, Any]]:
        """`spot_usdc`: the spot USDC already read, so that results agree with that read."""
        user_state = None if simple and mode == "unified" else self.get_user_state(address)
        if mode == "unified":
            balance = self._spot_usdc_total(address) if spot_usdc is None else spot_usdc
        else:
            balance = user_state.margin_summary.account_value

        if simple:
            return balance

        return {
            'balance': balance,
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

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        # Get spot user state
        response = self._with_retry(self.info.spot_user_state, address)

        # Get current prices for all tokens (use provided prices or fetch fresh)
        if prices is None:
            prices = self.get_price()
        
        # Process balances
        total_balance = Decimal('0')
        token_balances: Dict[str, SpotTokenBalance] = {}
        
        for balance in response.get('balances', []):
            token = balance.get('coin')
            if not token:
                continue
                
            # Get token amount
            token_amount = Decimal(str(balance.get('total', '0')))
            if token_amount == 0:
                continue
                
            # Price from the perp market; without one, from the token's USDC spot pair
            # (spot mids are keyed by pair id, e.g. "@107"); stablecoins default to $1
            token_price = Decimal(str(prices.get(token, '0')))
            if token_price == 0:
                spot_coin = self.info.name_to_coin.get(f"{token}/USDC")
                if spot_coin:
                    token_price = Decimal(str(prices.get(spot_coin, '0')))
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
            token: Token name (e.g., "PURR", "HYPE") for its USDC pair, or a full pair name
                (e.g., "HYPE/USDT0")

        Returns:
            The pair name (e.g., "PURR/USDC") that exists in info.name_to_coin.

        Raises:
            ValueError: If no spot pair found for the token.
        """
        pair = f"{token}/USDC"
        if pair in self.info.name_to_coin:
            return pair
        if "/" in token and token in self.info.name_to_coin:
            return token  # a full pair name, e.g. "HYPE/USDT0"
        raise UnknownSymbolException(
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
        return _round_down(size, sz_decimals)

    def transfer_to_spot(self, amount: float) -> dict:
        """Transfer USDC from perp wallet to spot wallet.

        Note: This does not work with API wallets. Use the main wallet's private key.

        Args:
            amount (float): Amount of USDC to transfer.

        Returns:
            dict: API response.
        """
        return self._usd_class_transfer(amount, to_perp=False)

    def transfer_to_perp(self, amount: float) -> dict:
        """Transfer USDC from spot wallet to perp wallet.

        Note: This does not work with API wallets. Use the main wallet's private key.

        Args:
            amount (float): Amount of USDC to transfer.

        Returns:
            dict: API response.
        """
        return self._usd_class_transfer(amount, to_perp=True)

    def _usd_class_transfer(self, amount: float, to_perp: bool) -> dict:
        """Shared implementation of transfer_to_spot and transfer_to_perp."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        if amount <= 0:
            raise InvalidArgumentException("Amount must be positive")
        action = "transfer to perp" if to_perp else "transfer to spot"
        result = self._signed(action, self.exchange.usd_class_transfer, amount, to_perp)
        if isinstance(result, dict) and result.get("status") == "err" and "Must deposit" in str(result.get("response")):
            # Hyperliquid's answer to a transfer signed by an API wallet
            raise ActionRejectedException(action, f"{result['response']} (API wallets cannot "
                                          "transfer funds; use the main wallet's private key)")
        self._action_result(result, action)
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
            raise HyperliquidException(f"Hyperliquid has no price for {token} (pair {pair_name}, coin {coin})")
        return float(price_str)

    def create_spot_order(
        self,
        token: str,
        size: float,
        is_buy: bool,
        limit_price: Optional[float] = None,
        post_only: bool = False,
        time_in_force: Optional[Literal["Gtc", "Ioc", "Alo"]] = None,
        slippage: Optional[float] = None,
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Create a spot order.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").
            size (float): Order size in token units.
            is_buy (bool): True for buy, False for sell.
            limit_price (Optional[float]): Price for limit orders. None for market.
            post_only (bool): Maker only (limit orders only).
            time_in_force (Optional[str]): "Gtc", "Ioc", or "Alo". Defaults to "Ioc" for a
                market order (it never rests on the book), else "Gtc", as in create_order().
            slippage (Optional[float]): Slippage for market orders.
            cloid (Optional[str]): Client order id, see create_order().

        Returns:
            Order: Order response.

        Raises:
            InvalidArgumentException: If size or limit_price is not a positive number, or
                either is too small for the pair; checked before anything is sent.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        order_cloid = _order_cloid(cloid)
        if time_in_force is None:
            time_in_force = "Ioc" if limit_price is None else "Gtc"
        time_in_force = self._resolve_time_in_force(time_in_force, post_only)
        if not _positive_number(size):
            raise InvalidArgumentException(f"size must be a positive number, got {size!r}")
        if limit_price is not None and not _positive_number(limit_price):
            raise InvalidArgumentException(f"limit_price must be a positive number, got {limit_price!r}")

        pair_name = self._resolve_spot_pair(token)

        # Market order: get price and apply slippage
        if limit_price is None:
            current_price = self.get_spot_price(token)
            if current_price <= 0:
                raise HyperliquidException(f"Invalid current price for {token}: {current_price}")
            limit_price = self._with_slippage(current_price, is_buy, slippage)

        # Format size and price for spot
        size = self._format_spot_size(pair_name, size)
        limit_price = self._format_spot_price(pair_name, limit_price)
        if not _positive_number(limit_price):  # rounds to 0, or overflows to infinity
            raise InvalidArgumentException(f"price rounds to {limit_price} at {token}'s price precision")

        sz_decimals = self._get_spot_sz_decimals(pair_name)
        min_size = 1.0 / (10 ** sz_decimals) if sz_decimals > 0 else 1
        if size < min_size:
            raise InvalidArgumentException(f"Minimum size for {token} is {min_size} (szDecimals={sz_decimals})")

        order_type = {"limit": {"tif": time_in_force}}

        logger.debug(f"Spot order: pair={pair_name}, size={size}, price={limit_price}, buy={is_buy}")

        try:
            response = self._place_order(
                pair_name,
                order_cloid,
                is_buy=is_buy,
                sz=size,
                limit_px=limit_price,
                order_type=order_type,
                reduce_only=False,
            )

            logger.debug(f"Spot order response: {response}")
            return self._order_from_response(response, token, is_buy, size, limit_price, order_type, False,
                                             f"{token} spot order", market="spot", cloid=order_cloid)

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
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Buy a spot token.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").
            size (float): Amount of tokens to buy.
            limit_price (Optional[float]): Price for limit order. None for market order.
            post_only (bool): Maker only (limit orders only).
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage.
            cloid (Optional[str]): Client order id, see create_order().

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
            cloid=cloid,
        )

    def spot_sell(
        self,
        token: str,
        size: float,
        limit_price: Optional[float] = None,
        post_only: bool = False,
        slippage: Optional[float] = None,
        *,
        cloid: Optional[str] = None,
    ) -> Order:
        """Sell a spot token.

        Args:
            token (str): Token name (e.g., "PURR", "HYPE").
            size (float): Amount of tokens to sell.
            limit_price (Optional[float]): Price for limit order. None for market order.
            post_only (bool): Maker only (limit orders only).
            slippage (Optional[float]): Slippage for market orders. Overrides default_slippage.
            cloid (Optional[str]): Client order id, see create_order().

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
            cloid=cloid,
        )

    def spot_cancel_order(self, order_id: Union[str, int], token: str) -> bool:
        """Cancel a specific spot order.

        Args:
            order_id (Union[str, int]): The order ID to cancel.
            token (str): Token name (e.g., "PURR").

        Returns:
            bool: True if the order was canceled, False if it is not open: never placed,
                already canceled or filled.

        Raises:
            AuthenticationRequiredException: If the client has no account
            OrderException: If the exchange answered, but did not cancel the order
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        oid = _order_id(order_id)
        return self._cancel_one(self._resolve_spot_pair(token), oid)

    def spot_cancel_all_orders(self, token: Optional[str] = None) -> None:
        """Cancel all open spot orders.

        Args:
            token (Optional[str]): If provided, only cancels orders for this token.
                                   If None, cancels all spot orders.

        Raises:
            RuntimeError: If any cancellation fails.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

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
        self._cancel_all([{"coin": order["coin"], "oid": order["oid"]} for order in spot_orders])

    def _spot_coin_symbols(self) -> Dict[str, str]:
        """Map each spot coin ("PURR/USDC", "@107") to the symbol this library reports for it.

        Pairs quoted in USDC report the base token name ("PURR"); other pairs report
        "BASE/QUOTE" ("HYPE/USDT0"). Tokens are looked up by their `index` field, which is
        not their position in the list.
        """
        meta = self._with_retry(self.info.spot_meta)
        token_names = {token["index"]: token["name"] for token in meta["tokens"]}
        symbols: Dict[str, str] = {}
        for pair in meta["universe"]:
            base, quote = (token_names[index] for index in pair["tokens"])
            symbols[pair["name"]] = _spot_symbol(base, quote)
        return symbols

    def get_spot_markets(self) -> List[SpotMarket]:
        """Every spot pair with its current figures: mark, mid and previous-day price and the
        day volume, in the quote token. One request.

        The symbol is the one the spot methods take and report: the base token for a pair
        quoted in USDC ("HYPE"), else "BASE/QUOTE" ("HYPE/USDT0").

        Returns:
            List[SpotMarket]: In Hyperliquid's order of the pairs.
        """
        meta, contexts = self._with_retry(self.info.spot_meta_and_asset_ctxs)
        tokens = {token["index"]: token for token in meta["tokens"]}
        # Matched by coin, never by position: Hyperliquid lists the contexts by pair index,
        # pairs no longer listed included (1190 contexts for 351 pairs, 2026-11-14).
        by_coin = {context["coin"]: context for context in contexts}
        markets = []
        for pair in meta["universe"]:
            base, quote = (tokens[index] for index in pair["tokens"])
            context = by_coin[pair["name"]]
            markets.append(SpotMarket(
                symbol=_spot_symbol(base["name"], quote["name"]),
                coin=pair["name"],
                base=base["name"],
                quote=quote["name"],
                size_decimals=base["szDecimals"],
                mark_price=_decimal(context.get("markPx")),
                mid_price=_decimal(context.get("midPx")),
                previous_day_price=_decimal(context.get("prevDayPx")),
                day_volume=_decimal(context.get("dayNtlVlm")),
            ))
        return markets

    def get_spot_open_orders(self, token: Optional[str] = None, *, address: Optional[str] = None) -> List[Order]:
        """Get open spot orders.

        Args:
            token (Optional[str]): If provided, only returns orders for this token.
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            List[Order]: List of open spot orders.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        if token:
            pair = self._resolve_spot_pair(token)  # e.g. "PURR/USDC" for "PURR" or "PURR/USDC"
            base, quote = pair.split("/", 1)
            # Reported under the same symbol as without a filter: "PURR", not the "PURR/USDC" asked for.
            spot_coin_to_token = {self.info.name_to_coin[pair]: _spot_symbol(base, quote)}
        else:
            spot_coin_to_token = self._spot_coin_symbols()

        response = self._with_retry(self.info.frontend_open_orders, address)

        orders = []
        for order_data in response:
            coin = order_data.get("coin", "")
            if coin not in spot_coin_to_token:
                continue

            order = self._parse_open_order(order_data, spot_coin_to_token[coin], market="spot")
            orders.append(order)

        return orders

    def get_spot_order_book(self, token: str, *, decimal: bool = False) -> Dict[str, Any]:
        """Get the order book for a spot token.

        Args:
            token (str): Token name (e.g., "FRAC", "HYPE").
            decimal (bool): Prices and sizes as Decimal instead of float (see get_order_book).

        Returns:
            Dict[str, Any]: Order book data (same format as get_order_book).
        """
        pair_name = self._resolve_spot_pair(token)
        return self.get_order_book(pair_name, decimal=decimal)

    def get_spot_fills(self, token: Optional[str] = None, *, address: Optional[str] = None) -> list:
        """Get recent spot fills of a wallet, by default the client's account.

        Args:
            token (Optional[str]): If provided, only returns fills for this token.
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            List[Fill]: List of spot fill objects.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        spot_coin_to_token = self._spot_coin_symbols()
        if token and token.endswith("/USDC"):
            token = token[: -len("/USDC")]  # USDC pairs are reported by their token name

        raw = self._with_retry(self.info.user_fills, address)
        fills = []
        for f in raw:
            fill = self._parse_fill(f)
            if fill.market != "spot":
                continue
            # Named as the other spot methods name it; a pair no longer listed keeps
            # Hyperliquid's name (e.g. "@98") instead of being dropped.
            fill.symbol = spot_coin_to_token.get(f["coin"], f["coin"])
            if token and fill.symbol != token:
                continue
            fills.append(fill)
        return fills

    def get_account_summary(self, address: Optional[str] = None) -> AccountSummary:
        """An account's balances by its account mode: every collateral balance,
        every perp dex with its positions, and the totals that hold for the mode.

        - unified: the spot balances are the collateral of every dex; account_value is their
          USD value (USDC already includes the PnL of open positions), available_to_trade is
          USDC total minus hold. Per-dex account value and withdrawable are None: the dex
          figures are only the part of the collateral backing its positions.
        - standard: each dex has its own collateral; account_value adds every dex and the spot
          balances, available_to_trade is None (see each dex's withdrawable).
        - portfolio margin and unknown modes: balances and dexes only, every total None.

        Outcome balances (HIP-4) and tokens without a price are listed unpriced and left out
        of the totals; sources["unpriced"] names them. Only this client's dexes are read
        (perp_dexs; sources["dexes"]): positions and margin on
        other HIP-3 dexes are not in the summary, although in unified accounts their PnL is in
        the USDC. The figures come from separate requests (about 2 + 2 per dex), a moment apart.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)
        mode = self.get_account_mode(address)
        balances = [_collateral(balance) for balance in self.get_spot_balance(address, simple=False).tokens.values()]
        own_collateral = mode == "standard"  # otherwise the spot account holds every dex's collateral
        dexes = []
        for dex in self.perp_dexs:
            state = self._get_user_state_single(address, dex)
            dexes.append(DexAccount(
                dex=dex or None,
                account_value=state.margin_summary.account_value if own_collateral else None,
                withdrawable=state.withdrawable if own_collateral else None,
                margin_used=state.margin_summary.total_margin_used,
                cross_maintenance_margin_used=state.cross_maintenance_margin_used,
                positions=[asset.position for asset in state.asset_positions],
            ))
        scope = {"dexes": ", ".join(dex or "default" for dex in self.perp_dexs) + " (other dexes are not read)"}
        unpriced = [b.token for b in balances if b.usd_value is None]
        if unpriced:
            scope["unpriced"] = ", ".join(unpriced) + " (not in account_value)"
        priced = sum((b.usd_value for b in balances if b.usd_value is not None), Decimal(0))
        margin_used = sum((d.margin_used for d in dexes), Decimal(0))
        if mode == "unified":
            usdc = next((b for b in balances if b.token == "USDC"), None)
            return AccountSummary(
                mode=mode, account_value=priced, available_to_trade=usdc.available if usdc else Decimal(0),
                margin_used=margin_used, balances=balances, dexes=dexes, sources={
                    "account_value": "spot balances at USD prices; USDC includes the PnL of open positions",
                    "available_to_trade": "USDC total minus hold",
                    "margin_used": "totalMarginUsed of the dexes read",
                    **scope,
                })
        if mode == "standard":
            return AccountSummary(
                mode=mode, account_value=sum((d.account_value or Decimal(0) for d in dexes), priced),
                available_to_trade=None, margin_used=margin_used, balances=balances, dexes=dexes, sources={
                    "account_value": "accountValue of every dex plus the spot balances at USD prices",
                    "available_to_trade": "none for the account: each dex has its own collateral (see withdrawable)",
                    "margin_used": "totalMarginUsed of the dexes read",
                    **scope,
                })
        return AccountSummary(
            mode=mode, account_value=None, available_to_trade=None, margin_used=None, balances=balances,
            dexes=dexes, sources={"totals": f"not established for {mode} accounts; see balances and dexes", **scope})

    def get_all_balances(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """Get the total value of an account: perp plus spot balance for standard accounts; the
        spot balance alone for unified accounts, whose perp collateral is the spot USDC.

        Args:
            address (Optional[str]): The address to get balances for. If None, uses authenticated user's address.
            simple (bool): If True (default), returns just the total balance. If False, returns detailed information.

        Returns:
            Union[Decimal, Dict[str, Any]]: If simple=True (default), returns just the total balance in USD.
                                          If simple=False, returns a dictionary containing:
                                          - total_balance: Total account value in USD
                                          - account_mode: "standard" or "unified"
                                          - perp: get_perp_balance(simple=False); in unified mode its
                                            balance is part of the spot balance, not added to it
                                          - spot: get_spot_balance(simple=False)

        Raises:
            UnsupportedAccountModeException: For portfolio-margin accounts and unknown account modes.
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        mode = self._balance_mode(address)
        spot = self.get_spot_balance(address, simple=simple)
        if simple:
            return spot if mode == "unified" else self._perp_balance(address, mode, True) + spot

        usdc = spot.tokens.get("USDC")
        perp = self._perp_balance(address, mode, False, spot_usdc=usdc.amount if usdc else Decimal("0"))
        perp_value = perp['balance'] if mode == "standard" else Decimal("0")
        return {
            'total_balance': perp_value + spot.total_balance,
            'account_mode': mode,
            'perp': perp,
            'spot': spot,
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
                raise self._unknown_symbol(symbol)
            return market

        return markets

    def get_markets(self) -> List[Market]:
        """Every perp market of this client's dexes (perp_dexs, e.g. also xyz:), with
        its current figures: mark, mid and oracle price, funding, open interest, volume, and
        whether it is delisted or at its open interest cap. Two requests per dex.

        Returns:
            List[Market]: In Hyperliquid's order, delisted markets included.
        """
        markets: List[Market] = []
        for dex in self.perp_dexs:
            with_contexts = self._markets_with_contexts(self.info, dex)
            capped = set(self._with_retry(self.info.post, "/info", {"type": "perpsAtOpenInterestCap", "dex": dex}))
            for market, context in with_contexts:
                markets.append(Market(
                    symbol=market["name"],
                    dex=dex or None,
                    size_decimals=market["szDecimals"],
                    max_leverage=market["maxLeverage"],
                    margin_mode=_margin_mode(market),
                    delisted=bool(market.get("isDelisted", False)),
                    at_open_interest_cap=(market["name"] in capped
                                          or (bool(dex) and market["name"].split(":", 1)[-1] in capped)),
                    mark_price=_decimal(context.get("markPx")),
                    mid_price=_decimal(context.get("midPx")),
                    oracle_price=_decimal(context.get("oraclePx")),
                    previous_day_price=_decimal(context.get("prevDayPx")),
                    funding=_decimal(context.get("funding")),
                    premium=_decimal(context.get("premium")),
                    open_interest=_decimal(context.get("openInterest")),
                    day_volume_usd=_decimal(context.get("dayNtlVlm")),
                ))
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

        Raises:
            UnknownSymbolException: If symbol is not a market of this client.
            HyperliquidException: If Hyperliquid has no predicted funding rate for symbol.
        """
        # predictedFundings: [[coin, [[venue, {"fundingRate": ..., ...}], ...]], ...]
        rates = self._with_retry(self.info.post, "/info", {"type": "predictedFundings"})
        market_names = {market['name'] for market in self.get_market_info()}
        if symbol is not None and symbol not in market_names:
            raise self._unknown_symbol(symbol)

        funding_data = []
        for coin, venues in rates:
            if coin not in market_names:
                continue
            funding_rate = next((info["fundingRate"] for venue, info in venues if venue == "HlPerp"), None)
            if funding_rate is not None:
                funding_data.append({'symbol': coin, 'funding_rate': float(funding_rate)})

        # If specific symbol requested, return just that rate
        if symbol is not None:
            rate = next((data['funding_rate'] for data in funding_data if data['symbol'] == symbol), None)
            if rate is None:
                raise HyperliquidException(f"Hyperliquid has no predicted funding rate for {symbol}")
            return rate

        # Apply threshold filter if provided
        if threshold is not None:
            funding_data = [
                data for data in funding_data
                if abs(data['funding_rate']) >= abs(threshold)
            ]

        # Sort by funding rate: highest positive to lowest negative
        funding_data.sort(key=lambda x: x['funding_rate'], reverse=True)

        return funding_data

    def cancel_all(self) -> None:
        """Cancel all open orders across all symbols.

        Alias for cancel_all_orders(). Prefer cancel_all_orders() for new code.
        """
        return self.cancel_all_orders()

    def _markets_with_contexts(self, info: Info, dex: str) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """(market, context) for each market of `dex`, in Hyperliquid's order, from one
        metaAndAssetCtxs request. Failures (after the retries) and answers that are not in the
        documented format ([meta, one context per market]) are raised as they are."""
        meta, contexts = self._with_retry(info.post, "/info", {"type": "metaAndAssetCtxs", "dex": dex})
        return list(zip(meta["universe"], contexts, strict=True))

    def _fetch_market_specs(self, info: Optional[Info] = None) -> Dict[str, Dict]:
        """Fetch current market specifications from the API using meta_and_asset_ctxs for richer
        data, through `info` (default: the client's)."""
        info = self.info if info is None else info
        specs = {}

        for dex in self.perp_dexs:
            # Every dex or none: a skipped dex would leave its markets unknown, or formatted with
            # wrong size decimals.
            for market, ctx in self._markets_with_contexts(info, dex):
                specs[market["name"]] = {
                    "size_decimals": market["szDecimals"],
                    "price_decimals": market.get('px_dps', 1),
                    "max_leverage": market["maxLeverage"],
                    "only_isolated": market.get('onlyIsolated', False),
                    "margin_mode": _margin_mode(market),
                    # HIP-3 markets: fees in growth mode are a tenth; the deployer's share on top
                    "growth_mode": market.get("growthMode") == "enabled",
                    "deployer_fee_scale": market.get("deployerFeeScale"),
                    "funding": ctx.get("funding"),
                    "open_interest": ctx.get("openInterest"),
                    "mark_price": ctx.get("markPx"),
                }

        if not specs:
            raise RuntimeError("Failed to fetch market specs from any dex")
        return specs

    def refresh_market_specs(self) -> Dict[str, Dict]:
        """Reload the market metadata, so markets listed since the client was created can be
        traded: the SDK's symbol tables (a new Info, built as when the client is created) and
        the market specs (size decimals, max leverage, margin mode).

        Nothing reloads on its own; call this in a long-running program when markets were
        listed. The client changes only once everything is loaded: a failure raises
        (NetworkException, ...) and leaves the client as it was.

        Returns:
            Dict[str, Dict]: The new market specs.
        """
        info = self._new_info()
        meta, spot_meta = self._startup_metadata(info)
        specs = self._fetch_market_specs(info)
        # The previous Info is not closed: a request of another thread may still use it.
        self.info, self._meta, self._spot_meta = info, meta, spot_meta
        if hasattr(self, "exchange"):
            self.exchange.info = info
        self._store_market_specs(specs)
        return specs

    def _store_market_specs(self, specs: Dict[str, Dict]) -> None:
        """Use freshly fetched specs, and share them if the cache holds this client's network
        and dex list."""
        self.market_specs = specs
        self._market_specs_fetched_at = time.time()
        with HyperliquidClient._cache_lock:
            if HyperliquidClient._cached_key == self._cache_key:
                HyperliquidClient._cached_meta = self._meta
                HyperliquidClient._cached_spot_meta = self._spot_meta
                HyperliquidClient._cached_market_specs = specs
                HyperliquidClient._cached_market_specs_at = self._market_specs_fetched_at

    def get_stop_loss_price(self, symbol: str, *, address: Optional[str] = None) -> Optional[Decimal]:
        """Get the stop loss price for a specific symbol.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            Optional[Decimal]: The stop loss price if a stop loss order exists, None otherwise

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        # Get all open orders for the symbol
        open_orders = self.get_open_orders(symbol, address=address)
        
        # Filter for stop loss orders
        sl_orders = [order for order in open_orders if order.type == "stop_loss"]
        
        if not sl_orders:
            return None
            
        # Return the price of the first stop loss order
        # If there are multiple stop loss orders, this returns the first one
        return sl_orders[0].trigger_price
    
    def get_take_profit_price(self, symbol: str, *, address: Optional[str] = None) -> Optional[Decimal]:
        """Get the take profit price for a given symbol.

        Args:
            symbol (str): The trading symbol to check
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            Optional[Decimal]: The trigger price of the first take profit order found, or None if no take profit order exists

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        orders = self.get_open_orders(symbol, address=address)
        tp_orders = [order for order in orders if order.type == "take_profit"]
        
        if not tp_orders:
            return None
            
        return tp_orders[0].trigger_price
        
    def has_position(self, symbol: str, *, address: Optional[str] = None) -> bool:
        """Check if a wallet has an open position for the given symbol.

        Args:
            symbol (str): The trading symbol to check
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            bool: True if a position exists, False otherwise

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        positions = self.get_positions(address=address)
        return any(p.symbol == symbol and p.size != 0 for p in positions)
    
    def get_position_size(self, symbol: str, *, address: Optional[str] = None) -> Optional[Decimal]:
        """Get the size of a position for a given symbol.

        Args:
            symbol: The trading symbol (e.g., "BTC")
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            The position size as a Decimal, or None if no position exists

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        positions = self.get_positions(address=address)
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            return None
        return position.size
        
    def get_position_direction(self, symbol: str, *, address: Optional[str] = None) -> Optional[str]:
        """Get the direction of a position for a given symbol.

        Args:
            symbol: The trading symbol (e.g., "BTC")
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            "long" if position size is positive, "short" if negative, None if no position exists

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        position_size = self.get_position_size(symbol, address=address)
        if position_size is None:
            return None
        return "long" if float(position_size) > 0 else "short"
        
    def has_active_orders(self, symbol: Optional[str] = None, *, address: Optional[str] = None) -> bool:
        """Check if there are any active orders for the given symbol.

        Args:
            symbol (Optional[str]): The trading symbol to check
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            bool: True if there are active orders, False otherwise

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        orders = self.get_open_orders(symbol, address=address)
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
            raise AuthenticationRequiredException()
            
        # Get current price
        current_price = Decimal(str(self.get_price(symbol)))
        
        # Calculate risk per unit
        if current_price > stop_loss_price:  # Long position
            risk_per_unit = current_price - stop_loss_price
        elif current_price < stop_loss_price:  # Short position
            risk_per_unit = stop_loss_price - current_price
        else:
            raise InvalidArgumentException("Stop loss price cannot be equal to current price")
            
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
        order_id: Union[int, str],
        symbol: str,
        is_buy: bool,
        size: float,
        price: float,
        order_type: Dict,
        reduce_only: bool = False
    ) -> Order:
        """Modify an existing order.
        
        Args:
            order_id (Union[int, str]): The ID of the order to modify
            symbol (str): Trading pair symbol (e.g., "BTC")
            is_buy (bool): True for buy orders, False for sell orders
            size (float): New order size
            price (float): New price for the order
            order_type (Dict): Order type specification
            reduce_only (bool): Whether the order should only reduce position
            
        Returns:
            Order: The order after the modify: open, or filled if the new price matched at once

        Raises:
            RuntimeError: If client is not authenticated
            ValueError: If the exchange rejected the modify (its text is in the message)
            ActionOutcomeUnknownException: If the modify was sent but its outcome is unknown;
                check the order with get_order_status() before trying again
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
            
        oid = _order_id(order_id)
        size, price = self._validate_and_format_order(symbol, size, price)
        response = self._signed(
            f"{symbol} order modify",
            self.exchange.modify_order,
            oid=oid,
            name=symbol,
            is_buy=is_buy,
            sz=size,
            limit_px=price,
            order_type=order_type,
            reduce_only=reduce_only,
        )
        # Read like the answer to an order: resting, filled (the new price matched at once) or error.
        return self._order_from_response(response, symbol, is_buy, size, price, order_type, reduce_only,
                                         f"{symbol} order modify")

    def update_stop_loss(self, symbol: str, new_price: float) -> Optional[Order]:
        """Move the stop loss of an existing position to a new trigger price.

        Places the new stop loss first, sized to the live position, then cancels the old stop
        loss orders, so the position is never left without one. If the new order is rejected
        or its outcome is unknown, the old orders stay in place and the error is raised. If no
        stop loss exists yet, one is created.

        While it runs, every other signed action of the same key in this process (orders,
        cancels, closes, from any client or thread) waits, so none changes the position or its
        orders between the steps. Under rate limiting or slow answers that wait includes this
        method's retries, which can take tens of seconds.

        Args:
            symbol (str): The trading symbol
            new_price (float): The new stop loss trigger price

        Returns:
            Optional[Order]: The new stop loss order

        Raises:
            AuthenticationRequiredException: If the client has no account
            PositionNotFoundException: If there is no position in `symbol`
            ProtectionReplacementIncompleteException: If the new order was placed but does not
                cover the whole position (it executed at once, partly, or the position grew), or
                that could not be checked, also when there was no old one; or an old one could
                not be canceled
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        return self._replace_protection(symbol, new_price, "stop_loss")

    def update_take_profit(self, symbol: str, new_price: float) -> Optional[Order]:
        """Move the take profit of an existing position to a new trigger price.

        Places the new take profit first, sized to the live position, then cancels the old
        take profit orders. If the new order is rejected or its outcome is unknown, the old
        orders stay in place and the error is raised. If no take profit exists yet, one is
        created. Other signed actions of the key wait while it runs, as in update_stop_loss().

        Args:
            symbol (str): The trading symbol
            new_price (float): The new take profit trigger price

        Returns:
            Optional[Order]: The new take profit order

        Raises:
            AuthenticationRequiredException: If the client has no account
            PositionNotFoundException: If there is no position in `symbol`
            ProtectionReplacementIncompleteException: If the new order was placed but does not
                cover the whole position (it executed at once, partly, or the position grew), or
                that could not be checked, also when there was no old one; or an old one could
                not be canceled
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        return self._replace_protection(symbol, new_price, "take_profit")

    def _replace_protection(self, symbol: str, new_price: float, kind: str) -> Order:
        """Shared implementation of update_stop_loss and update_take_profit.

        Modify is not used: it cannot change trigger orders under the exchange's
        always-place rule, and a modify whose response is lost cannot be told apart from
        one that failed.
        """
        # One replacement at a time per signing key: two concurrent ones would both cancel the
        # same old order and leave two new ones.
        with _signer_state(self.exchange_account.address).operation_lock:
            position = next((p for p in self.get_positions() if p.symbol == symbol and p.size != 0), None)
            if position is None:
                raise PositionNotFoundException(f"No position found for {symbol}")
            old_orders = [order for order in self.get_open_orders(symbol) if order.type == kind]

            place = self.stop_loss if kind == "stop_loss" else self.take_profit
            new_order = place(symbol, abs(float(position.size)), new_price)

            # The new order must protect the whole position now: it may have executed at once
            # (partly), or the position may have grown meanwhile. Only then are the old orders
            # removed; without old orders, a gap is reported all the same.
            old_ids = [o.order_id for o in old_orders]
            kept = "were kept: " if old_ids else ""
            try:
                position = next(
                    (p for p in self.get_positions() if p.symbol == symbol and p.size != 0), None
                )
            except Exception as exc:
                raise ProtectionReplacementIncompleteException(
                    new_order, old_ids,
                    f"{kept}the position could not be read to check the new order covers it",
                ) from exc
            if position is not None and not (
                new_order.status == "open" and new_order.size >= abs(position.size)
                and new_order.is_buy == position.is_short  # still the closing side: it may have flipped
            ):
                raise ProtectionReplacementIncompleteException(
                    new_order, old_ids, f"{kept}the new order does not cover the whole position",
                    position=position,
                )

            not_canceled = []
            for order in old_orders:
                try:
                    self._cancel_one(symbol, order.order_id)
                except Exception as exc:
                    logger.warning(f"Failed to cancel old {kind} order {order.order_id} for {symbol}: {exc}")
                    not_canceled.append(order.order_id)
            if not_canceled:
                raise ProtectionReplacementIncompleteException(new_order, not_canceled)
            return new_order

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
            raise AuthenticationRequiredException()
            
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

    def get_open_order_by_id(self, symbol: str, order_id: Union[str, int], *,
                             address: Optional[str] = None) -> Optional[Order]:
        """Find an open order by its ID.

        Only searches currently open orders. Returns None if the order is
        already filled, cancelled, or doesn't exist. Use get_order_status()
        to query any order regardless of state.

        Args:
            symbol (str): The trading symbol
            order_id (Union[str, int]): The order ID to find
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            Optional[Order]: The open order if found, None otherwise

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        oid = _order_id(order_id)
        orders = self.get_open_orders(symbol, address=address)
        for order in orders:
            if order.order_id == oid:
                return order
        return None

    def close_all_positions(self) -> Dict[str, Order]:
        """Close all open positions, each with close().

        Every position is tried, also after one fails to close.

        Returns:
            Dict[str, Order]: The closing order per symbol, when every position closed.

        Raises:
            RuntimeError: If the client is not authenticated
            ClosePositionsIncompleteException: If a position was not (fully) closed; it carries
                the orders that went through (``closed``) and the error per symbol (``failed``)
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        closed: Dict[str, Order] = {}
        failed: Dict[str, Exception] = {}
        for position in self.get_positions():
            try:
                closed[position.symbol] = self.close(position.symbol, position)
            except Exception as error:  # reported below, after the other positions were tried
                failed[position.symbol] = error
        if failed:
            raise ClosePositionsIncompleteException(closed, failed)
        return closed

    def cancel_order(self, order_id: Union[str, int], symbol: str) -> bool:
        """Cancel a specific order by order ID and symbol.

        Args:
            order_id (Union[str, int]): The order ID to cancel
            symbol (str): The symbol the order is for

        Returns:
            bool: True if the order was canceled, False if it is not open: never placed,
                already canceled or filled.

        Raises:
            AuthenticationRequiredException: If the client has no account
            OrderException: If the exchange answered, but did not cancel the order
            HyperliquidException: If the cancel could not be sent (NetworkException, ...)
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        canceled = self._cancel_one(symbol, _order_id(order_id))
        if canceled:
            logger.info(f"Successfully cancelled order {order_id} for {symbol}")
        else:
            logger.info(f"Order {order_id} for {symbol} not found or already processed")
        return canceled

    def cancel_order_by_cloid(self, cloid: str, symbol: str) -> bool:
        """Cancel an order by the client order id it was placed with (the SDK's cancel_by_cloid).

        Args:
            cloid (str): The order's client order id, "0x" and 32 hex digits (Order.cloid)
            symbol (str): The symbol the order is for

        Returns:
            bool: True if the order was canceled, False if it is not open: never placed,
                already canceled or filled.

        Raises:
            AuthenticationRequiredException: If the client has no account
            InvalidArgumentException: If cloid is not 0x followed by 32 hex digits
            OrderException: If the exchange answered, but did not cancel the order
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        return self._cancel_one(symbol, _cloid(cloid))

    def get_order_book(self, symbol: str, *, decimal: bool = False) -> Dict[str, Any]:
        """Get the current order book (L2 snapshot) for a symbol.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            decimal (bool): Prices, sizes, spread and mid as Decimal, exactly as Hyperliquid
                sends them, instead of float (which can lose digits of large, fine sizes).

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
            UnknownSymbolException: If symbol is not a market of this client.
        """
        self._require_symbol(symbol)
        l2_data = self._with_retry(self.info.l2_snapshot, symbol)
        bids_raw, asks_raw = l2_data["levels"]  # l2Book: levels = [bids, asks], each possibly empty
        number: Callable[[str], Any] = (lambda text: Decimal(str(text))) if decimal else float

        bids = []
        for bid in bids_raw:
            if isinstance(bid, dict) and "px" in bid and "sz" in bid:
                bids.append({
                    "price": number(bid["px"]),
                    "size": number(bid["sz"]),
                    "num_orders": bid.get("n", 0),
                })

        asks = []
        for ask in asks_raw:
            if isinstance(ask, dict) and "px" in ask and "sz" in ask:
                asks.append({
                    "price": number(ask["px"]),
                    "size": number(ask["sz"]),
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
        urgency_factor: float = 0.5,
        bbo_cache: Optional[Dict[str, Dict[str, float]]] = None,
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
            InvalidArgumentException: If side or urgency_factor is invalid
            UnknownSymbolException: If symbol is not a market of this client
        """
        if side not in ["buy", "sell"]:
            raise InvalidArgumentException("side must be 'buy' or 'sell'")

        if not 0.0 <= urgency_factor <= 1.0:
            raise InvalidArgumentException("urgency_factor must be between 0.0 and 1.0")

        if bbo_cache is not None and symbol in bbo_cache:
            cached = bbo_cache[symbol]
            c_bid = cached.get("bid")
            c_ask = cached.get("ask")
            order_book = {
                "bids": [{"price": c_bid}] if c_bid else [],
                "asks": [{"price": c_ask}] if c_ask else [],
                "best_bid": c_bid,
                "best_ask": c_ask,
            }
        else:
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
            ValueError: If leverage is invalid, the market does not allow cross margin
                (is_cross=True on an isolated-only market), or the exchange rejects the update
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        self._require_symbol(symbol)  # else the SDK fails with a bare KeyError while signing
        if leverage < 1:
            raise InvalidArgumentException("Leverage must be >= 1")
        max_lev = self.market_specs.get(symbol, {}).get("max_leverage")
        if max_lev and leverage > int(max_lev):
            raise InvalidArgumentException(f"Max leverage for {symbol} is {max_lev}")
        margin_mode = self.market_specs.get(symbol, {}).get("margin_mode", "cross")
        if is_cross and margin_mode != "cross":
            raise InvalidArgumentException(
                f"{symbol} does not allow cross margin (margin mode {margin_mode}); "
                "use is_cross=False"
            )
        result = self._signed("leverage update", self.exchange.update_leverage, leverage, symbol, is_cross, idempotent=True)
        self._action_result(result, f"{symbol} leverage update")
        return result

    def add_isolated_margin(self, symbol: str, amount: float) -> dict:
        """Add margin to an isolated position.

        Args:
            symbol (str): Trading pair symbol (e.g., "BTC")
            amount (float): USD amount to add to the isolated margin

        Returns:
            dict: API response

        Raises:
            RuntimeError: If client is not authenticated
            ValueError: If amount is not greater than zero. The exchange treats a negative
                amount as removing margin, so it is never sent by accident.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        self._require_symbol(symbol)  # else the SDK fails with a bare KeyError while signing
        if not _positive_number(amount):
            raise InvalidArgumentException(f"amount must be greater than 0, got {amount!r}")
        result = self._signed("isolated margin update", self.exchange.update_isolated_margin, amount, symbol)
        self._action_result(result, f"{symbol} isolated margin update")
        return result

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
            trade_id=raw.get("tid"),
        )

    def get_fills(self, symbol: Optional[str] = None, *, address: Optional[str] = None) -> list:
        """Get recent fills of a wallet, by default the client's account.

        Args:
            symbol (Optional[str]): If provided, only returns fills for this symbol.
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            List[Fill]: List of fill objects.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)
        raw = self._with_retry(self.info.user_fills, address)
        fills = [self._parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return fills

    _FILLS_PER_PAGE = 2000  # most fills one userFillsByTime response returns

    def get_fills_by_time(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        symbol: Optional[str] = None,
        *,
        address: Optional[str] = None,
    ) -> list:
        """Get fills within a time range.

        Args:
            start_time (int): Start time in milliseconds since epoch.
            end_time (Optional[int]): End time in ms. Defaults to current time.
            symbol (Optional[str]): If provided, only returns fills for this symbol.
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            List[Fill]: List of fill objects, oldest first. The exchange keeps only the 10000
                most recent fills of an account.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)
        end = end_time if end_time is not None else int(time.time() * 1000)  # fixed for all pages
        raw, overflow = self._fill_pages(address, start_time, end)
        if overflow:
            logger.warning(f"More than {self._FILLS_PER_PAGE} fills in one millisecond; later fills in the range "
                           "were not returned")
        fills = [self._parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return fills

    _FUNDING_PER_PAGE = 500  # userFunding returns at most 500 records, the oldest from startTime

    def get_user_funding(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        symbol: Optional[str] = None,
        *,
        address: Optional[str] = None,
    ) -> List[FundingPayment]:
        """The funding payments of a wallet in a time range, oldest first (Info userFunding),
        every dex included (HIP-3 symbols prefixed, e.g. xyz:NVDA). Not to be confused with
        get_funding_history, a market's funding rate history.

        One response holds at most 500 payments: this pages through the range, one request per
        500 payments. More than 500 at one timestamp cannot be paged and raise
        HyperliquidException, never a partial list.

        Args:
            start_time (int): Start in milliseconds since the epoch (inclusive).
            end_time (Optional[int]): End in ms (inclusive). Defaults to now.
            symbol (Optional[str]): Only payments of this symbol.
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Raises:
            AddressRequiredException: Without address on a client without an account
            InvalidArgumentException: If the range ends before it starts
            HyperliquidException: If more than 500 payments share one timestamp
        """
        address = self._address(address)
        end = end_time if end_time is not None else int(time.time() * 1000)  # fixed for all pages
        if end < start_time:
            raise InvalidArgumentException("the range ends before it starts")
        records: List[dict] = []
        seen: set = set()
        start = start_time
        while True:
            page = self._with_retry(self.info.user_funding_history, address, start, end)
            new = []
            for record in page:  # one payment per coin and time
                key = (record["time"], record["delta"]["coin"])
                if key not in seen:
                    seen.add(key)
                    new.append(record)
            records.extend(new)
            if len(page) < self._FUNDING_PER_PAGE:
                break
            if not new:
                raise HyperliquidException(
                    f"More than {self._FUNDING_PER_PAGE} funding payments at {page[-1]['time']} ms: the rest of "
                    "the range cannot be read")
            start = page[-1]["time"]  # inclusive: that time's payments come again and are skipped
        payments = [FundingPayment(
            time=r["time"], symbol=r["delta"]["coin"], market=coin_market(r["delta"]["coin"]),
            size=Decimal(str(r["delta"]["szi"])), funding_rate=Decimal(str(r["delta"]["fundingRate"])),
            usdc=Decimal(str(r["delta"]["usdc"]).strip()),
        ) for r in records]
        return [p for p in payments if symbol is None or p.symbol == symbol]

    _RETAINED_FILLS = 10000  # Hyperliquid keeps only the most recent 10000 fills of an account

    def _fill_pages(self, address: str, start: int, end: int) -> tuple:
        """The raw fills from start to end (both inclusive), oldest first, each once; and
        whether more than one response of fills shared a millisecond (the rest is unreachable).

        One response holds at most 2000 fills, the oldest from its start. Paging goes on from
        the last fill's time (inclusive, so that millisecond's fills come again and are skipped
        by trade id, which Hyperliquid documents for every fill).
        """
        if end < start:
            raise InvalidArgumentException("the range ends before it starts")
        raw: List[dict] = []
        seen: set = set()
        while True:
            page = self._with_retry(self.info.user_fills_by_time, address, start, end)
            new = []
            for fill in page:
                if fill["tid"] not in seen:
                    seen.add(fill["tid"])
                    new.append(fill)
            raw.extend(new)
            if len(page) < self._FILLS_PER_PAGE:
                return raw, False
            if not new:
                return raw, True
            start = page[-1]["time"]

    def _kept_before(self, address: str, start: int) -> bool:
        """Whether a fill older than `start` is still kept. Asked after the range was read: if
        one is kept now, every newer fill was kept when the range was read."""
        return start > 0 and bool(self._with_retry(self.info.user_fills_by_time, address, 0, start - 1))

    _ARRIVALS_UNTIL_MS = 30 * 86_400_000  # far past any clock difference to Hyperliquid's servers

    def _fewer_kept_than_retained(self, address: str, in_range: set, end: int, now: int) -> bool:
        """Whether fewer fills than Hyperliquid retains are kept from the range's start on: the
        range's trade ids, those after it up to `now`, and every fill since `now` (a new fill
        can push an old one out). Read without a bound from this computer's clock, which may be
        behind the servers'; each trade id counted once. False when the count cannot be
        completed."""
        kept = set(in_range)
        for start, stop in ((end + 1, now), (now + 1, now + self._ARRIVALS_UNTIL_MS)):
            if stop < start:
                continue
            fills, overflow = self._fill_pages(address, start, stop)
            if overflow:
                return False
            kept.update(fill["tid"] for fill in fills)
        return len(kept) < self._RETAINED_FILLS

    def get_fill_history(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        symbol: Optional[str] = None,
        *,
        address: Optional[str] = None,
    ) -> FillHistory:
        """The fills of a time range, and whether Hyperliquid's limits cut it short.

        Hyperliquid keeps only the 10000 most recent fills of an account. The range is known
        to be complete if a fill older than start_time is still kept after it was read (then
        every newer one was kept too), or if fewer than 10000 fills are kept from start_time
        on, counting those that arrived during the call; else truncated_reason is
        "retention_window". That costs one or two requests, and more pages only when the range
        ends in the past.

        This holds as long as Hyperliquid answers consistently (a fill, once returned, is also in
        later answers) and its servers' clock is within 30 days of this computer's. A fill that
        Hyperliquid records late, behind a range already read, cannot be ruled out by any
        sequence of reads; truncated False means none of the limits above applies.

        Args:
            start_time (int): Start in milliseconds since the epoch (inclusive).
            end_time (Optional[int]): End in ms (inclusive). Defaults to now.
            symbol (Optional[str]): Only fills of this symbol.
            address (Optional[str]): The wallet to read. Defaults to the client's account.

        Returns:
            FillHistory: The fills oldest first, truncated and truncated_reason.

        Raises:
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)
        now = int(time.time() * 1000)
        end = now if end_time is None else end_time
        raw, overflow = self._fill_pages(address, start_time, end)
        reason = None
        if overflow:
            reason = "same_millisecond_overflow"
        elif not self._kept_before(address, start_time) and not self._fewer_kept_than_retained(
                address, {fill["tid"] for fill in raw}, end, now):
            reason = "retention_window"
        fills = [self._parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return FillHistory(fills=fills, truncated=reason is not None, truncated_reason=reason)

    def get_order_status(self, order_id: Union[int, str], *, address: Optional[str] = None) -> dict:
        """Query the status of a specific order by order ID.

        Args:
            order_id (Union[int, str]): The order ID to query.
            address (Optional[str]): The wallet the order belongs to. Defaults to the client's account.

        Returns:
            dict: Order status from the API.

        Raises:
            AddressRequiredException: Without address on a client without an account
            InvalidArgumentException: If order_id is not a whole number
        """
        oid = _order_id(order_id)
        address = self._address(address)
        return self._with_retry(self.info.query_order_by_oid, address, oid)

    def get_order_status_by_cloid(self, cloid: str, *, address: Optional[str] = None) -> dict:
        """Query the status of an order by the client order id it was placed with (the SDK's
        query_order_by_cloid). The answer has the format of get_order_status().

        Args:
            cloid (str): The order's client order id, "0x" and 32 hex digits (Order.cloid)
            address (Optional[str]): The wallet the order belongs to. Defaults to the client's account.

        Returns:
            dict: Order status from the API; {"status": "unknownOid"} if the wallet has no
                order with this client order id.

        Raises:
            AddressRequiredException: Without address on a client without an account
            InvalidArgumentException: If cloid is not 0x followed by 32 hex digits
        """
        client_order_id = _cloid(cloid)
        address = self._address(address)
        return self._with_retry(self.info.query_order_by_cloid, address, client_order_id)

    def bulk_order(self, orders: List[dict]) -> dict:
        """Place several orders in one action (the SDK's bulk_orders).

        Args:
            orders: List of order dicts, each with keys:
                - symbol (str): Trading pair symbol
                - is_buy (bool): True for buy, False for sell
                - size (float): Order size
                - limit_price (float): Limit price
                - reduce_only (bool, optional): Defaults to False
                - time_in_force (str, optional): "Gtc", "Ioc", or "Alo". Defaults to "Gtc"
                - cloid (str, optional): Client order id, "0x" and 32 hex digits. With one, the
                  order can be looked up (get_order_status_by_cloid) if the outcome is unknown.

        Returns:
            dict: Hyperliquid's answer. Its statuses have one entry per order, in order:
                {"resting": {"oid"}}, {"filled": {"oid", "totalSz", "avgPx"}}, or
                {"error": text} for an order the exchange rejected; the others are placed.

        Raises:
            AuthenticationRequiredException: If the client has no account
            InvalidArgumentException: If any order is invalid; checked before anything is sent
            OrderRejectedException: If the exchange refused the whole action
            ActionOutcomeUnknownException: If the action was sent but its outcome is unknown,
                or the answer is not one documented status per order; look the orders up by
                their cloids, or check the open orders and fills, before sending again
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        order_requests = []
        for o in orders:
            missing = [key for key in ("symbol", "is_buy", "size", "limit_price") if key not in o]
            if missing:
                raise InvalidArgumentException(f"bulk_order: an order lacks {', '.join(missing)}: {o}")
            size, price = self._validate_and_format_order(o["symbol"], o["size"], o["limit_price"])
            for key in ("is_buy", "reduce_only"):
                if not isinstance(o.get(key, False), bool):
                    raise InvalidArgumentException(f"bulk_order: {key} must be True or False, got {o[key]!r}")
            time_in_force = o.get("time_in_force", "Gtc")
            if time_in_force not in _TIMES_IN_FORCE:
                raise InvalidArgumentException(
                    f"bulk_order: time_in_force must be Gtc, Ioc or Alo, got {time_in_force!r}")
            request = {
                "coin": o["symbol"],
                "is_buy": o["is_buy"],
                "sz": size,
                "limit_px": price,
                "order_type": {"limit": {"tif": time_in_force}},
                "reduce_only": o.get("reduce_only", False),
            }
            if o.get("cloid") is not None:
                request["cloid"] = _cloid(o["cloid"])
            order_requests.append(request)

        response = self._signed("bulk order", self.exchange.bulk_orders, order_requests)
        if isinstance(response, dict) and response.get("status") == "err":  # refused as a whole
            raise OrderRejectedException("bulk order", str(response.get("response")))
        statuses = self._statuses(response)
        if (statuses is None or len(statuses) != len(order_requests)
                or not all(self._is_documented_status(status) for status in statuses)):
            raise ActionOutcomeUnknownException("bulk order", f"answered in no documented format: {response}")
        return response

    def bulk_cancel(self, cancels: List[dict]) -> dict:
        """Cancel multiple orders atomically.

        Args:
            cancels: List of dicts, each with keys:
                - symbol (str): Trading pair symbol
                - order_id (str or int): The order ID to cancel

        Returns:
            dict: API response

        Raises:
            AuthenticationRequiredException: If the client has no account
            InvalidArgumentException: If a cancel lacks its symbol or order id, or its symbol
                is not a market of the client (UnknownSymbolException); checked before
                anything is sent
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        cancel_requests = []
        for c in cancels:
            missing = [key for key in ("symbol", "order_id") if key not in c]
            if missing:
                raise InvalidArgumentException(f"bulk_cancel: a cancel lacks {', '.join(missing)}: {c}")
            self._require_symbol(c["symbol"])  # else the SDK fails with a bare KeyError while signing
            cancel_requests.append({"coin": c["symbol"], "oid": _order_id(c["order_id"])})
        return self._signed("cancel", self.exchange.bulk_cancel, cancel_requests, idempotent=True)

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
        self._require_symbol(symbol)
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
            AddressRequiredException: Without address on a client without an account
        """
        address = self._address(address)

        return self._with_retry(self.info.post, "/info", {"type": "portfolio", "user": address})

