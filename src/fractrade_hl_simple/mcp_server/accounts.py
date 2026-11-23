"""Accounts at run time: which account a tool call acts on, the clients,
the check that an account's key trades that account, the lock trade tools run under, and
the kill switch's halt.

Reads of every wallet go through one client without a key per API URL, passing the address
on each call (library `address=`). Each trading account has its own client with its
key, for trades only. Clients come from the injected factory, on first use or when explicitly
refreshing metadata or enabling dexes.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from eth_account import Account

from ..exceptions import HyperliquidException, InvalidArgumentException, UnknownSymbolException
from ..models import HyperliquidAccount, coin_market
from .config import ADDRESS, TOOL_BASE_URLS, AccountConfig, Config
from .errors import ToolError
from .state import SharedState

class ClientFactory(Protocol):
    """Build an independent client, without reading credentials from the environment.
    `cache_market_specs=False` forces fresh metadata and updates the library's shared cache,
    so later lazy clients use fresh metadata too (or fetch it for a different dex scope).
    """

    def __call__(self, base_url: str, account: Optional[HyperliquidAccount], default_slippage: Optional[float],
                 perp_dexs: tuple[str, ...], *, cache_market_specs: bool = True) -> Any: ...


def _dexes(own: Sequence[str], session: Sequence[str]) -> tuple[str, ...]:
    """A dex list: `own` then those enabled for the session, each once."""
    return tuple(dict.fromkeys([*own, *session]))


def require_enabled_dexes(dexes: Sequence[str], symbols: Sequence[str]) -> None:
    """UnknownSymbolException for the first symbol of a HIP-3 dex not in `dexes`, naming
    the dex and how to enable it, before any client is made."""
    for symbol in symbols:
        dex = symbol.split(":", 1)[0] if ":" in symbol else None
        if coin_market(symbol) == "hip3_perp" and dex not in dexes:
            raise UnknownSymbolException(f"Unknown symbol {symbol!r}; the {dex} dex is not enabled; "
                                         "enable_perp_dexs adds it")


def signer_address(key: str) -> str:
    """The address of a private key: the signer of every action signed with it."""
    try:
        return str(Account.from_key(key).address)
    except Exception:  # its text (and that of its context) can hold the key
        pass
    # Raised outside the except block, so the original error is not even its __context__.
    raise ToolError("SIGNER_NOT_AUTHORIZED", "the key is not a valid private key")


_ROLES = {
    "missing": "Hyperliquid does not know it (an API wallet that expired or was removed, or was never approved)",
    "user": "it is a wallet of its own, which can withdraw and transfer (and orders signed with it would "
            "trade that wallet)",
}


def signer_problem(config: AccountConfig, signer: str, client: Callable[[], Any]) -> Optional[str]:
    """Why the key with address `signer` must not trade `config.address`, or None if it may.
    The server signs only with keys this check confirmed as API wallets: Hyperliquid lets an API wallet
    sign trading actions, but a withdrawal, a transfer or the approval of an API wallet only
    with the account's own key. So the account's own key is refused, and any other key must
    be an API wallet (Hyperliquid's `userRole`: "agent"): of the account itself, or, for a
    vault or sub-account, of any account (each order names the vault or sub-account, so the
    exchange executes it for that account or rejects it). `client` makes the read client for
    the lookup. Raises the library's exception if the lookup fails."""
    if signer.lower() == config.address.lower():
        return (f"the key of account {config.name!r} is the account's own key ({signer}), which can withdraw "
                "and transfer: this server trades only with an API wallet")
    answer = client().get_user_role(signer)
    role = answer.get("role") if isinstance(answer, dict) else None
    data = answer.get("data") if isinstance(answer, dict) else None
    owner = data.get("user") if isinstance(data, dict) else None
    valid_owner = isinstance(owner, str) and ADDRESS.fullmatch(owner) is not None
    if role == "agent" and valid_owner and (config.vault or str(owner).lower() == config.address.lower()):
        return None
    if role == "agent" and valid_owner:
        what = f"it is an API wallet of another account ({owner})"
    elif role == "agent":  # an answer in no documented shape fails closed
        what = f"Hyperliquid answered an API wallet without a valid account: {answer!r}"
    elif isinstance(role, str):
        what = _ROLES.get(role, f"Hyperliquid reports its role as {role!r}")
    else:
        what = f"Hyperliquid answered {answer!r}"
    return f"the key of account {config.name!r} ({signer}) is no API wallet of {config.address}: {what}"


SIGNER_HINT = ("create an API wallet for the account in the Hyperliquid app (API), approve it and paste its key "
               "into the account's key file; the server never signs with an account's own key. A new key is read "
               "when a server starts: start a new chat (or restart the assistant); a key that was only not yet "
               "authorized works at the next call")


@dataclass(frozen=True)
class Target:
    """The account a tool call acts on."""

    account: Optional[AccountConfig]  # None for an address that is not configured
    address: str
    base_url: str

    @property
    def name(self) -> Optional[str]:
        return None if self.account is None else self.account.name

    @property
    def key(self) -> tuple[str, str]:
        """The exchange account: two configured names for one address share it."""
        return (self.address.lower(), self.base_url)


class AccountRegistry:
    """Resolves tool arguments to a `Target` and holds the per-account run-time state.

    `keys` holds the private keys of the accounts allowed to trade, by account name; the
    server passes none when trading is switched off. Every other client is watch-only.
    `state` holds what every server of the config shares (the account locks, halts, order
    times and cloids: state.py); required when there are keys.
    """

    def __init__(self, config: Config, keys: Mapping[str, str], make_client: ClientFactory,
                 state: Optional[SharedState] = None) -> None:
        self._config = config
        self._keys = {a.name: keys[a.name] for a in config.accounts.values() if a.trading and a.name in keys}
        self._make_client = make_client
        if self._keys and state is None:
            raise ValueError("an account that can trade needs the shared state")
        self._state = state  # a read-only server's is not started: it only reads the halts
        self._local_halts: set[tuple[str, str]] = set()  # halted by this server: holds if the file failed
        self._readers: dict[str, Any] = {}  # by base_url
        self._traders: dict[str, Any] = {}  # by account name
        # Client creation fetches market metadata, so it has its own lock: the halt and the
        # account locks below must never wait for the network.
        self._create_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # Per server, on purpose: the last signer check (used only when a lookup for a call
        # that reduces risk fails), and the dexes enable_perp_dexs adds for this session.
        self._signer_confirmed: set[str] = set()  # account names whose key was confirmed
        self._session_dexes: dict[str, tuple[str, ...]] = {}  # by base_url: enable_perp_dexs

    @property
    def trading_enabled(self) -> bool:
        """Whether any account can trade (its key was loaded)."""
        return bool(self._keys)

    def can_trade(self, name: str) -> bool:
        """Whether the configured account `name` can trade."""
        return name in self._keys

    # ── resolution ──────────────────────────────────────────────────

    def resolve_read(self, account: Optional[str] = None, address: Optional[str] = None,
                     base_url: Optional[str] = None) -> Target:
        """The target of a read tool: the named account, or any `address` at `base_url`
        (default mainnet), or else the default account, or else the only account."""
        if address is not None:
            if account is not None:
                raise ToolError("INVALID_ARGUMENT", "pass account or address, not both")
            if not ADDRESS.fullmatch(address):
                raise ToolError("INVALID_ARGUMENT", "address must be 0x followed by 40 hex characters")
            base_url = TOOL_BASE_URLS[0] if base_url is None else base_url
            if base_url not in TOOL_BASE_URLS:
                raise ToolError("INVALID_ARGUMENT", f"base_url must be one of {', '.join(TOOL_BASE_URLS)}")
            return Target(None, address, base_url)
        if base_url is not None:
            raise ToolError("INVALID_ARGUMENT", "base_url is used only with address",
                            hint="a configured account's base_url is set in the config")
        if account is not None:
            return self._target(self._configured(account))
        if self._config.default_account is not None:
            return self._target(self._config.accounts[self._config.default_account])
        if len(self._config.accounts) == 1:
            return self._target(next(iter(self._config.accounts.values())))
        raise ToolError("AMBIGUOUS_ACCOUNT", "several accounts are configured and none is the default",
                        hint=f"pass account: {', '.join(sorted(self._config.accounts))}")

    def resolve_trade(self, account: Optional[str] = None) -> Target:
        """The target of a trade tool: the named account, or else the only account allowed
        to trade. Never the default account when several can trade."""
        if account is not None:
            config = self._configured(account)
            if config.name not in self._keys:
                raise ToolError("TRADING_DISABLED", f"account {config.name!r} is not enabled for trading")
            return self._target(config)
        if len(self._keys) == 1:
            return self._target(self._config.accounts[next(iter(self._keys))])
        if not self._keys:
            raise ToolError("TRADING_DISABLED", "no account is enabled for trading")
        raise ToolError("AMBIGUOUS_ACCOUNT", "several accounts can trade",
                        hint=f"pass account: {', '.join(sorted(self._keys))}")

    def trading_targets(self) -> list[Target]:
        """Every account that can trade, in config order."""
        return [self._target(config) for config in self._config.accounts.values() if config.name in self._keys]

    def _configured(self, name: str) -> AccountConfig:
        config = self._config.accounts.get(name)
        if config is None:
            raise ToolError("ACCOUNT_NOT_FOUND", f"no account named {name!r}",
                            hint=f"configured: {', '.join(sorted(self._config.accounts))}")
        return config

    @staticmethod
    def _target(config: AccountConfig) -> Target:
        return Target(config, config.address, config.base_url)

    # ── HIP-3 dexes ─────────────────────────────────────────────────

    def perp_dexs(self, base_url: str) -> tuple[str, ...]:
        """The HIP-3 dexes read at `base_url`: those of every account there (perp_dexs in the
        config), then those enabled for the session. Hyperliquid's own perps are always read."""
        with self._state_lock:
            session = self._session_dexes.get(base_url, ())
        return self._dexes_read(base_url, session)

    def account_perp_dexs(self, config: AccountConfig) -> tuple[str, ...]:
        """The HIP-3 dexes an account trades: its own (config), then those enabled for the
        session at its base_url."""
        with self._state_lock:
            session = self._session_dexes.get(config.base_url, ())
        return _dexes(config.perp_dexs, session)

    def _dexes_read(self, base_url: str, session: tuple[str, ...]) -> tuple[str, ...]:
        configured = [dex for config in self._config.accounts.values() if config.base_url == base_url
                      for dex in config.perp_dexs]
        return _dexes(configured, session)

    def enable_perp_dexs(self, base_url: str, dexes: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Read and trade `dexes` at `base_url` too, for every account there, until the server
        stops. Returns the dexes read there and those this call enabled.

        The read client is made again with them first, unless one made with the same dexes
        exists: a dex the network does not have (one of the config's too) is INVALID_ARGUMENT,
        naming the network's dexes, and nothing changes. The dexes, the read client and the
        trading clients are then changed at once: a trading client whose dexes change is
        made again at its next use (one in use finishes its call)."""
        with self._create_lock:  # one change at a time, and no client is made meanwhile
            with self._state_lock:
                before = self._session_dexes.get(base_url, ())
                reader = self._readers.get(base_url)
            added = tuple(dex for dex in dict.fromkeys(dexes) if dex not in before)
            if not added:
                return self._dexes_read(base_url, before), ()
            session = (*before, *added)
            wanted = self._dexes_read(base_url, session)
            if reader is None or wanted != self._dexes_read(base_url, before):
                reader = self._new_reader(base_url, wanted)
            accounts = [config for config in self._config.accounts.values() if config.base_url == base_url]
            with self._state_lock:
                self._session_dexes[base_url] = session
                self._readers[base_url] = reader
                for config in accounts:
                    if _dexes(config.perp_dexs, session) != _dexes(config.perp_dexs, before):
                        self._traders.pop(config.name, None)
        return wanted, added

    def _new_reader(self, base_url: str, dexes: tuple[str, ...], *, cache_market_specs: bool = True) -> Any:
        """A read client of `dexes`. For a dex the network does not have, the library names
        its own get_perp_dexs(); an agent gets the network's dexes instead, read with a client
        of Hyperliquid's own perps (the configured dexes may be the wrong ones)."""
        options = {} if cache_market_specs else {"cache_market_specs": False}
        try:
            return self._make_client(base_url, None, None, dexes, **options)
        except InvalidArgumentException:
            listed = [dex["name"] for dex in self._make_client(base_url, None, None, ()).get_perp_dexs()]
            missing = [dex for dex in dexes if dex not in listed]
            if not missing:
                raise
            raise ToolError("INVALID_ARGUMENT", f"not a perp dex at {base_url}: {', '.join(missing)}",
                            hint=f"its HIP-3 dexes: {', '.join(listed)}") from None

    # ── clients ─────────────────────────────────────────────────────

    def read_client(self, target: Target) -> Any:
        """The client for reading `target`: the one without a key for its base_url, shared by
        every wallet there. Pass `target.address` as `address=` on each call."""
        return self.market_client(target.base_url)

    def market_client(self, base_url: str) -> Any:
        """The client without a key for `base_url`, for market data (the read client)."""
        return self._kept(self._readers, base_url,
                          lambda: self._make_client(base_url, None, None, self.perp_dexs(base_url)))

    def planning_client(self, target: Target) -> Any:
        """An unsigned reader with the account's trading dex scope, for submission previews.

        Reuse the network reader when scopes match. Otherwise build a scoped reader using
        the library's metadata cache; no additional cached client to refresh or invalidate.
        Pass the target address explicitly on account reads.
        """
        if target.account is None:
            return self.read_client(target)
        dexes = self.account_perp_dexs(target.account)
        if dexes == self.perp_dexs(target.base_url):
            return self.read_client(target)
        return self._new_reader(target.base_url, dexes)

    def trade_client(self, target: Target) -> Any:
        """The client that signs for `target`, a trading account from `resolve_trade`."""
        config = target.account
        if config is None or config.name not in self._keys:
            raise ToolError("TRADING_DISABLED", f"{target.name or target.address} is not enabled for trading")

        return self._kept(self._traders, config.name, lambda: self._new_trader(config))

    def _new_trader(self, config: AccountConfig, *, cache_market_specs: bool = True) -> Any:
        account = HyperliquidAccount(private_key=self._keys[config.name], public_address=config.address,
                                     is_vault=config.vault)
        # Market orders and closes use the account's slippage cap as the default.
        options = {} if cache_market_specs else {"cache_market_specs": False}
        return self._make_client(config.base_url, account, float(config.max_slippage),
                                 self.account_perp_dexs(config), **options)

    def refresh_market_specs(self, base_url: str) -> dict[str, Any]:
        """Replace this network's reader and existing traders with freshly built clients.

        Build everything before publishing: a failure leaves existing references usable.
        In-flight calls can finish on old clients. Creation and dex changes share this lock.
        No account lock is acquired and no state lock is held during network reads, so the
        immediate halt stays independent.
        """
        with self._create_lock:
            dexes = self.perp_dexs(base_url)
            with self._state_lock:
                accounts = [config for config in self._config.accounts.values()
                            if config.base_url == base_url and config.name in self._traders]
            reader = self._new_reader(base_url, dexes, cache_market_specs=False)
            traders = {config.name: self._new_trader(config, cache_market_specs=False) for config in accounts}
            result = {"dexes_read": ["default", *dexes], "perp_markets": len(reader.market_specs),
                      "trading_clients": [{"account": config.name,
                                           "dexes_traded": ["default", *self.account_perp_dexs(config)],
                                           "perp_markets": len(traders[config.name].market_specs)}
                                          for config in accounts]}
            with self._state_lock:
                self._readers[base_url] = reader
                self._traders.update(traders)
        return result

    def _kept(self, clients: dict[str, Any], key: str, make: Callable[[], Any]) -> Any:
        """clients[key], made on first use; a client that fails to be made is not kept. Read
        under the state lock, so it matches the dexes enable_perp_dexs published with it."""
        with self._state_lock:
            client = clients.get(key)
        if client is not None:
            return client
        with self._create_lock:
            with self._state_lock:
                client = clients.get(key)
            if client is None:
                client = make()
                with self._state_lock:
                    clients[key] = client
        return client

    # ── signer check ────────────────────────────────────────────────

    def check_signer(self, target: Target, reduces_risk: bool = False) -> None:
        """Raise SIGNER_NOT_AUTHORIZED unless the key of `target`'s account is an API wallet
        that trades its address. Call it holding the account's lock, at the start of every
        trade call: it looks the key up each time, so an approval removed, or moved to another
        account, is found before the next call. A lookup that fails with a retryable error
        (Hyperliquid's info down, rate limited) raises it, except for a call that
        `reduces_risk` after a confirmed lookup: that one uses the confirmed result, so a
        close still works then. A refusal drops the confirmed result."""
        config = target.account
        if config is None or config.name not in self._keys:
            raise ToolError("TRADING_DISABLED", f"{target.name or target.address} is not enabled for trading")
        signer = signer_address(self._keys[config.name])
        try:
            problem = signer_problem(config, signer, lambda: self.read_client(target))
        except HyperliquidException as error:
            with self._state_lock:
                confirmed = config.name in self._signer_confirmed
            if reduces_risk and confirmed and error.retryable:
                return
            raise
        with self._state_lock:
            if problem is None:
                self._signer_confirmed.add(config.name)
            else:
                self._signer_confirmed.discard(config.name)
        if problem is not None:
            raise ToolError("SIGNER_NOT_AUTHORIZED", problem, hint=SIGNER_HINT)

    # ── client order ids ────────────────────────────────────────────

    def reserve_cloid(self, target: Target, cloid: Optional[str]) -> str:
        """Reserve a client order id for an order of `target`'s exchange account, for every
        server, for 30 days: `cloid` as the agent passed it, else a new one, made here as
        the library makes it so that it is known too. One used before is refused, so a
        repeated call cannot place a second order under the same id. Compared in lower
        case, as the exchange reads hex. Call it holding the account's lock, just before the
        order is sent; release_cloid() if the order provably was not sent."""
        reserved = self._shared().reserve_cloid(target.key, cloid)
        if reserved is None:
            raise ToolError("INVALID_ARGUMENT", f"cloid {cloid} was used before for this account",
                            hint="look its order up with get_order_status_by_cloid; a new order needs a new cloid")
        return reserved

    def reserve_cloids(self, target: Target, cloids: list[Optional[str]]) -> list[str]:
        """reserve_cloid for the orders of a batch, all or none, in one write: each given cloid,
        a new one for each None. One used before, or given twice in the batch, refuses them
        all (INVALID_ARGUMENT)."""
        reserved = self._shared().reserve_cloids(target.key, cloids)
        if reserved is None:
            raise ToolError("INVALID_ARGUMENT", "a cloid of the batch was used before for this account, or is given "
                                                "twice", hint="each order needs its own new cloid; look a used one up "
                                                              "with get_order_status_by_cloid")
        return reserved

    def release_cloids(self, target: Target, cloids: list[str]) -> None:
        """release_cloid for every order of a batch that provably was not sent, in one write."""
        self._shared().release_cloids(target.key, cloids)

    def remember_cloids(self, target: Target, cloids: list[str]) -> None:
        """Mark the cloids the library made (close, update_stop_loss, ...) as used, in one
        write: the agent may see them in a result."""
        self._shared().remember_cloids(target.key, cloids)

    def release_cloid(self, target: Target, cloid: str) -> None:
        """Free a reserved cloid whose order the library reports as not sent: an argument it
        refused, or a retryable error (rate limit, connect timeout: not processed)."""
        self._shared().release_cloid(target.key, cloid)

    # ── orders that add risk (max_orders_per_hour) ──────────────────

    def count_risk_order(self, target: Target, count: int = 1) -> float:
        """Count `count` orders that add risk (a batch's, in one write) for every server, just
        before they are sent; returns their time, for release_risk_order."""
        return self._shared().count_risk_order(target.key, count)

    def release_risk_order(self, target: Target, at: float, count: int = 1) -> None:
        """Uncount orders that provably were not sent."""
        self._shared().release_risk_order(target.key, at, count)

    def risk_orders_last_hour(self, target: Target) -> int:
        """The orders that added risk on `target`'s exchange account in the last hour, from
        every server of the config (orders from the app or bots are not counted)."""
        return self._shared().risk_orders_last_hour(target.key)

    def peek_risk_orders_last_hour(self, target: Target) -> int:
        """Preview the shared count without starting state, creating locks or counting orders."""
        return self._shared().peek_risk_orders_last_hour(target.key)

    def preview_halted(self, target: Target) -> bool:
        """Like is_halted, but unknown shared state must not look like a passed preview."""
        self._shared()
        return self.is_halted(target)

    # ── trade lock and halt ─────────────────────────────────────────

    def lock(self, target: Target) -> Any:
        """The lock every trade tool holds for its whole run on `target`'s exchange account,
        its reads included, shared by every server of the config. Check `is_halted` after
        acquiring it, so a call that waited for the lock cannot run after a halt."""
        return self._shared().account_lock(target.key)

    def halt(self, target: Target) -> None:
        """Halt `target`'s exchange account for every server of the config, until none of
        them runs. Takes effect at once, without waiting for a running trade tool. Halted
        here first, so that if the shared file cannot be written, this server stays halted
        and the error says that the other ones may not be."""
        with self._state_lock:
            self._local_halts.add(target.key)
        try:
            self._shared().halt(target.key)
        except OSError as error:
            raise ToolError("INTERNAL", f"halted in this server only: the shared halt cannot be written "
                                        f"({error.strerror or error})",
                            hint="other chats on the account are not halted: quit them, or the assistant") from error

    def is_halted(self, target: Target) -> bool:
        with self._state_lock:
            if target.key in self._local_halts:
                return True
        return self._state is not None and self._state.is_halted(target.key)

    def _shared(self) -> SharedState:
        if self._state is None:
            raise ToolError("TRADING_DISABLED", "trading is switched off for this server")
        return self._state
