import logging
import os
import re
import threading
import time
import warnings
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Union, cast, overload

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
    PositionNotFoundException,
    RateLimitException,
    ServerErrorException,
    UnauthorizedException,
    UnsupportedAccountModeException,
    ConfigurationException,
    ClientErrorException,
    HyperliquidException,
    InvalidArgumentException,
    NetworkException,
    TimeoutException,
    UnknownSymbolException,
)
from . import _answers, _inputs, _maker, _protection, _signing, pnl
from ._inputs import TimeInForce
from .models import (
    DACITE_CONFIG,
    AccountSummary,
    Candle,
    PnLReport,
    ProtectionSync,
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
    OrderType as OrderType,  # not used here; kept importable from this module
    Position,
    SpotState,
    SpotTokenBalance,
    UserState,
)

logger = logging.getLogger("fractrade_hl_simple")
logger.addHandler(logging.NullHandler())


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
HISTORICAL_ORDERS_LIMIT = 2000  # historicalOrders lists at most this many entries
_PRIVATE_KEY = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")


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


_USDC_TOKEN = 0  # USDC's spot token index (mainnet and testnet)
# Hyperliquid's candle intervals (info endpoint docs, candle snapshot) and their length in ms;
# a month counted as 31 days. Only the most recent MAX_CANDLES of each interval are kept.
CANDLE_INTERVALS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000, "12h": 43_200_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000, "1M": 2_678_400_000,
}
MAX_CANDLES = 5000


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


TPSL_MARKET_TOLERANCE = _answers.TPSL_MARKET_SLIPPAGE_PCT / 100  # of market TP/SL orders
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


def _api_url(base_url: Optional[str], env: Optional[str], trust_env: bool) -> str:
    """The API URL of a client: base_url as given (one of the SDK's URL constants), else the
    network named by env, by HYPERLIQUID_ENV (with trust_env), or mainnet."""
    if base_url is None:
        if env is None:
            env = (os.environ.get("HYPERLIQUID_ENV") if trust_env else None) or "mainnet"
            env = env.strip().lower()
        if env not in _NETWORK_URLS:
            raise InvalidArgumentException("env and HYPERLIQUID_ENV must be either 'mainnet' or 'testnet'")
        return _NETWORK_URLS[env]
    if not isinstance(base_url, str) or base_url not in _NETWORKS:
        raise InvalidArgumentException(
            f"base_url must be one of the constants MAINNET_API_URL ({constants.MAINNET_API_URL}), "
            f"TESTNET_API_URL ({constants.TESTNET_API_URL}) or LOCAL_API_URL "
            f"({constants.LOCAL_API_URL}) from hyperliquid.utils.constants, got {base_url!r}; "
            "with any other URL the SDK would sign for testnet")
    return base_url


def _check_settings(default_slippage: float, request_timeout: float, max_retries: int, retry_delay: float) -> None:
    """InvalidArgumentException for a client setting out of its range."""
    if not 0 < default_slippage <= 0.5:
        raise InvalidArgumentException("default_slippage must be between 0 (exclusive) and 0.5 (inclusive)")
    if not 0 < request_timeout <= 120:
        raise InvalidArgumentException("request_timeout must be between 0 (exclusive) and 120 seconds")
    if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
        raise InvalidArgumentException(f"max_retries must be a whole number of at least 0, got {max_retries!r}")
    if isinstance(retry_delay, bool) or not (_inputs.positive_number(retry_delay) or retry_delay == 0):
        raise InvalidArgumentException(f"retry_delay must be a number of seconds of at least 0, got {retry_delay!r}")


class HyperliquidClient:
    """A Hyperliquid client: market data, wallet reads and trading.

    What holds for every method and is not repeated in each:

    - A method that reads a wallet takes `address` for any wallet. Without it, it reads the
      client's account; a client without one raises AddressRequiredException.
    - A method that signs (orders, cancels, transfers, leverage and margin changes) needs an
      account with a private key: without one it raises AuthenticationRequiredException
      (a RuntimeError).
    - A bad argument raises InvalidArgumentException (a ValueError); UnknownSymbolException
      is one of them.
    - Symbols are Hyperliquid's names: "BTC" for a perp, "xyz:NVDA" for a market of a HIP-3
      dex, a token such as "HYPE" (or a pair, "HYPE/USDT0") in the spot methods.
    """

    # Metadata cached for the next client of the same network and dex list (_cached_key).
    _cached_key: Optional[tuple] = None
    _cached_market_specs: Optional[Dict[str, Dict]] = None
    _cached_market_specs_at: float = 0
    _cached_meta: Optional[Any] = None
    _cached_spot_meta: Optional[Any] = None
    _cache_lock = threading.Lock()  # read and publish the _cached_* fields together
    _CACHE_TTL: float = 86400  # 24 hours

    market_specs: Dict[str, Dict]  # per symbol; loaded when the client is created
    _meta: Any       # Hyperliquid's meta of the default dex, as the SDK's Info takes it
    _spot_meta: Any  # Hyperliquid's spotMeta

    # ── Construction and market metadata ────────────────────────────────────

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
        """Create a client for one network, with or without an account.

        Args:
            account: The account to sign with and to read by default. If None, the credentials
                are read from the environment (see trust_env); without any, the client reads
                public data only.
            env: Deprecated, use base_url. "mainnet" or "testnet".
            default_slippage: Slippage of market orders (0.05 = 5 %).
            max_retries: How often a transient failure is retried; 0 to disable.
            retry_delay: Base delay in seconds between retries (exponential backoff).
            cache_market_specs: If True (default), reuses cached market specs across instances
                within the same process. Cache expires after 24 hours. Set to False to always
                fetch fresh specs on init.
            extended_universe: If True, enables trading of stocks, commodities, indices, and forex
                (xyz: symbols like xyz:TSLA, xyz:GOLD). Off by default to avoid extra API
                overhead. The same as perp_dexs=["xyz"].
            proxy: Proxies as requests takes them ({"https": "http://host:port"}).
            request_timeout: Seconds to wait for each HTTP request.
            base_url: The API to use, as in the Hyperliquid SDK: one of the constants
                MAINNET_API_URL, TESTNET_API_URL or LOCAL_API_URL from
                hyperliquid.utils.constants. If None, the network named by the HYPERLIQUID_ENV
                environment variable ("mainnet" or "testnet"), else mainnet.
            perp_dexs: The HIP-3 dexes (builder-deployed perps) this client reads and trades
                besides the default dex, as the SDK's Info takes them, e.g. ["xyz", "para"]; their
                markets are named with the dex as prefix (para:TOTAL2). The default dex is always
                included. Each dex adds requests to creating the client and to every read of
                positions, of all open orders or of all prices (a symbol's price and open orders
                read only its dex). get_perp_dexs() lists the dexes of the network. Not together
                with extended_universe=True.
            trust_env: Like requests' Session.trust_env, for this library's settings. True
                (default): settings not passed are read from the environment and a .env file:
                the credentials when account is None, the network when base_url is None. False:
                none of them is read; without account the client has no key, and reads take the
                wallet as address=. HTTP settings such as proxy variables are still applied by
                requests itself; pass proxy= to set a proxy explicitly.

        Raises:
            InvalidArgumentException: If base_url is not one of those constants, env is not
                'mainnet' or 'testnet', both are passed, another parameter is out of range, or
                the network does not have a dex of perp_dexs
        """
        if env is not None:
            warnings.warn("env is deprecated; pass base_url=constants.MAINNET_API_URL or "
                          "constants.TESTNET_API_URL (from hyperliquid.utils import constants)",
                          DeprecationWarning, stacklevel=2)
            if base_url is not None:
                raise InvalidArgumentException("pass base_url or env, not both")
        if trust_env and ((base_url is None and env is None) or account is None):
            load_env_file()  # configured from the environment: honour a .env file, as before
        base_url = _api_url(base_url, env, trust_env)
        _check_settings(default_slippage, request_timeout, max_retries, retry_delay)
        self.request_timeout = request_timeout
        self.base_url = base_url
        self.env = _NETWORKS[base_url]  # "mainnet", "testnet", or None for a local node
        self.default_slippage = default_slippage
        self.max_retries = max_retries
        self.retry_delay = float(retry_delay)  # time.sleep takes no Decimal
        self.perp_dexs = _perp_dexs(perp_dexs, extended_universe)
        self._proxy = proxy

        self._load_markets(cache_market_specs)

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

    def _load_markets(self, cache_market_specs: bool) -> None:
        """The client's Info and markets: from the process-wide cache if it holds them for this
        network and dex list, else read (and the cache updated). Cached metadata is reused only
        by a client for the same network and dex list: the SDK derives asset ids from it, so
        metadata of another network or dex list would send orders to the wrong market."""
        self._cache_key = (self.base_url, tuple(self.perp_dexs))
        with HyperliquidClient._cache_lock:
            cached = (HyperliquidClient._cached_key, HyperliquidClient._cached_meta,
                      HyperliquidClient._cached_spot_meta, HyperliquidClient._cached_market_specs,
                      HyperliquidClient._cached_market_specs_at)
        cached_key, cached_meta, cached_spot_meta, cached_specs, cached_specs_at = cached
        if (
            cache_market_specs
            and cached_key == self._cache_key
            and cached_specs is not None
            and cached_meta is not None
            and time.time() - cached_specs_at < self._CACHE_TTL
        ):
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

    def _setup_authenticated_client(self, account: HyperliquidAccount):
        """Set up the account the client signs with. An account that cannot sign raises
        InvalidArgumentException (a ValueError); the message never contains the key."""
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

    def _apply_proxy(self, sdk_instance):
        """Apply proxy settings to an SDK instance's requests session."""
        if self._proxy and hasattr(sdk_instance, 'session'):
            sdk_instance.session.proxies.update(self._proxy)

    def is_authenticated(self) -> bool:
        """Whether the client has an account with a private key and an address."""
        return (
            hasattr(self, 'account') and
            self.account is not None and
            self.account.private_key is not None and
            self.account.public_address is not None
        )

    def _markets_with_contexts(self, info: Info, dex: str
                               ) -> Tuple[Dict[str, Any], List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        """The meta of `dex` and (market, context) for each of its markets, in Hyperliquid's
        order, from one metaAndAssetCtxs request. Failures (after the retries) and answers that
        are not in the documented format ([meta, one context per market]) are raised as they are."""
        meta, contexts = self._with_retry(info.post, "/info", {"type": "metaAndAssetCtxs", "dex": dex})
        return meta, list(zip(meta["universe"], contexts, strict=True))

    def _fetch_market_specs(self, info: Optional[Info] = None, spot_meta: Any = None) -> Dict[str, Dict]:
        """Fetch current market specifications from the API using meta_and_asset_ctxs for richer
        data, through `info` (default: the client's). `spot_meta` (default: the client's) names
        each dex's collateral token."""
        info = self.info if info is None else info
        tokens = {token.get("index"): token.get("name")
                  for token in (self._spot_meta if spot_meta is None else spot_meta)["tokens"]
                  if isinstance(token, dict)}
        specs = {}

        for dex in self.perp_dexs:
            # Every dex or none: a skipped dex would leave its markets unknown, or formatted with
            # wrong size decimals.
            meta, markets = self._markets_with_contexts(info, dex)
            collateral = tokens.get(meta.get("collateralToken"))  # USDC on the default dex; HIP-3 dexes may differ
            for market, ctx in markets:
                specs[market["name"]] = {
                    "collateral": collateral,
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
        the market specs (size decimals, max leverage, margin mode). Returns the new specs.

        Nothing reloads on its own; call this in a long-running program when markets were
        listed. The client changes only once everything is loaded: a failure raises
        (NetworkException, ...) and leaves the client as it was.
        """
        info = self._new_info()
        meta, spot_meta = self._startup_metadata(info)
        specs = self._fetch_market_specs(info, spot_meta)
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

    # ── Requests and signed actions ─────────────────────────────────────────

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

    def _with_retry(self, fn, *args, **kwargs):
        """Call `fn(*args, **kwargs)`, retrying transient failures with exponential backoff.

        For reads and idempotent actions only. Orders, modifies, transfers and margin changes
        go through _signed(), which never retries a request the exchange may have executed.

        Raises:
            RateLimitException, ServerErrorException, NetworkException or TimeoutException
            when all retries are exhausted; ClientErrorException for any other HTTP 4xx;
            any other error at once.
        """
        attempts = self.max_retries + 1  # first attempt + retries

        for attempt in range(attempts):
            try:
                result = fn(*args, **kwargs)
                if _is_unparsed(result):  # the Exchange's requests (Info raises it itself)
                    raise ServerErrorException(_NOT_JSON, 200)
                return result
            except ClientError as e:
                if e.status_code == 429:
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

        raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover

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
        steps, see _protection.replace, so they also wait through the replacement's own
        retries). The wait before an action's own retry is outside the lock."""
        with _signing.signer_state(self.exchange_account.address).operation_lock:
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

    def _with_fresh_nonce(self, fn, *args, **kwargs):
        """Call a signing SDK method with this signer's lock held and an unused nonce.

        The SDK uses the current time in milliseconds as the nonce, so two actions from one
        signer in the same millisecond collide. This waits until the clock has moved past the
        last action of the signer, across all clients in the process.
        """
        signer = _signing.signer_state(self.exchange_account.address)
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
            if _answers.is_definite_order_response(response):
                return response
            cause = ValueError(f"unexpected order response: {response}")
        status = self._reconcile_order(symbol, cloid, cause)
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}

    def _reconcile_order(self, symbol: str, cloid: Cloid, cause: Exception) -> Dict[str, Any]:
        """Status entry ({"resting" | "filled" | "error": ...}) for an order whose placement
        response was lost, from the order status looked up by client order id. An order found
        open comes with the size still open, {"resting": {"oid", "open_size"}}: part of it may
        have filled.

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
                    return {"resting": {"oid": oid, "open_size": order["sz"]}}
                if status == "triggered":  # a trigger order that fired and is executing
                    detail = "trigger order triggered, its outcome is not known yet"
                    continue
                if status in _answers.REJECTED_STATUSES:
                    text = _answers.REJECTION_TEXTS.get(status)
                    return {"error": f"{text} (order {cloid}: {status})" if text
                            else f"Order {cloid} was rejected ({status})"}
                if status == "filled" or status in _answers.CANCELED_STATUSES:
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

    def _cancel_all(self, cancels: List[Dict[str, Any]]) -> Any:
        """Bulk-cancel orders ({"coin", "oid"}); raise OrdersNotCanceledException naming each
        one that the exchange did not cancel. Orders that are already gone count as done.
        Returns the exchange's answer."""
        response = self._signed("cancel", self.exchange.bulk_cancel, cancels, idempotent=True)
        outcomes = list(zip(cancels, _answers.cancel_outcomes(response, len(cancels))))
        failed = [{"symbol": cancel["coin"], "order_id": cancel["oid"],
                   "reason": reason if outcome == "failed" else f"not confirmed ({reason}): it may still be open"}
                  for cancel, (outcome, reason) in outcomes if outcome in ("failed", "unknown")]
        if failed:
            raise OrdersNotCanceledException(
                failed, canceled=[cancel["oid"] for cancel, (outcome, _) in outcomes if outcome == "canceled"],
                not_open=[cancel["oid"] for cancel, (outcome, _) in outcomes if outcome == "gone"])
        return response

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
        outcome, reason = _answers.cancel_outcomes(response, 1)[0]
        if outcome == "unknown":
            raise OrderException(f"Cancel was not confirmed by the exchange: {reason}, in {response}")
        if outcome == "gone" and attempts > 1:
            return self._was_canceled(coin, order)
        if outcome in ("canceled", "gone"):
            return outcome == "canceled"
        raise OrderException(f"Order {order} for {coin} was not canceled: {reason}")

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

    # ── Market data ─────────────────────────────────────────────────────────

    @overload
    def get_price(self, symbol: str) -> float: ...

    @overload
    def get_price(self, symbol: None = None) -> Dict[str, float]: ...

    def get_price(self, symbol: Optional[str] = None) -> Union[float, Dict[str, float]]:
        """The mid price of `symbol`, or without one the mids of every market as a dict. For one
        symbol only its dex's mids are read, as the SDK reads a market order's price; without
        one, those of every dex of the client (spot mids come with the default dex)."""
        prices: Dict[str, float] = {}
        for dex in self._dexes_of(symbol):
            response = self._with_retry(self.info.all_mids, dex=dex)
            prices.update({sym: float(price) for sym, price in response.items()})

        if symbol is not None:
            if symbol not in prices:
                raise self._unknown_symbol(symbol)
            return prices[symbol]

        return prices

    def get_market_info(self, symbol: Optional[str] = None) -> Union[Dict, List[Dict]]:
        """A market's entry in Hyperliquid's meta, as Hyperliquid sends it; without a symbol, a
        list of the entries of every market on the client's perp dexes."""
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
        whether it is delisted or at its open interest cap; in Hyperliquid's order, delisted
        markets included. Two requests per dex."""
        markets: List[Market] = []
        for dex in self.perp_dexs:
            _, with_contexts = self._markets_with_contexts(self.info, dex)
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
        """The predicted funding rate of `symbol`, or without one those of every market.

        Args:
            threshold: Without a symbol, only the rates whose absolute value is at least the
                threshold's.

        Returns:
            The symbol's rate as a float; without a symbol a list of
            {"symbol", "funding_rate"} dicts, from the highest positive to the lowest negative.

        Raises:
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

        if symbol is not None:
            rate = next((data['funding_rate'] for data in funding_data if data['symbol'] == symbol), None)
            if rate is None:
                raise HyperliquidException(f"Hyperliquid has no predicted funding rate for {symbol}")
            return rate

        if threshold is not None:
            funding_data = [
                data for data in funding_data
                if abs(data['funding_rate']) >= abs(threshold)
            ]

        funding_data.sort(key=lambda x: x['funding_rate'], reverse=True)

        return funding_data

    def get_funding_history(
        self,
        symbol: str,
        start_time: int,
        end_time: Optional[int] = None,
    ) -> List[dict]:
        """The historical funding rates of a market, oldest first: a list of dicts with the keys
        time, funding_rate and premium.

        One response holds at most 500 rates (about 20 days of hourly funding): this pages
        through the range, one request per 500.

        Args:
            start_time: Start in milliseconds since the epoch.
            end_time: End in ms. Defaults to now.
        """
        self._require_symbol(symbol)
        end = end_time if end_time is not None else int(time.time() * 1000)  # fixed for all pages
        raw: List[dict] = []
        start = start_time
        while True:
            page = self._with_retry(self.info.funding_history, symbol, start, end)
            raw.extend(page)
            if len(page) < self._FUNDING_PER_PAGE:
                break
            start = page[-1]["time"] + 1  # one rate per time for a market: none is skipped
        return [
            {
                "time": r["time"],
                "funding_rate": float(r["fundingRate"]),
                "premium": float(r["premium"]),
            }
            for r in raw
        ]

    def get_perp_dexs(self) -> List[Dict[str, Any]]:
        """The HIP-3 dexes of the network (the SDK's perp_dexs), the names perp_dexs= takes:
        [{"name": "xyz", "full_name": "XYZ"}, ...], in Hyperliquid's order. The default dex is
        not listed. Every dex Hyperliquid lists is in it, also one whose markets are all
        delisted."""
        return [{"name": dex["name"], "full_name": dex.get("fullName")}
                for dex in self._with_retry(self.info.perp_dexs) if dex]

    def get_order_book(self, symbol: str, *, decimal: bool = False) -> Dict[str, Any]:
        """The current order book (L2 snapshot) of a market.

        Args:
            decimal: Prices, sizes, spread and mid as Decimal, exactly as Hyperliquid sends them,
                instead of float (which can lose digits of large, fine sizes).

        Returns:
            A dict:
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
        """A limit price between the best bid and the best ask, by how urgent the order is,
        rounded as the exchange takes it. The mid price if the order's own side of the book is
        empty.

        Args:
            side: "buy" or "sell".
            urgency_factor: From 0.0 (patient: the best price of the order's own side, a maker
                order) to 1.0 (aggressive: the best price of the other side, which crosses the
                spread).
            bbo_cache: The best bid and ask per symbol, as in maker_order(): used instead of
                requesting the order book.
        """
        if side not in ["buy", "sell"]:
            raise InvalidArgumentException("side must be 'buy' or 'sell'")

        if not 0.0 <= urgency_factor <= 1.0:
            raise InvalidArgumentException("urgency_factor must be between 0.0 and 1.0")

        if bbo_cache is not None and symbol in bbo_cache:
            cached = bbo_cache[symbol]
            c_bid = cached.get("bid")
            c_ask = cached.get("ask")
            order_book: Dict[str, Any] = {
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

    def get_candles(
        self,
        symbol: str,
        interval: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        lookback: Optional[int] = None,
    ) -> List[Candle]:
        """Candles of a market, oldest first: open, high, low, close, volume and the number of
        trades (the SDK's candles_snapshot). One request.

        Pass start_time, or lookback: the number of the most recent candles up to end_time.
        Hyperliquid keeps only the most recent 5000 candles of each interval (about 3.5 days of
        1m, 208 days of 1h); for an older start_time it returns those it keeps. The last candle
        may still be forming: its close_time is then in the future.

        Args:
            symbol: A market of the client: a perp (BTC), a HIP-3 market (xyz:SPCX), or a
                spot pair as the SDK names it (PURR/USDC).
            interval: "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d",
                "3d", "1w" or "1M".
            start_time: Start in milliseconds since the epoch (inclusive).
            end_time: End in ms (inclusive). Defaults to now.
            lookback: Instead of start_time, how many candles, 1 to 5000; at most that many
                are returned.
        """
        self._require_symbol(symbol)
        if interval not in CANDLE_INTERVALS:
            raise InvalidArgumentException(f"interval must be one of {', '.join(CANDLE_INTERVALS)}, got {interval!r}")
        if (start_time is None) == (lookback is None):
            raise InvalidArgumentException("pass start_time or lookback, exactly one")
        end = end_time if end_time is not None else int(time.time() * 1000)
        if lookback is not None:
            if not isinstance(lookback, int) or isinstance(lookback, bool) or not 1 <= lookback <= MAX_CANDLES:
                raise InvalidArgumentException(f"lookback must be a whole number from 1 to {MAX_CANDLES}, got {lookback!r}")
            # At least lookback candles (trimmed below); never before the epoch (1w, 1M go there).
            start = max(0, end - lookback * CANDLE_INTERVALS[interval])
        else:
            start = cast(int, start_time)  # not None: exactly one of the two is given
        if end < start:
            raise InvalidArgumentException("the range ends before it starts")
        raw = self._with_retry(self.info.candles_snapshot, symbol, interval, start, end)
        candles = [Candle(symbol=symbol, interval=interval, open_time=int(c["t"]), close_time=int(c["T"]),
                          open=Decimal(str(c["o"])), high=Decimal(str(c["h"])), low=Decimal(str(c["l"])),
                          close=Decimal(str(c["c"])), volume=Decimal(str(c["v"])), trades=int(c["n"]))
                   for c in raw]
        candles.sort(key=lambda candle: candle.open_time)
        return candles[-lookback:] if lookback is not None else candles

    # ── Account ─────────────────────────────────────────────────────────────

    _SENTINEL_DEX = object()  # Used to detect "dex not explicitly passed"

    def get_user_state(self, address: Optional[str] = None, dex: object = _SENTINEL_DEX) -> UserState:
        """A wallet's perp account state (Hyperliquid's clearinghouseState): margin summaries and
        positions.

        Without dex, positions and margin summaries are merged across the client's perp dexes
        (perp_dexs: the default dex, plus xyz with extended_universe=True). Pass dex="" or
        dex="xyz" to read a single clearinghouse.
        """
        address = self._address(address)

        if dex is not self._SENTINEL_DEX:
            return self._get_user_state_single(address, cast(str, dex))

        if len(self.perp_dexs) == 1:
            return self._get_user_state_single(address, self.perp_dexs[0])

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
            for name in merged_margin:
                merged_margin[name] += getattr(state.margin_summary, name)
                merged_cross[name] += getattr(state.cross_margin_summary, name)
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
        """The open positions of a wallet on the client's perp dexes (perp_dexs)."""
        state = self.get_user_state(self._address(address))
        return [pos.position for pos in state.asset_positions]

    def _position(self, symbol: str) -> Optional[Position]:
        """The open position in `symbol` (None: flat), read from its own dex only."""
        state = self.get_user_state(self._address(None), dex=_sdk_dex(symbol))
        return next((p.position for p in state.asset_positions
                     if p.position.symbol == symbol and p.position.size != 0), None)

    def has_position(self, symbol: str, *, address: Optional[str] = None) -> bool:
        """Whether the wallet has an open position in `symbol`."""
        positions = self.get_positions(address=address)
        return any(p.symbol == symbol and p.size != 0 for p in positions)

    def get_position_size(self, symbol: str, *, address: Optional[str] = None) -> Optional[Decimal]:
        """The size of the wallet's position in `symbol` (negative for a short), or None without
        one."""
        positions = self.get_positions(address=address)
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            return None
        return position.size

    def get_position_direction(self, symbol: str, *, address: Optional[str] = None) -> Optional[str]:
        """The direction of the wallet's position in `symbol`: "long" or "short", or None without
        one."""
        position_size = self.get_position_size(symbol, address=address)
        if position_size is None:
            return None
        return "long" if float(position_size) > 0 else "short"

    def get_account_mode(self, address: Optional[str] = None) -> str:
        """How an account holds its collateral.

        Returns:
            "standard": spot and each perp dex have their own balance.
            "unified": one balance per token in the spot account, shared by spot and the perp
                positions of every dex.
            "portfolio_margin": unified, plus borrowing against collateral.
            "unknown": "dexAbstraction" (discontinued by Hyperliquid) or a mode this library
                does not know.
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
        """
        return self._with_retry(self.info.user_role, self._address(address))

    def get_leverage(self, symbol: str, *, address: Optional[str] = None) -> Leverage:
        """The leverage a wallet uses on a perp market: cross or isolated margin and its value,
        as set with set_leverage (or in the Hyperliquid app), else the market's default.
        Hyperliquid's documented activeAssetData request.

        Raises:
            UnknownSymbolException: If the symbol is not a market of the client (no request)
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
        userFees request: userCrossRate and userAddRate, the wallet's tier and staking discount,
        with its referral discount (activeReferralDiscount) taken off the fees, as the fee
        formula of Hyperliquid's fee documentation does.

        On a HIP-3 dex they are scaled as that formula does, by the market's growthMode and
        deployerFeeScale s: growth mode cuts the protocol's fees and rebates to a tenth; the
        deployer adds s times the protocol's fee (s at most 1), or, above 1, the protocol's fee
        is raised to the deployer's, so 2 × s times. A maker rebate (a negative rate) is only
        cut by growth mode, the referral discount does not apply to it. The formula's terms for
        a dex whose collateral is an aligned quote asset (lower taker fees, higher rebates) are
        not applied: there the rates are somewhat too high.

        Raises:
            UnknownSymbolException: If the symbol is not a market of the client (no request)
            HyperliquidException: If Hyperliquid answers in no documented format
        """
        self._require_symbol(symbol)
        spec = self.market_specs.get(symbol, {})
        answer = self._with_retry(self.info.post, "/info", {"type": "userFees", "user": self._address(address)})
        try:
            taker, maker = Decimal(str(answer["userCrossRate"])), Decimal(str(answer["userAddRate"]))
            discount = Decimal(str(answer["activeReferralDiscount"]))
        except (KeyError, TypeError, InvalidOperation):
            raise HyperliquidException(f"userFees answered in no documented format: {answer!r}") from None
        if not (taker.is_finite() and maker.is_finite() and discount.is_finite()):
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
            if dex is not None and growth:
                rate = rate / 10
            if rate < 0:  # a rebate: only growth mode applies
                return rate
            if dex is not None and scale is not None:  # scale: set on every HIP-3 dex
                rate = rate * (1 + scale) if scale <= 1 else rate * 2 * scale
            return rate * (1 - discount)

        return FeeRates(symbol=symbol, taker=scaled(taker), maker=scaled(maker), dex=dex,
                        growth_mode=growth if dex is not None else False, deployer_fee_scale=scale)

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

    @overload
    def get_perp_balance(self, address: Optional[str] = None, simple: Literal[True] = True) -> Decimal: ...

    @overload
    def get_perp_balance(self, address: Optional[str] = None, *, simple: Literal[False]) -> Dict[str, Any]: ...

    @overload
    def get_perp_balance(self, address: Optional[str], simple: Literal[False]) -> Dict[str, Any]: ...

    @overload
    def get_perp_balance(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]: ...

    def get_perp_balance(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """The balance that perp positions trade against, including their unrealized PnL.

        Standard accounts: the account value of the perp account (summed over the client's
        perp dexes, each holds its own collateral). Unified accounts: the USDC in the spot
        account, which is shared with spot and already includes the PnL; the perp account
        figures are only the part of it backing positions.

        Returns:
            The balance in USD. With simple=False a dict: balance, positions (the open
            positions), margin_used and state (the full user state).

        Raises:
            UnsupportedAccountModeException: For portfolio-margin accounts and unknown account
                modes.
        """
        address = self._address(address)

        return self._perp_balance(address, self._balance_mode(address), simple)

    @overload
    def _perp_balance(self, address: str, mode: str, simple: Literal[True],
                      spot_usdc: Optional[Decimal] = None) -> Decimal: ...

    @overload
    def _perp_balance(self, address: str, mode: str, simple: Literal[False],
                      spot_usdc: Optional[Decimal] = None) -> Dict[str, Any]: ...

    @overload
    def _perp_balance(self, address: str, mode: str, simple: bool,
                      spot_usdc: Optional[Decimal] = None) -> Union[Decimal, Dict[str, Any]]: ...

    def _perp_balance(self, address: str, mode: str, simple: bool,
                      spot_usdc: Optional[Decimal] = None) -> Union[Decimal, Dict[str, Any]]:
        """`spot_usdc`: the spot USDC already read, so that results agree with that read."""
        if simple and mode == "unified":  # the spot USDC alone: no perp state is read
            return self._spot_usdc_total(address) if spot_usdc is None else spot_usdc
        user_state = self.get_user_state(address)
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

    @overload
    def get_all_balances(self, address: Optional[str] = None, simple: Literal[True] = True) -> Decimal: ...

    @overload
    def get_all_balances(self, address: Optional[str] = None, *, simple: Literal[False]) -> Dict[str, Any]: ...

    @overload
    def get_all_balances(self, address: Optional[str], simple: Literal[False]) -> Dict[str, Any]: ...

    @overload
    def get_all_balances(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]: ...

    def get_all_balances(self, address: Optional[str] = None, simple: bool = True) -> Union[Decimal, Dict[str, Any]]:
        """The total value of an account in USD: perp plus spot balance for standard accounts; the
        spot balance alone for unified accounts, whose perp collateral is the spot USDC.

        Returns:
            The total. With simple=False a dict:
            - total_balance: Total account value in USD
            - account_mode: "standard" or "unified"
            - perp: get_perp_balance(simple=False); in unified mode its balance is part of the
              spot balance, not added to it
            - spot: get_spot_balance(simple=False)

        Raises:
            UnsupportedAccountModeException: For portfolio-margin accounts and unknown account
                modes.
        """
        address = self._address(address)

        mode = self._balance_mode(address)
        if simple:
            spot_total = self.get_spot_balance(address, simple=True)
            return spot_total if mode == "unified" else self._perp_balance(address, mode, True) + spot_total

        spot = self.get_spot_balance(address, simple=False)
        usdc = spot.tokens.get("USDC")
        perp = self._perp_balance(address, mode, False, spot_usdc=usdc.amount if usdc else Decimal("0"))
        perp_value = perp['balance'] if mode == "standard" else Decimal("0")
        return {
            'total_balance': perp_value + spot.total_balance,
            'account_mode': mode,
            'perp': perp,
            'spot': spot,
        }

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

    def get_portfolio(self, address: Optional[str] = None) -> dict:
        """A wallet's portfolio as Hyperliquid answers the portfolio request: the account value
        and PnL history and the volume, per period."""
        address = self._address(address)

        return self._with_retry(self.info.post, "/info", {"type": "portfolio", "user": address})

    def calculate_position_size(self, symbol: str, risk_amount: Decimal, stop_loss_price: Decimal) -> Decimal:
        """The position size that loses `risk_amount` USD when the price goes from the current
        mid to `stop_loss_price`, rounded to the market's size decimals.

        Raises:
            AuthenticationRequiredException: If the client has no account
            InvalidArgumentException: If the stop loss price equals the current price
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        current_price = Decimal(str(self.get_price(symbol)))

        if current_price > stop_loss_price:  # Long position
            risk_per_unit = current_price - stop_loss_price
        elif current_price < stop_loss_price:  # Short position
            risk_per_unit = stop_loss_price - current_price
        else:
            raise InvalidArgumentException("Stop loss price cannot be equal to current price")

        position_size = risk_amount / risk_per_unit

        if symbol in self.market_specs:
            size_decimals = self.market_specs[symbol]["size_decimals"]

            position_size = position_size.quantize(Decimal('0.' + '0' * size_decimals))

        return position_size

    def set_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> dict:
        """Set the leverage of a market, with cross (default) or isolated margin. Returns
        Hyperliquid's answer.

        Raises:
            InvalidArgumentException: If leverage is below 1 or above the market's maximum, or
                the market does not allow cross margin (is_cross=True on an isolated-only market)
            ActionRejectedException: If the exchange rejects the update
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
        _answers.action_result(result, f"{symbol} leverage update")
        return result

    def add_isolated_margin(self, symbol: str, amount: float) -> dict:
        """Add `amount` USD of margin to an isolated position. Returns Hyperliquid's answer.

        Raises:
            InvalidArgumentException: If amount is not greater than zero. The exchange treats
                a negative amount as removing margin, so it is never sent by accident.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        self._require_symbol(symbol)  # else the SDK fails with a bare KeyError while signing
        if not _inputs.positive_number(amount):
            raise InvalidArgumentException(f"amount must be greater than 0, got {amount!r}")
        result = self._signed("isolated margin update", self.exchange.update_isolated_margin, amount, symbol)
        _answers.action_result(result, f"{symbol} isolated margin update")
        return result

    def transfer_to_spot(self, amount: float) -> dict:
        """Transfer `amount` USDC from the perp account to the spot account.

        This does not work with API wallets: use the main wallet's private key.
        """
        return self._usd_class_transfer(amount, to_perp=False)

    def transfer_to_perp(self, amount: float) -> dict:
        """Transfer `amount` USDC from the spot account to the perp account.

        This does not work with API wallets: use the main wallet's private key.
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
        _answers.action_result(result, action)
        return result

    # ── Orders ──────────────────────────────────────────────────────────────

    def _format_price(self, symbol: str, price: float) -> float:
        """Format a price for the exchange, as Hyperliquid's price rule allows (_inputs.round_price).

        Uses 5 significant figures, then rounds to (6 - sz_decimals) decimal places
        for perps. This naturally handles all price ranges: BTC gets ~1 decimal,
        micro-caps get ~6 decimals. From 100000 on, prices are rounded to an integer.
        """
        sz_decimals = self.market_specs.get(symbol, {}).get("size_decimals", 0)
        return _inputs.round_price(price, 6 - sz_decimals)

    def _format_size(self, symbol: str, size: float) -> float:
        """Format a size for the exchange, rounding to sz_decimals."""
        sz_decimals = self.market_specs.get(symbol, {}).get("size_decimals", 0)
        return _inputs.round_down(size, sz_decimals)

    def _validate_and_format_order(
        self,
        symbol: str,
        size: float,
        limit_price: float
    ) -> tuple[float, float]:
        """Validate and format order size and price using Hyperliquid SDK logic.

        - Sizes are rounded to sz_decimals (from market specs)
        - Prices are rounded to 5 significant figures, then to (6 - sz_decimals) decimal places;
          from 100000 on to an integer
        - Validates minimum position sizes based on market specs, and that the price (limit or
          trigger, always required) is a positive number that stays one after rounding: not 0,
          not infinite (InvalidArgumentException)

        Rounded values come back unchanged, so preparing an order twice changes nothing.
        """
        self._check_order_size(symbol, size)

        size = self._format_size(symbol, size)

        if not _inputs.positive_number(limit_price):
            raise InvalidArgumentException(f"price must be a positive number, got {limit_price!r}")
        price = self._format_price(symbol, limit_price)
        if not _inputs.positive_number(price):  # rounds to 0
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
        if not _inputs.positive_number(size):
            raise InvalidArgumentException(f"size must be a positive number, got {size!r}")
        size_decimals = self.market_specs[symbol].get("size_decimals", 0)
        min_size = 1.0 / (10 ** size_decimals) if size_decimals > 0 else 1
        if size < min_size:
            raise InvalidArgumentException(
                f"Minimum position size for {symbol} is {_inputs.plain(min_size)} "
                f"(size_decimals={size_decimals}). Got: {_inputs.plain(size)}"
            )

    @staticmethod
    def _resolve_time_in_force(time_in_force: str, post_only: bool) -> TimeInForce:
        """Time in force for a limit order. post_only is Hyperliquid's "Alo" (add liquidity only)."""
        if time_in_force not in _inputs.TIMES_IN_FORCE:
            raise InvalidArgumentException(f"time_in_force must be Gtc, Ioc or Alo, got {time_in_force!r}")
        if not post_only:
            return cast(TimeInForce, time_in_force)
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

        The size is rounded down to the market's lot step, the price rounded to 5 significant
        figures (from 100000 on to an integer), which can round up. Without limit_price it is
        the price of a market order: the mid price plus or minus the slippage (slippage=, else
        the client's default_slippage), read now. create_order() prepares every order with this
        method, and prepared values come back unchanged, so placing the returned size and price
        (with time_in_force="Ioc" for a market order) sends the same order.

        Returns:
            (size, limit_price) as they are signed.

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
        """Place an order: with limit_price a limit order, without it a market order.

        Args:
            size: In the base currency; rounded down to the market's lot step.
            limit_price: If None, a market order: the mid price plus or minus the slippage
                (slippage=, else the client's default_slippage).
            post_only: Sent as time in force "Alo"; the exchange rejects the order if it would
                match immediately. Only valid for limit orders.
            time_in_force: "Gtc" (good till canceled), "Ioc" (immediate or cancel) or "Alo" (add
                liquidity only). Default: "Ioc" without limit_price (a market order, as in the
                SDK's market_open and buy()/sell()), "Gtc" with one.
            slippage: For a market order, overrides default_slippage.
            cloid: Client order id to place the order with: "0x" and 32 hex digits, unique per
                order. If None, a random one. It is the order's ``cloid``; look the order up or
                cancel it by it with get_order_status_by_cloid() and cancel_order_by_cloid().

        Raises:
            OrderRejectedException: The exchange refused the order (InsufficientMarginException
                for lack of margin).
            ActionOutcomeUnknownException: The order was sent, but whether it was placed is not
                known. Do not send it again before looking it up, by its cloid with
                get_order_status_by_cloid().
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        order_cloid = _inputs.order_cloid(cloid)
        if time_in_force is None:
            time_in_force = "Ioc" if limit_price is None else "Gtc"
        time_in_force = self._resolve_time_in_force(time_in_force, post_only)

        # The size and price to sign: rounded, and for a market order the mid ± slippage
        size, limit_price = self.prepare_order(symbol, size, is_buy, limit_price, slippage)
        logger.debug(f"Formatted order: symbol={symbol}, size={size}, limit_price={limit_price}")

        order_type = {"limit": {"tif": time_in_force}}

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
            return _answers.order_from_response(response, symbol, is_buy, size, limit_price, order_type, reduce_only,
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
        """Buy: a market order, or with limit_price a limit order. The arguments, result and
        errors are create_order()'s."""
        time_in_force: TimeInForce = "Gtc" if limit_price is not None else "Ioc"
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
        """Sell: a market order, or with limit_price a limit order. The arguments, result and
        errors are create_order()'s."""
        time_in_force: TimeInForce = "Gtc" if limit_price is not None else "Ioc"
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
            Hyperliquid's answer. Its statuses have one entry per order, in order:
            {"resting": {"oid"}}, {"filled": {"oid", "totalSz", "avgPx"}}, or
            {"error": text} for an order the exchange rejected; the others are placed.

        Raises:
            InvalidArgumentException: If any order is invalid; checked before anything is sent
            OrderRejectedException: If the exchange refused the whole action, also with a
                single error for several orders (an error of the payload itself, such as a
                price too far from the reference price: none of them was placed)
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
            if time_in_force not in _inputs.TIMES_IN_FORCE:
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
                request["cloid"] = _inputs.cloid(o["cloid"])
            order_requests.append(request)

        response = self._signed("bulk order", self.exchange.bulk_orders, order_requests)
        if isinstance(response, dict) and response.get("status") == "err":  # refused as a whole
            raise OrderRejectedException("bulk order", str(response.get("response")))
        statuses = _answers.status_list(response)
        refused = _answers.batch_error(statuses, len(order_requests))
        if refused is not None:  # nothing was placed
            raise OrderRejectedException("bulk order", refused)
        if (statuses is None or len(statuses) != len(order_requests)
                or not all(_answers.is_documented_status(status) for status in statuses)):
            raise ActionOutcomeUnknownException("bulk order", f"answered in no documented format: {response}")
        return response

    def modify_order(
        self,
        order_id: Union[int, str],
        symbol: str,
        is_buy: bool,
        size: float,
        price: float,
        order_type: Dict,
        reduce_only: bool = False,
        cloid: Optional[str] = None,
    ) -> Order:
        """Modify an open order: it then has this side, size, price, order type and reduce-only
        flag.

        Args:
            order_type: As the SDK takes it, e.g. {"limit": {"tif": "Gtc"}}.
            cloid: The modified order's client order id, as the SDK's modify_order takes one;
                without it a new random one. Returned in Order.cloid.

        Returns:
            The order after the modify: open, or filled if the new price matched at once.

        Raises:
            OrderRejectedException: If the exchange rejected the modify (its text is in the
                message)
            OrderOutcomeUnknownException: If the modify was sent but its outcome is unknown;
                look the order up by its new cloid (get_order_status_by_cloid) before trying
                again
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        oid = _inputs.order_id(order_id)
        order_cloid = _inputs.order_cloid(cloid)
        size, price = self._validate_and_format_order(symbol, size, price)
        try:
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
                cloid=order_cloid,
            )
            # Read like the answer to an order: resting, filled (the new price matched at once) or
            # error. A limit order is no position TP/SL (those are triggers); a trigger's is not known.
            return _answers.order_from_response(response, symbol, is_buy, size, price, order_type, reduce_only,
                                                f"{symbol} order modify", cloid=order_cloid,
                                                is_position_tpsl=None if "trigger" in order_type else False)
        except OrderOutcomeUnknownException:
            raise
        except ActionOutcomeUnknownException as exc:  # named by its new cloid, to look it up
            raise OrderOutcomeUnknownException(
                symbol, order_cloid.to_raw(), "the modify was sent, but its answer was lost or in no documented format"
            ) from exc

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
            fallback: What to do with the remaining size on timeout. One of:
                - "ioc": Place an IOC market order for remaining size (default)
                - "market": Place a market order for remaining size
                - "cancel": Place nothing more; the last attempt is returned with status
                  "canceled"
            bbo_cache: The best bid and ask per symbol, {"BTC": {"bid": ..., "ask": ...}}, kept
                current by the caller (e.g. from a websocket): used instead of requesting the
                order book.
            fill_event: An Event the caller sets when the order may have filled (e.g. from a
                websocket): the wait before the next re-pricing then ends at once.

        Returns:
            The final order. Check order.status and order.filled_size for results.

        Raises:
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
        return _maker.MakerChase(self, symbol, is_buy, size, timeout, reprice_interval, fallback, reduce_only,
                                 bbo_cache, fill_event).run()

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
        """maker_order() for a buy."""
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
        """maker_order() for a sell."""
        return self.maker_order(symbol, False, size, timeout, reprice_interval, fallback, reduce_only,
                                bbo_cache=bbo_cache, fill_event=fill_event)

    def get_open_orders(self, symbol: Optional[str] = None, *, address: Optional[str] = None) -> List[Order]:
        """The open orders of a wallet on the client's dexes (spot orders come with the default
        dex), optionally of one symbol (then only its dex is read).

        size is the size placed, remaining_size the size still open. Hyperliquid also shrinks a
        resting reduce-only order to the position, so for such an order with less open than
        placed, filled_size (their difference) may not be fills: filled_size_known is then False
        (the order's fills tell).
        """
        address = self._address(address)

        # Failures propagate: never an empty list for orders that could not be read
        open_orders_response = []
        for dex in self._dexes_of(symbol):
            open_orders_response.extend(self._with_retry(
                self.info.frontend_open_orders, address, dex=dex
            ))

        if symbol:
            open_orders_response = [order for order in open_orders_response if order["coin"] == symbol]

        return [_answers.parse_open_order(order_data, order_data.get("coin", "")) for order_data in open_orders_response]

    def get_open_order_by_id(self, symbol: str, order_id: Union[str, int], *,
                             address: Optional[str] = None) -> Optional[Order]:
        """The open order with this id, or None.

        Only searches currently open orders: None also for an order that is already filled or
        cancelled. Use get_order_status() to query any order regardless of state.
        """
        oid = _inputs.order_id(order_id)
        orders = self.get_open_orders(symbol, address=address)
        for order in orders:
            if order.order_id == oid:
                return order
        return None

    def has_active_orders(self, symbol: Optional[str] = None, *, address: Optional[str] = None) -> bool:
        """Whether the wallet has open orders, optionally only those in `symbol`."""
        orders = self.get_open_orders(symbol, address=address)
        return len(orders) > 0

    def get_order_status(self, order_id: Union[int, str], *, address: Optional[str] = None) -> dict:
        """The status of an order by its order id, as Hyperliquid answers it (the SDK's
        query_order_by_oid)."""
        oid = _inputs.order_id(order_id)
        address = self._address(address)
        return self._with_retry(self.info.query_order_by_oid, address, oid)

    def get_order_status_by_cloid(self, cloid: str, *, address: Optional[str] = None) -> dict:
        """The status of an order by the client order id it was placed with ("0x" and 32 hex
        digits, the order's Order.cloid; the SDK's query_order_by_cloid), in the format of
        get_order_status(): {"status": "unknownOid"} if the wallet has no order with this
        client order id."""
        client_order_id = _inputs.cloid(cloid)
        address = self._address(address)
        return self._with_retry(self.info.query_order_by_cloid, address, client_order_id)

    def get_historical_orders(self, *, address: Optional[str] = None) -> List[Order]:
        """A wallet's recent orders with their status (Hyperliquid's historicalOrders: at most
        its 2000 most recent entries), newest status first.

        Each order has Hyperliquid's status (open, filled, canceled, triggered, rejected,
        reduceOnlyCanceled, ...: the order statuses of the info endpoint docs) and status_time,
        when it got it; size is the size placed and remaining_size what was open then. Orders of
        every dex and spot orders, whatever dexes the client reads. An order can be listed more
        than once (seen: once as open, at the time it was placed, and once with its final
        status), but the list is not a complete log of status changes.
        """
        address = self._address(address)
        orders = []
        for entry in self._with_retry(self.info.historical_orders, address):
            order = _answers.parse_open_order(entry["order"], entry["order"].get("coin", ""))
            order.status, order.status_time = entry["status"], entry.get("statusTimestamp")
            orders.append(order)
        return orders

    def cancel_order(self, order_id: Union[str, int], symbol: str) -> bool:
        """Cancel an order by its order id.

        Returns:
            True if the order was canceled, False if it is not open: never placed, already
            canceled or filled.

        Raises:
            OrderException: If the exchange refused the cancel, or its answer does not confirm
                it: then it is not known whether the order was canceled
            HyperliquidException: If no answer came (NetworkException, TimeoutException, ...):
                the cancel may still have been executed. Check the order with
                get_order_status() before relying on it being open.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        canceled = self._cancel_one(symbol, _inputs.order_id(order_id))
        if canceled:
            logger.info(f"Successfully cancelled order {order_id} for {symbol}")
        else:
            logger.info(f"Order {order_id} for {symbol} not found or already processed")
        return canceled

    def cancel_order_by_cloid(self, cloid: str, symbol: str) -> bool:
        """Cancel an order by the client order id it was placed with (the SDK's cancel_by_cloid):
        "0x" and 32 hex digits, the order's Order.cloid. Returns and raises as cancel_order()."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        return self._cancel_one(symbol, _inputs.cloid(cloid))

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        """Cancel all open perp orders on the client's dexes (perp_dexs), stop losses and take
        profits included, optionally of one symbol (then only its dex is read). Spot orders are
        left open: spot_cancel_all_orders() cancels those.

        Raises:
            UnknownSymbolException: If symbol is not a market of the client (nothing is read)
            OrdersNotCanceledException: Naming each order that is still open: the exchange did
                not cancel it, or its market is unknown to the client (listed since the client
                was created: refresh_market_specs(), then cancel again). The others are canceled.
            HyperliquidException: If the open orders cannot be read; then nothing is cancelled
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
        canceled: List[Any] = []
        not_open: List[Any] = []
        if known:
            try:
                response = self._cancel_all([{"coin": order["coin"], "oid": order["oid"]} for order in known])
            except OrdersNotCanceledException as exc:
                failed, canceled, not_open = exc.failed + failed, exc.canceled, exc.not_open
            else:
                statuses = _answers.status_list(response) or []
                canceled = [order["oid"] for order, status in zip(known, statuses) if status == "success"]
                not_open = [order["oid"] for order, status in zip(known, statuses) if status != "success"]
        if failed:
            raise OrdersNotCanceledException(failed, canceled=canceled, not_open=not_open)

    def cancel_all(self) -> None:
        """Cancel all open perp orders on the client's dexes (perp_dexs), stop losses and take
        profits included; spot orders stay (spot_cancel_all_orders() cancels those).

        Alias for cancel_all_orders(). Prefer cancel_all_orders() for new code.
        """
        return self.cancel_all_orders()

    def bulk_cancel(self, cancels: List[dict]) -> dict:
        """Cancel several orders in one action (the SDK's bulk_cancel).

        Args:
            cancels: List of dicts, each with keys:
                - symbol (str): Trading pair symbol
                - order_id (str or int): The order ID to cancel

        Returns:
            Hyperliquid's answer, once every order is canceled or no longer open. Its statuses
            have one entry per cancel, in order: "success" (canceled), or the error "Order was
            never placed, already canceled, or filled." A cancel is sent again when its answer
            was lost (it is idempotent); an order the first attempt canceled then shows that
            error too.

        Raises:
            InvalidArgumentException: If a cancel lacks its symbol or order id, or its symbol
                is not a market of the client (UnknownSymbolException); checked before
                anything is sent
            OrdersNotCanceledException: If the exchange did not cancel some of the orders
                (its failed names each, with the reason); the others are canceled or gone
            OrderException: If the answer does not confirm the cancels (an error status, no
                status per cancel, or a status in no documented format: that order's outcome
                is not known)
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        cancel_requests = []
        for c in cancels:
            missing = [key for key in ("symbol", "order_id") if key not in c]
            if missing:
                raise InvalidArgumentException(f"bulk_cancel: a cancel lacks {', '.join(missing)}: {c}")
            self._require_symbol(c["symbol"])  # else the SDK fails with a bare KeyError while signing
            cancel_requests.append({"coin": c["symbol"], "oid": _inputs.order_id(c["order_id"])})
        return self._cancel_all(cancel_requests)

    # ── Positions, stop losses and take profits ─────────────────────────────

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
            stop_loss_price: A stop loss for the whole position.
            take_profit_price: A take profit for the whole position.
            limit_price: Limit price of the entry; None for a market order.
            slippage: For a market entry, overrides default_slippage.
            stop_losses: Instead of stop_loss_price, several stop losses as (price, fraction):
                [(95, 0.5), (90, 0.5)] closes half the position at each. The fractions add up
                to at most 1.
            take_profits: Instead of take_profit_price, several take profits, the same way.
            time_in_force: With limit_price, "Gtc" (default) rests until filled, "Alo" rests
                as maker only; "Ioc" fills what it can at once and cancels the rest (a market
                entry with a price limit).

        An entry that executes at once (market, or limit_price with "Ioc"): each stop loss and
        take profit is placed once it fills, for its fraction of the filled size, rounded down
        to the lot size, the one nearest the entry of the stop losses (and of the take profits)
        taking what rounding left, so together they cover their share exactly, and a partial
        fill never loses more at its stops than the full size would.

        A resting limit entry ("Gtc", "Alo"): one stop loss and one take profit, sent attached
        to it as Hyperliquid's app attaches them. Hyperliquid places them for the entry's size
        once it has filled completely (or its rest is canceled for lack of margin); until then
        they are in the entry's Order.children, a partly filled entry's filled part has none,
        and canceling the entry cancels them.

        Returns:
            "entry", then "stop_loss" and "take_profit" with the single prices, or
            "stop_loss_1", "stop_loss_2", ... and "take_profit_1", ... with lists. For a
            resting entry only "entry".

        Raises:
            InvalidArgumentException: If a stop loss or take profit is on the wrong side of the
                entry (the mid price, or a resting entry's limit price), the fractions add up
                to more than 1, a resting entry has more than one of each, or one would be
                worth less than Hyperliquid's minimum order value of 10 USD at its price.
                Checked before any order is sent.
            ProtectionOrderException: If the entry was placed but a stop loss or take profit
                was not: placing it raised an error (an unknown outcome is marked, with its
                cloid: it may still arrive), Hyperliquid refused to attach it, or it executed
                at once because the price had already crossed it. It carries the orders
                placed and those missing.

        A level whose share rounds to 0 at a small partial fill is not placed (its part goes
        to the nearest of its kind, so the coverage stays): compare the returned keys.
        """
        return _protection.open_position(
            self, symbol, size, True, stop_loss_price, take_profit_price, limit_price, slippage,
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
        open_long_position (stop losses above the entry, take profits below it): the same
        arguments, result and errors."""
        return _protection.open_position(
            self, symbol, size, False, stop_loss_price, take_profit_price, limit_price, slippage,
            stop_losses, take_profits, time_in_force
        )

    def trigger_order(
        self,
        symbol: str,
        is_buy: bool,
        size: float,
        trigger_price: float,
        *,
        limit_price: Optional[float] = None,
        cloid: Optional[str] = None,
    ) -> Order:
        """Place a stop order that opens or adds to a position once the price gets there: a buy
        when the mark price rises to trigger_price, a sell when it falls to it.

        Without limit_price it executes as a market order within Hyperliquid's 10 % tolerance
        for market triggers (sent as its limit price), so in a gap it can fill well past the
        trigger. With limit_price it becomes a limit order at that price, which in a fast move
        may fill only partly, or not at all. Hyperliquid attaches no stop loss or take profit to
        a stop order ("Main order cannot be trigger order."): place them once it fills.

        Returns:
            The order: "open" while it waits for its trigger (and once triggered while a
            stop limit is partly filled), or "filled" if the price got there meanwhile.

        Raises:
            InvalidArgumentException: Before anything is sent: a buy's trigger_price not above
                the mid price (a sell's not below), the mark price already past it, a buy's
                limit_price below the trigger (a sell's above), or the entry worth less than
                Hyperliquid's 10 USD minimum at its trigger
            OrderRejectedException: If the exchange refused it, as create_order()
            OrderOutcomeUnknownException: If whether it was placed is not known; look it up by
                its cloid before placing it again
        """
        return _protection.trigger_order(self, symbol, is_buy, size, trigger_price, limit_price, cloid)

    def close(
        self,
        symbol: str,
        position: Optional[Position] = None,
        slippage: Optional[float] = None,
    ) -> Order:
        """Close a position with a reduce-only market order (immediate or cancel).

        Args:
            position: The position to close, if already read; if None it is read now.
            slippage: Overrides default_slippage.

        Returns:
            The closing order, filled for the whole position.

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

    def close_all_positions(self) -> Dict[str, Order]:
        """Close all open positions, each with close().

        Every position is tried, also after one fails to close.

        Returns:
            The closing order per symbol, when every position closed.

        Raises:
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
        """Place a stop loss for an open position: a reduce-only order that closes `size` at
        market when trigger_price is reached. A rejection or an unknown outcome raises as in
        create_order().

        Args:
            is_buy: Side of the order. Omit it to derive the side from the position (sell for a
                long, buy for a short); a value that would not close the position raises
                ValueError.
            cloid: Client order id, see create_order().

        Raises:
            PositionNotFoundException: If there is no position in `symbol`
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
        """Place a take profit for an open position: a reduce-only order that closes `size` at
        market when trigger_price is reached. A rejection or an unknown outcome raises as in
        create_order().

        Args:
            is_buy: Side of the order. Omit it to derive the side from the position (sell for a
                long, buy for a short); a value that would not close the position raises
                ValueError.
            cloid: Client order id, see create_order().

        Raises:
            PositionNotFoundException: If there is no position in `symbol`
        """
        return self._protection_order("tp", symbol, size, trigger_price, is_buy, cloid)

    def _protection_order(self, tpsl: str, symbol: str, size: float, trigger_price: float,
                          is_buy: Optional[bool], cloid: Optional[str]) -> Order:
        """Shared implementation of stop_loss ("sl") and take_profit ("tp")."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        order_cloid = _inputs.order_cloid(cloid)
        return self._place_trigger(tpsl, symbol, size, trigger_price, self._closing_side(symbol, is_buy), order_cloid)

    def _place_trigger(self, tpsl: str, symbol: str, size: float, trigger_price: float,
                       is_buy: bool, order_cloid: Cloid) -> Order:
        """Place a reduce-only market trigger order ("sl" or "tp") on the side `is_buy`, which
        the caller derived from the position."""
        size, trigger_price = self._validate_and_format_order(symbol, size, trigger_price)
        worst = self._market_trigger_limit(symbol, trigger_price, is_buy)
        order_type = {"trigger": {"triggerPx": trigger_price, "isMarket": True, "tpsl": tpsl}}
        response = self._place_order(
            symbol,
            order_cloid,
            is_buy=is_buy,
            sz=size,
            limit_px=worst,
            reduce_only=True,
            order_type=order_type,
        )
        name = "stop loss" if tpsl == "sl" else "take profit"
        return _answers.order_from_response(response, symbol, is_buy, size, worst, order_type, True,
                                            f"{symbol} {name}", cloid=order_cloid)

    def _market_trigger_limit(self, symbol: str, trigger_price: float, is_buy: bool) -> float:
        """The limit price of a market trigger order (a stop loss, a take profit, a stop entry):
        beyond its trigger in the direction it executes, by Hyperliquid's documented 10 %
        tolerance for market TP/SL; above it for a buy, below it for a sell. At the trigger it
        could keep a triggered order from filling when the price turns right after it (the
        SDK's example sends a stop beyond its trigger too)."""
        factor = 1 + TPSL_MARKET_TOLERANCE if is_buy else 1 - TPSL_MARKET_TOLERANCE
        return self._format_price(symbol, trigger_price * factor)

    def get_stop_loss_price(self, symbol: str, *, address: Optional[str] = None) -> Optional[Decimal]:
        """The trigger price of the stop loss of the wallet's position in `symbol`: a stop
        trigger on the side that closes it (the first one's if there are several). None without
        a position or without one; a stop on the other side is an entry, not a stop loss."""
        return self._protection_price(symbol, "stop_loss", address)

    def get_take_profit_price(self, symbol: str, *, address: Optional[str] = None) -> Optional[Decimal]:
        """The trigger price of the take profit trigger of the wallet's position in `symbol`
        (not a limit order), as get_stop_loss_price()."""
        return self._protection_price(symbol, "take_profit", address)

    def _protection_price(self, symbol: str, kind: str, address: Optional[str]) -> Optional[Decimal]:
        orders = [order for order in self.get_open_orders(symbol, address=address) if order.type == kind]
        if not orders:
            return None
        position = next((p for p in self.get_positions(address=address) if p.symbol == symbol and p.size != 0), None)
        closing = [order for order in orders if position is not None and order.is_buy == position.is_short]
        return closing[0].trigger_price if closing else None

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

        Returns:
            The new stop loss order.

        Raises:
            PositionNotFoundException: If there is no position in `symbol`
            ProtectionReplacementIncompleteException: If the new order was placed but does not
                cover the whole position (it executed at once, partly, triggered since, or the
                position grew), read again by its id after the position, or that could not be
                checked, also when there was no old one; or an old one could not be canceled
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        return _protection.replace(self, symbol, new_price, "stop_loss")

    def update_take_profit(self, symbol: str, new_price: float) -> Optional[Order]:
        """Move the take profit of an existing position to a new trigger price.

        Places the new take profit first, sized to the live position, then cancels the old
        take profit orders. If the new order is rejected or its outcome is unknown, the old
        orders stay in place and the error is raised. If no take profit exists yet, one is
        created. Other signed actions of the key wait while it runs, as in update_stop_loss().

        Returns:
            The new take profit order.

        Raises:
            PositionNotFoundException: If there is no position in `symbol`
            ProtectionReplacementIncompleteException: As in update_stop_loss(): the new order
                was placed but does not cover the whole position, or that could not be checked,
                or an old one could not be canceled
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        return _protection.replace(self, symbol, new_price, "take_profit")

    def resize_protection(
        self,
        symbol: str,
        stop_losses: Optional[Sequence[Tuple[float, float]]] = None,
        take_profits: Optional[Sequence[Tuple[float, float]]] = None,
        *,
        take_profit_type: Literal["trigger", "limit"] = "trigger",
    ) -> ProtectionSync:
        """Resize the stop losses and take profits of an open position to its live size, level
        by level.

        Each list is (price, fraction) per level, as in open_long_position(): the fraction of
        the live position it closes; a list's fractions add up to at most 1 (stop losses that
        add up to less leave part of the position without one). A list given replaces that
        kind's orders on the closing side: a level whose order is open at that price with that
        size is kept as it is (no request); the others are placed and the old orders that
        match no level are cancelled. A list left out (None) leaves that kind as it is. Sizes
        are rounded down to the lot size, the level nearest the price taking what rounding
        left, so a kind's levels cover their share exactly.

        Stop losses are reduce-only trigger market orders, as stop_loss() places them; old stop
        losses of any kind (stop limits too) are replaced by them. Take profits are trigger
        market orders (take_profit_type "trigger", as take_profit()) or resting reduce-only
        post-only limit orders at the level's price ("limit": they fill as maker); one call
        takes one type, and stop limits are not placed. Native position TP/SL (set for the
        whole position in the Hyperliquid app) and orders with TP/SL children are not changed
        by this call: they are in the result's untouched. Hyperliquid itself can still cancel
        such a resting limit when the position gets smaller; take-profit limits that would
        exceed the position together with the untouched resting limits are refused.

        The order of the steps follows how Hyperliquid treats reduce-only orders (seen live,
        2026-11-18): it keeps a position's resting reduce-only limit orders at most as large as
        the position, cancelling an existing one (reduceOnlyCanceled) when a new one would
        exceed it, and when the position gets smaller; untriggered stops do not count.
          - Stop losses, and take profit triggers replacing take profit triggers: the new
            orders first; the old ones are cancelled only once every level of the kind (kept
            or new) is read again open with its size, on an unchanged position, as
            update_stop_loss() does.
          - Take profits where a limit order is involved (an old one or the new ones): the
            old ones are cancelled first, then the new ones placed; if an old one cannot be
            cancelled, none is placed (together they could exceed the position). For that
            moment the position has no take profit, never no stop loss.
        Afterwards the open orders and the position are read again: every level must be open,
        by its order id, with its size, on an unchanged position.

        Everything is checked before anything is sent: each level's price as it will be sent
        (above 0; on the closing side of the mid: a long's stop losses below it and take
        profits above, a short's the other way), its size (at least one lot, worth at least
        Hyperliquid's 10 USD minimum), and no two levels of a kind at the same price.

        Other signed actions of the key wait while it runs, as in update_stop_loss().

        Returns:
            The placed, kept, cancelled and untouched orders, and the position size.

        Raises:
            InvalidArgumentException: Neither list, an empty list, fractions above 1, or a level
                that fails the checks above (nothing sent)
            PositionNotFoundException: If there is no position in `symbol`
            ProtectionSyncIncompleteException: Orders were sent, but the position is not
                protected as asked: a placement failed or its outcome is unknown, an order
                executed at once, an old order could not be cancelled, or a level is not open
                with its size or the position changed. Its attributes say what happened: every
                order placed, kept, cancelled, retained or not cancelled, and every level
                missing (an order's unfilled rest included). Nothing is undone.
        """
        return _protection.resize(self, symbol, stop_losses, take_profits, take_profit_type)

    def trailing_stop(self, symbol: str, trail_percent: float) -> Optional[Order]:
        """Set the stop loss at a percentage distance from the current price: below it for a
        long, above it for a short.

        This is a one-shot update, not a live trailing stop. It calculates the stop price from
        the current market price and trail_percent (2.0 for 2 %), then calls
        update_stop_loss(). To simulate a live trailing stop, call this method repeatedly from
        a loop or scheduler (e.g. every 30 seconds).

        Returns:
            The new stop loss order, or None if no position exists.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        current_price = self.get_price(symbol)

        position_direction = self.get_position_direction(symbol)
        if position_direction is None:
            return None

        if position_direction == "long":
            new_stop_price = current_price * (1 - trail_percent / 100)
        else:
            new_stop_price = current_price * (1 + trail_percent / 100)

        return self.update_stop_loss(symbol, new_stop_price)

    # ── Fills, funding and PnL ──────────────────────────────────────────────

    def get_fills(self, symbol: Optional[str] = None, *, address: Optional[str] = None) -> list:
        """The recent fills of a wallet, optionally only those of one symbol."""
        address = self._address(address)
        raw = self._with_retry(self.info.user_fills, address)
        fills = [_answers.parse_fill(f) for f in raw]
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
        """The fills of a wallet in a time range, oldest first. The exchange keeps only the 10000
        most recent fills of an account.

        Args:
            start_time: Start in milliseconds since the epoch.
            end_time: End in ms. Defaults to now.
        """
        address = self._address(address)
        end = end_time if end_time is not None else int(time.time() * 1000)  # fixed for all pages
        raw, overflow = self._fill_pages(address, start_time, end)
        if overflow:
            logger.warning(f"More than {self._FILLS_PER_PAGE} fills in one millisecond; later fills in the range "
                           "were not returned")
        fills = [_answers.parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return fills

    _FUNDING_PER_PAGE = 500  # userFunding and fundingHistory return at most 500 records, the oldest from startTime

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
            start_time: Start in milliseconds since the epoch (inclusive).
            end_time: End in ms (inclusive). Defaults to now.
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
            start_time: Start in milliseconds since the epoch (inclusive).
            end_time: End in ms (inclusive). Defaults to now.

        Returns:
            The fills oldest first, with truncated and truncated_reason.
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
        fills = [_answers.parse_fill(f) for f in raw]
        if symbol:
            fills = [f for f in fills if f.symbol == symbol]
        return FillHistory(fills=fills, truncated=reason is not None, truncated_reason=reason)

    def get_pnl(
        self,
        start_time: int,
        end_time: Optional[int] = None,
        symbol: Optional[str] = None,
        *,
        spot: bool = False,
        address: Optional[str] = None,
    ) -> PnLReport:
        """A wallet's perp trades of a time range with their PnL, from its fills and funding.

        A trade runs from flat to a position and back to flat: entries, adds and partial exits
        belong to it; a fill that turns a long into a short (or back) closes it and opens the
        next. Per trade: Hyperliquid's closed PnL (before fees), the fees, the funding paid or
        received while it was open, and the net. A trade still open at the end is "open"; when
        the range ends now it has the position's unrealized PnL, and a position without fills
        in the range is an open trade too. A trade open when the range started is marked
        opened_before_start: its entry price covers only the fills in the range. A trade's
        figures are in its currency, its dex's collateral (HIP-3 dexes may use USDH, USDE,
        USDT0, ...): from the market specs, else the token its fees are paid in, else
        "unknown"; the totals add up the USDC trades, the others are in other_currencies.

        Reads: the fill history (see get_fill_history: Hyperliquid keeps only the 10000 most
        recent fills; truncated says so) and the funding payments, both of every dex; with
        end_time None also the positions, of the client's dexes (perp_dexs) only: an open trade
        on another dex has no unrealized PnL. The positions are read first, then the range ends,
        so the fills of a position opened meanwhile are in it.

        Args:
            start_time: Start in milliseconds since the epoch (inclusive).
            end_time: End in ms (inclusive). Defaults to now.
            symbol: Only this perp market.
            spot: Also each spot market's closed PnL (in its quote token) and fees by token.

        Returns:
            The trades, oldest first, and their totals.
        """
        address = self._address(address)
        ends_now = end_time is None
        positions = None
        if ends_now:  # read before the end is fixed: the fills that made them are then in the range
            positions = [p for p in self.get_positions(address=address) if not symbol or p.symbol == symbol]
        end = int(time.time() * 1000) if end_time is None else end_time
        if end < start_time:
            raise InvalidArgumentException("the range ends before it starts")
        history = self.get_fill_history(start_time, end, address=address)
        funding = self.get_user_funding(start_time, end, symbol, address=address)
        fills = [f for f in history.fills if not symbol or f.symbol == symbol]
        currencies = {name: spec["collateral"] for name, spec in self.market_specs.items() if spec.get("collateral")}
        trades = pnl.perp_trades(fills, funding, start_time, positions, currencies)
        spot_figures = pnl.spot_markets(history.fills, self._spot_coin_symbols()) if spot else None
        return pnl.report(trades, start_time, end, history.truncated, history.truncated_reason, spot_figures)

    # ── Spot ────────────────────────────────────────────────────────────────

    @overload
    def get_spot_balance(self, address: Optional[str] = None, simple: Literal[True] = True,
                         prices: Optional[Dict[str, float]] = None) -> Decimal: ...

    @overload
    def get_spot_balance(self, address: Optional[str] = None, *, simple: Literal[False],
                         prices: Optional[Dict[str, float]] = None) -> SpotState: ...

    @overload
    def get_spot_balance(self, address: Optional[str], simple: Literal[False],
                         prices: Optional[Dict[str, float]] = None) -> SpotState: ...

    @overload
    def get_spot_balance(self, address: Optional[str] = None, simple: bool = True,
                         prices: Optional[Dict[str, float]] = None) -> Union[Decimal, SpotState]: ...

    def get_spot_balance(self, address: Optional[str] = None, simple: bool = True,
                         prices: Optional[Dict[str, float]] = None) -> Union[Decimal, SpotState]:
        """The value of a wallet's spot balances in USD.

        Each token is valued at the mid of its own spot pair quoted in USDC (found by its token
        index, so never at the price of a perp or token with the same name); USDC at 1, a token
        without such a pair or mid at 0.

        Args:
            prices: The prices as get_price() returns them, if already read: they are then not
                requested again, e.g. when reading several wallets.

        Returns:
            The total in USD. With simple=False a SpotState: total_balance, tokens (a
            SpotTokenBalance per token) and raw_state (Hyperliquid's answer).
        """
        address = self._address(address)

        response = self._with_retry(self.info.spot_user_state, address)

        if prices is None:
            prices = self.get_price()

        usdc_pairs = self._spot_usdc_pairs()
        total_balance = Decimal('0')
        token_balances: Dict[str, SpotTokenBalance] = {}

        for balance in response.get('balances', []):
            token = balance.get('coin')
            if not token:
                continue

            token_amount = Decimal(str(balance.get('total', '0')))
            if token_amount == 0:
                continue

            # The mid of the token's own pair quoted in USDC, found by the balance's token
            # index (spot mids are keyed by pair, e.g. "@107"); USDC itself is 1, a token
            # without such a pair 0 (unpriced). Never a perp's price by the token's name: it can
            # be another asset (spot TRUMP traded at 0.0007 USDC, the TRUMP perp at 0.95).
            index = balance.get('token')
            token_price = (Decimal(1) if index == _USDC_TOKEN
                           else Decimal(str(prices.get(usdc_pairs.get(index, ''), '0'))))

            usd_value = token_amount * token_price
            total_balance += usd_value

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

    def _spot_usdc_pairs(self) -> Dict[int, str]:
        """Each spot token's pair quoted in USDC, by token index: the coin its mid is listed
        under ("PURR/USDC", "@107"). From the spot meta of the client's markets (no request)."""
        pairs: Dict[int, str] = {}
        for pair in self._spot_meta["universe"]:
            base, quote = pair["tokens"]
            if quote == _USDC_TOKEN:
                pairs.setdefault(base, pair["name"])
        return pairs

    def _resolve_spot_pair(self, token: str) -> str:
        """The spot pair the SDK knows (a name in info.name_to_coin) for `token`: "PURR/USDC" for
        the token name "PURR", a full pair name ("HYPE/USDT0") for itself. Raises
        UnknownSymbolException if no spot pair is found."""
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
        """Format a price for a spot order: 5 significant figures, then (8 - sz_decimals)
        decimal places; from 100000 on an integer (_inputs.round_price)."""
        sz_decimals = self._get_spot_sz_decimals(pair_name)
        return _inputs.round_price(price, 8 - sz_decimals)

    def _format_spot_size(self, pair_name: str, size: float) -> float:
        """Format a size for a spot order."""
        sz_decimals = self._get_spot_sz_decimals(pair_name)
        return _inputs.round_down(size, sz_decimals)

    def get_spot_price(self, token: str) -> float:
        """The current mid price of a spot token."""
        pair_name = self._resolve_spot_pair(token)
        coin = self.info.name_to_coin[pair_name]
        mids = self._with_retry(self.info.all_mids)
        # Spot mids are keyed by the internal coin name
        price_str = mids.get(coin)
        if price_str is None:
            raise HyperliquidException(f"Hyperliquid has no price for {token} (pair {pair_name}, coin {coin})")
        return float(price_str)

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
        day volume, in the quote token; in Hyperliquid's order of the pairs. One request.

        The symbol is the one the spot methods take and report: the base token for a pair
        quoted in USDC ("HYPE"), else "BASE/QUOTE" ("HYPE/USDT0").
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

    def get_spot_order_book(self, token: str, *, decimal: bool = False) -> Dict[str, Any]:
        """The order book of a spot token, in the format of get_order_book() (`decimal` as
        there)."""
        pair_name = self._resolve_spot_pair(token)
        return self.get_order_book(pair_name, decimal=decimal)

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
        """Place a spot order: with limit_price a limit order, without it a market order.

        Args:
            post_only: Maker only (limit orders only).
            time_in_force: "Gtc", "Ioc", or "Alo". Defaults to "Ioc" for a market order (it
                never rests on the book), else "Gtc", as in create_order().
            slippage: For a market order, overrides default_slippage.
            cloid: Client order id, see create_order().

        Raises:
            InvalidArgumentException: If size or limit_price is not a positive number, or
                either is too small for the pair; checked before anything is sent.
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        order_cloid = _inputs.order_cloid(cloid)
        if time_in_force is None:
            time_in_force = "Ioc" if limit_price is None else "Gtc"
        time_in_force = self._resolve_time_in_force(time_in_force, post_only)
        if not _inputs.positive_number(size):
            raise InvalidArgumentException(f"size must be a positive number, got {size!r}")
        if limit_price is not None and not _inputs.positive_number(limit_price):
            raise InvalidArgumentException(f"limit_price must be a positive number, got {limit_price!r}")

        pair_name = self._resolve_spot_pair(token)

        if limit_price is None:
            current_price = self.get_spot_price(token)
            if current_price <= 0:
                raise HyperliquidException(f"Invalid current price for {token}: {current_price}")
            limit_price = self._with_slippage(current_price, is_buy, slippage)

        size = self._format_spot_size(pair_name, size)
        limit_price = self._format_spot_price(pair_name, limit_price)
        if not _inputs.positive_number(limit_price):  # rounds to 0
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
            return _answers.order_from_response(response, token, is_buy, size, limit_price, order_type, False,
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
        """Buy a spot token: a market order, or with limit_price a limit order. The arguments,
        result and errors are create_spot_order()'s."""
        time_in_force: TimeInForce = "Gtc" if limit_price is not None else "Ioc"
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
        """Sell a spot token: a market order, or with limit_price a limit order. The arguments,
        result and errors are create_spot_order()'s."""
        time_in_force: TimeInForce = "Gtc" if limit_price is not None else "Ioc"
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
        """Cancel a spot order by its order id. Returns and raises as cancel_order()."""
        if not self.is_authenticated():
            raise AuthenticationRequiredException()
        oid = _inputs.order_id(order_id)
        return self._cancel_one(self._resolve_spot_pair(token), oid)

    def spot_cancel_all_orders(self, token: Optional[str] = None) -> None:
        """Cancel all open spot orders, optionally only those of one token.

        Raises:
            OrdersNotCanceledException: Naming each order that the exchange did not cancel
        """
        if not self.is_authenticated():
            raise AuthenticationRequiredException()

        # Spot assets have the ids from 10000 on
        spot_coins = {coin for coin, asset in self.info.coin_to_asset.items() if asset >= 10000}

        if token:
            pair_name = self._resolve_spot_pair(token)
            target_coin = self.info.name_to_coin[pair_name]
            spot_coins = {target_coin}

        open_orders = self._with_retry(self.info.open_orders, self.account.public_address)
        spot_orders = [o for o in open_orders if o["coin"] in spot_coins]

        if not spot_orders:
            return

        self._cancel_all([{"coin": order["coin"], "oid": order["oid"]} for order in spot_orders])

    def get_spot_open_orders(self, token: Optional[str] = None, *, address: Optional[str] = None) -> List[Order]:
        """The open spot orders of a wallet, optionally only those of one token."""
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

            order = _answers.parse_open_order(order_data, spot_coin_to_token[coin], market="spot")
            orders.append(order)

        return orders

    def get_spot_fills(self, token: Optional[str] = None, *, address: Optional[str] = None) -> list:
        """The recent spot fills of a wallet, optionally only those of one token."""
        address = self._address(address)

        spot_coin_to_token = self._spot_coin_symbols()
        if token and token.endswith("/USDC"):
            token = token[: -len("/USDC")]  # USDC pairs are reported by their token name

        raw = self._with_retry(self.info.user_fills, address)
        fills = []
        for f in raw:
            fill = _answers.parse_fill(f)
            if fill.market != "spot":
                continue
            # Named as the other spot methods name it; a pair no longer listed keeps
            # Hyperliquid's name (e.g. "@98") instead of being dropped.
            fill.symbol = spot_coin_to_token.get(f["coin"], f["coin"])
            if token and fill.symbol != token:
                continue
            fills.append(fill)
        return fills
