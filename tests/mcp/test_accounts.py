"""Account resolution, clients, trade lock and halt of the MCP server."""

import threading
from types import SimpleNamespace

import pytest
from hyperliquid.utils import constants

from fractrade_hl_simple.exceptions import InvalidArgumentException, NetworkException
from fractrade_hl_simple.mcp_server.accounts import AccountRegistry, signer_address
from fractrade_hl_simple.mcp_server.config import parse_config
from fractrade_hl_simple.mcp_server.errors import ToolError

MAIN = "0x" + "1" * 40
COPY = "0x" + "2" * 40
WHALE = "0x" + "3" * 40
KEYS = {"main": "0x" + "ab" * 32, "copy": "0x" + "cd" * 32}
MAINNET, TESTNET = constants.MAINNET_API_URL, constants.TESTNET_API_URL


def config(default: str = "", main_base_url: str = MAINNET, main_address: str = MAIN, copy_trading: bool = True):
    return parse_config(f'''
{f'default_account = "{default}"' if default else ""}
[accounts.main]
address = "{main_address}"
base_url = "{main_base_url}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.copy]
address = "{COPY}"
key_env = "HL_KEY_COPY"
trading = {str(copy_trading).lower()}
vault = true
max_order_usd = 100
max_leverage = 5
max_slippage = 0.01

[accounts.whale]
address = "{WHALE}"
''', "test.toml")


class FakeClients:
    """Client factory that records what it was asked to make."""

    def __init__(self):
        self.made = []
        self.slippage = []  # the default slippage of each client made, in the same order
        self.dexes = []     # the HIP-3 dexes of each client made, in the same order

    def __call__(self, base_url, account, default_slippage, perp_dexs):
        self.made.append((base_url, account))
        self.slippage.append(default_slippage)
        self.dexes.append(perp_dexs)
        return object()


def registry(conf=None, keys=KEYS, make_client=None):
    return AccountRegistry(conf or config(), keys, make_client or FakeClients(), shared_state())


def error_code(call):
    with pytest.raises(ToolError) as e:
        call()
    return e.value.code


class TestReadResolution:

    def test_without_a_default_the_only_account(self):
        only = parse_config(f'[accounts.whale]\naddress = "{WHALE}"\n', "test.toml")
        assert registry(only).resolve_read().name == "whale"

    def test_several_accounts_without_a_default_are_ambiguous(self):
        assert error_code(registry().resolve_read) == "AMBIGUOUS_ACCOUNT"

    def test_any_address_on_mainnet_by_default(self):
        other = "0x" + "4" * 40
        target = registry().resolve_read(address=other)
        assert (target.account, target.address, target.base_url) == (None, other, MAINNET)
        assert registry().resolve_read(address=other, base_url=TESTNET).base_url == TESTNET

    @pytest.mark.parametrize("arguments", [
        {"account": "main", "address": WHALE},            # both
        {"address": "0x1234"},                             # not an address
        {"address": WHALE, "base_url": MAINNET + "/"},     # not one of the SDK's URLs
        {"account": "main", "base_url": TESTNET},          # an account's URL comes from the config
    ])
    def test_invalid_arguments(self, arguments):
        assert error_code(lambda: registry().resolve_read(**arguments)) == "INVALID_ARGUMENT"


class TestTradeResolution:
    def test_the_only_trading_account_not_the_default(self):
        reg = registry(config(default="whale", copy_trading=False))
        assert reg.resolve_trade().name == "main"

    def test_several_trading_accounts_are_ambiguous_even_with_a_default(self):
        assert error_code(registry(config(default="main")).resolve_trade) == "AMBIGUOUS_ACCOUNT"
        assert registry().resolve_trade(account="copy").name == "copy"

    def test_no_account_trades_while_trading_is_switched_off(self):
        reg = registry(keys={})  # the server loads no keys without the trading switch
        assert error_code(reg.resolve_trade) == "TRADING_DISABLED"
        assert error_code(lambda: reg.resolve_trade(account="main")) == "TRADING_DISABLED"


class TestClients:
    def test_one_client_without_a_key_reads_every_wallet_at_one_url(self):
        clients = FakeClients()
        reg = registry(make_client=clients)
        assert clients.made == []
        readers = {id(reg.read_client(reg.resolve_read(account=name))) for name in ("main", "copy", "whale")}
        readers.add(id(reg.read_client(reg.resolve_read(address="0x" + "4" * 40))))
        assert len(readers) == 1 and clients.made == [(MAINNET, None)]
        reg.read_client(reg.resolve_read(address=WHALE, base_url=TESTNET))
        assert clients.made == [(MAINNET, None), (TESTNET, None)]

    def test_a_trading_account_signs_with_its_own_key(self):
        clients = FakeClients()
        reg = registry(make_client=clients)
        copy = reg.resolve_trade(account="copy")
        assert reg.trade_client(copy) is reg.trade_client(copy)
        (base_url, account), = clients.made
        assert (base_url, account.private_key, account.public_address, account.is_vault) == (
            MAINNET, KEYS["copy"], COPY, True)
        # A market order without slippage, and close_all_positions, use the account's cap.
        assert clients.slippage == [0.01]

    def test_a_trading_client_without_max_slippage_uses_the_default_cap(self):
        clients = FakeClients()
        reg = registry(make_client=clients)
        reg.trade_client(reg.resolve_trade(account="main"))
        reg.read_client(reg.resolve_read(account="main"))
        assert clients.slippage == [0.02, None]  # the reader keeps the library's default

    def test_each_trading_account_has_its_own_signing_client(self):
        clients = FakeClients()
        reg = registry(make_client=clients)
        main, copy = reg.trade_client(reg.resolve_trade(account="main")), reg.trade_client(reg.resolve_trade(account="copy"))
        reader = reg.read_client(reg.resolve_read(account="main"))
        assert len({id(main), id(copy), id(reader)}) == 3
        signers = {(account.public_address, account.private_key) for _, account in clients.made if account}
        assert signers == {(MAIN, KEYS["main"]), (COPY, KEYS["copy"])}

    def test_only_trading_accounts_get_a_client_that_signs(self):
        reg = registry(config(copy_trading=False))
        for target in (reg.resolve_read(account="whale"), reg.resolve_read(account="copy"),
                       reg.resolve_read(address=MAIN)):  # a trading account's address, not named
            assert error_code(lambda target=target: reg.trade_client(target)) == "TRADING_DISABLED"

    def test_made_once_when_first_used_by_two_tools_at_once(self):
        started, release, made = threading.Event(), threading.Event(), []

        def slow(*args):
            made.append(args)
            started.set()
            release.wait(5)
            return object()

        reg = registry(make_client=slow)
        target = reg.resolve_read(account="whale")
        results = []
        first = threading.Thread(target=lambda: results.append(reg.read_client(target)))
        first.start()
        started.wait(5)
        second = threading.Thread(target=lambda: results.append(reg.read_client(target)))
        second.start()
        second.join(0.1)  # waits for the first instead of making its own
        release.set()
        first.join()
        second.join()
        assert len(made) == 1 and results[0] is results[1]

    def test_a_failed_client_is_not_kept(self):
        calls = []

        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise ConnectionError("metadata unavailable")
            return object()

        reg = registry(make_client=flaky)
        target = reg.resolve_read(account="whale")
        with pytest.raises(ConnectionError):
            reg.read_client(target)
        assert reg.read_client(target) is not None
        assert len(calls) == 2


def held_elsewhere(reg, target):
    """Whether `target`'s lock is held: another thread tries it, without waiting long."""
    got = threading.Event()

    def take():
        with reg.lock(target):
            got.set()

    taker = threading.Thread(target=take, daemon=True)
    taker.start()
    held = not got.wait(0.3)
    return held, taker


class TestLockAndHalt:
    def test_names_for_one_address_share_lock_and_halt(self):
        reg = registry(config(main_address=COPY))
        main, copy = reg.resolve_trade(account="main"), reg.resolve_trade(account="copy")
        with reg.lock(main):
            held, taker = held_elsewhere(reg, copy)
            assert held
        taker.join(5)
        reg.halt(main)
        assert reg.is_halted(copy)
        assert not reg.is_halted(reg.resolve_read(account="whale"))

    def test_same_address_at_another_url_is_another_account(self):
        reg = registry(config(main_address=COPY, main_base_url=TESTNET))
        main, copy = reg.resolve_trade(account="main"), reg.resolve_trade(account="copy")
        with reg.lock(main):
            held, taker = held_elsewhere(reg, copy)
            assert not held
        reg.halt(main)
        assert not reg.is_halted(copy)

    def test_halt_does_not_wait_for_a_running_tool_or_a_client_being_made(self):
        started, release, running = threading.Event(), threading.Event(), threading.Event()

        def slow(*args):
            started.set()
            release.wait(5)
            return object()

        reg = registry(make_client=slow)
        main = reg.resolve_trade(account="main")

        def tool():  # a trade tool is running
            with reg.lock(main):
                running.set()
                release.wait(5)

        runner = threading.Thread(target=tool)
        runner.start()
        assert running.wait(5)
        maker = threading.Thread(target=reg.read_client, args=(reg.resolve_read(account="whale"),))
        maker.start()
        assert started.wait(5)    # and a client is being made
        halter = threading.Thread(target=reg.halt, args=(main,))
        halter.start()
        halter.join(1)
        try:
            assert not halter.is_alive() and reg.is_halted(main)
        finally:
            release.set()
            runner.join()
            maker.join()


# ── signer check ──────────────────────────────────────────

SIGNER = signer_address(KEYS["main"])  # the address of main's key, an API wallet of MAIN


class RoleReader:
    """The read client of the signer check: answers userRole as Hyperliquid documents it."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked = []

    def get_user_role(self, address):
        self.asked.append(address)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def checked(conf, reader, keys=KEYS):
    accounts = registry(conf, keys, lambda base_url, account, default_slippage, perp_dexs: reader)
    return accounts, accounts.resolve_trade("main")


def dex_config():
    return parse_config(dex_config_text(), "test.toml")


def dex_config_text_without_para():
    return parse_config(dex_config_text().replace('perp_dexs = ["para"]', 'perp_dexs = []'), "test.toml")


def dex_config_text():
    return f'''
[defaults]
perp_dexs = ["xyz"]

[accounts.main]
address = "{MAIN}"
key_env = "HL_KEY_MAIN"
trading = true
max_order_usd = 100
max_leverage = 5

[accounts.copy]
address = "{COPY}"
key_env = "HL_KEY_COPY"
trading = true
vault = true
max_order_usd = 100
max_leverage = 5
perp_dexs = ["para"]

[accounts.whale]
address = "{WHALE}"
base_url = "{TESTNET}"
perp_dexs = []
'''


class Network:
    """A client factory for a network whose HIP-3 dexes are `listed`; a client of a dex it
    does not have fails as the library's does. `gate` (an Event) holds the making of a client
    with `slow` among its dexes until it is set."""

    def __init__(self, listed=("xyz", "para", "io"), slow=None):
        self.listed, self.slow, self.made = listed, slow, []
        self.cache_options = []
        self.started, self.gate = threading.Event(), threading.Event()

    def __call__(self, base_url, account, default_slippage, perp_dexs, *, cache_market_specs=True):
        if self.slow in perp_dexs:
            self.started.set()
            self.gate.wait(5)
        unknown = [dex for dex in perp_dexs if dex not in self.listed]
        if unknown:
            raise InvalidArgumentException(f"{unknown[0]!r} is not a perp dex on mainnet; get_perp_dexs() lists them")
        self.made.append((account is not None, perp_dexs))
        self.cache_options.append(cache_market_specs)
        client = SimpleNamespace(perp_dexs=perp_dexs,
                                 market_specs={"BTC": {}, **{f"{dex}:MARKET": {} for dex in perp_dexs}})
        client.get_perp_dexs = lambda: [{"name": dex, "full_name": dex.upper()} for dex in self.listed]
        return client


class TestPerpDexs:
    def test_the_read_client_of_a_url_covers_the_dexes_of_every_account_there(self):
        clients = FakeClients()
        reg = registry(dex_config(), make_client=clients)
        reg.read_client(reg.resolve_read(account="main"))
        reg.read_client(reg.resolve_read(address=WHALE, base_url=TESTNET))
        reg.trade_client(reg.resolve_trade(account="copy"))
        assert clients.dexes == [("xyz", "para"), (), ("para",)]  # each trading account its own

    def test_enabled_dexes_are_read_and_traded_by_every_account_at_that_url(self):
        network = Network()
        reg = registry(dex_config(), make_client=network)
        main = reg.resolve_trade(account="main")
        old_reader, old_trader = reg.read_client(main), reg.trade_client(main)
        assert reg.enable_perp_dexs(MAINNET, ["io", "xyz"]) == (("xyz", "para", "io"), ("io", "xyz"))
        assert reg.read_client(main) is not old_reader and reg.read_client(main).perp_dexs == ("xyz", "para", "io")
        new_trader = reg.trade_client(main)
        assert new_trader is not old_trader and reg.trade_client(main) is new_trader
        assert new_trader.perp_dexs == ("xyz", "io")
        assert reg.account_perp_dexs(reg._config.accounts["copy"]) == ("para", "io", "xyz")
        assert reg.perp_dexs(TESTNET) == ()  # another network is not touched

    def test_an_unknown_dex_of_another_accounts_config_is_found_too(self):
        # copy's config names a dex the network lacks. Enabling it, before any read client
        # exists, must not hand it to main without a check.
        conf = parse_config(dex_config_text().replace('perp_dexs = ["para"]', 'perp_dexs = ["nope"]'), "test.toml")
        reg = registry(conf, make_client=Network())
        with pytest.raises(ToolError, match="not a perp dex at .*: nope"):
            reg.enable_perp_dexs(MAINNET, ["nope"])
        assert reg.account_perp_dexs(conf.accounts["main"]) == ("xyz",)

    def test_enabling_a_dex_again_makes_no_client(self):
        network = Network()
        reg = registry(dex_config(), make_client=network)
        reg.enable_perp_dexs(MAINNET, ["io"])
        made = len(network.made)
        assert reg.enable_perp_dexs(MAINNET, ["io"]) == (("xyz", "para", "io"), ())
        assert len(network.made) == made

    def test_a_dex_another_account_reads_needs_no_new_read_client_once_one_exists(self):
        # para is read at mainnet (copy's config); enabling it changes only main's trading client.
        network = Network()
        reg = registry(dex_config(), make_client=network)
        main = reg.resolve_trade(account="main")
        reader, trader = reg.read_client(main), reg.trade_client(main)
        reg.enable_perp_dexs(MAINNET, ["para"])
        assert reg.read_client(main) is reader and reg.trade_client(main) is not trader
        assert reg.trade_client(main).perp_dexs == ("xyz", "para")

    def test_nothing_is_published_before_the_new_read_client_exists(self):
        network = Network(slow="io")
        reg = registry(dex_config(), make_client=network)
        main = reg.resolve_trade(account="main")
        reader = reg.read_client(main)
        enabling = threading.Thread(target=reg.enable_perp_dexs, args=(MAINNET, ["io"]))
        enabling.start()
        assert network.started.wait(5)
        # While the client with io is being made, reads see the old dexes with the old client.
        assert reg.perp_dexs(MAINNET) == ("xyz", "para") and reg.read_client(main) is reader
        network.gate.set()
        enabling.join(5)
        assert reg.perp_dexs(MAINNET) == ("xyz", "para", "io") and reg.read_client(main).perp_dexs == reg.perp_dexs(MAINNET)

    def test_each_call_reports_the_dexes_it_enabled_itself(self):
        network = Network(slow="para")
        reg = registry(dex_config_text_without_para(), make_client=network)
        results = {}
        first = threading.Thread(target=lambda: results.setdefault("para", reg.enable_perp_dexs(MAINNET, ["para"])))
        first.start()
        assert network.started.wait(5)
        second = threading.Thread(target=lambda: results.setdefault("io", reg.enable_perp_dexs(MAINNET, ["io"])))
        second.start()
        network.gate.set()
        first.join(5), second.join(5)
        assert results["para"][1] == ("para",) and results["io"][1] == ("io",)
        assert reg.perp_dexs(MAINNET) == ("xyz", "para", "io")


class TestRefreshMarketSpecs:
    def test_all_existing_traders_keep_their_own_dex_scope_and_other_networks_stay_untouched(self):
        network = Network()
        reg = registry(dex_config(), make_client=network)
        main, copy = reg.resolve_trade("main"), reg.resolve_trade("copy")
        reg.enable_perp_dexs(MAINNET, ["io"])
        old_main, old_copy, other = reg.trade_client(main), reg.trade_client(copy), reg.market_client(TESTNET)
        result = reg.refresh_market_specs(MAINNET)
        assert result == {"dexes_read": ["default", "xyz", "para", "io"], "perp_markets": 4,
                          "trading_clients": [
                              {"account": "main", "dexes_traded": ["default", "xyz", "io"], "perp_markets": 3},
                              {"account": "copy", "dexes_traded": ["default", "para", "io"], "perp_markets": 3}]}
        assert reg.trade_client(main) is not old_main and reg.trade_client(copy) is not old_copy
        assert reg.market_client(TESTNET) is other
        assert reg.perp_dexs(MAINNET) == ("xyz", "para", "io")
        assert network.cache_options[-3:] == [False, False, False]

    def test_refresh_publishes_together_without_blocking_the_immediate_halt(self):
        network = Network()
        started, release = threading.Event(), threading.Event()

        def factory(base_url, account, slippage, dexes, *, cache_market_specs=True):
            client = network(base_url, account, slippage, dexes, cache_market_specs=cache_market_specs)
            if not cache_market_specs and account is not None:
                started.set()
                assert release.wait(5)
            return client

        reg = registry(make_client=factory)
        main = reg.resolve_trade("main")
        reader, trader = reg.read_client(main), reg.trade_client(main)
        results = []
        refresh = threading.Thread(target=lambda: results.append(reg.refresh_market_specs(MAINNET)), daemon=True)
        refresh.start()
        try:
            assert started.wait(5)
            assert reg.read_client(main) is reader and reg.trade_client(main) is trader
            halt = threading.Thread(target=reg.halt, args=(main,), daemon=True)
            halt.start()
            halt.join(1)
            assert not halt.is_alive() and reg.is_halted(main)
        finally:
            release.set()
            refresh.join(5)
        assert not refresh.is_alive() and len(results) == 1
        assert reg.read_client(main) is not reader and reg.trade_client(main) is not trader
        assert reg.is_halted(main)


class TestSignerCheck:
    def test_an_api_wallet_of_the_account_is_looked_up_before_each_action(self):
        agent = {"role": "agent", "data": {"user": "0x" + "AB" * 20}}  # the address compared in any case
        reader = RoleReader(agent, agent)
        accounts, main = checked(config(main_address="0x" + "ab" * 20), reader)
        accounts.check_signer(main)
        accounts.check_signer(main, reduces_risk=True)
        assert reader.asked == [SIGNER, SIGNER]

    @pytest.mark.parametrize("answer, says", [
        ({"role": "agent", "data": {"user": COPY}}, "an API wallet of another account"),
        ({"role": "missing"}, "expired or was removed"),
        ({"role": "user"}, "a wallet of its own"),
        ({"role": "subAccount", "data": {"master": MAIN}}, "its role as 'subAccount'"),
        ({}, "answered {}"),
    ])
    def test_a_key_that_does_not_trade_the_account_is_refused_each_time(self, answer, says):
        # Orders signed with it would go to another account than the one the server reads.
        reader = RoleReader(answer, answer)
        accounts, main = checked(config(), reader)
        for _ in range(2):  # a refusal is not kept: an API wallet approved meanwhile works
            with pytest.raises(ToolError) as e:
                accounts.check_signer(main)
            assert e.value.code == "SIGNER_NOT_AUTHORIZED" and says in str(e.value)
        assert reader.asked == [SIGNER, SIGNER]

    def test_the_accounts_own_key_is_refused_without_a_lookup(self):
        # It can withdraw and transfer: the server signs only with API wallets.
        clients = FakeClients()  # a client loads market data when made: none is made
        accounts = registry(config(main_address=SIGNER), KEYS, clients)
        with pytest.raises(ToolError) as e:
            accounts.check_signer(accounts.resolve_trade("main"))
        assert e.value.code == "SIGNER_NOT_AUTHORIZED" and "the account's own key" in str(e.value)
        assert "API wallet" in e.value.hint and clients.made == []

    def test_a_vault_is_traded_with_an_api_wallet_of_any_account(self):
        # Each action names the vault, so the exchange executes it for the vault or rejects it;
        # the key must still be one that cannot withdraw.
        reader = RoleReader({"role": "agent", "data": {"user": MAIN}})
        accounts = registry(config(), KEYS, lambda base_url, account, default_slippage, perp_dexs: reader)
        accounts.check_signer(accounts.resolve_trade("copy"))
        assert reader.asked == [signer_address(KEYS["copy"])]

    @pytest.mark.parametrize("answer", [{"role": "user"}, {"role": "missing"}])
    def test_a_vault_with_a_key_that_is_no_api_wallet_is_refused(self, answer):
        reader = RoleReader(answer)
        accounts = registry(config(), KEYS, lambda base_url, account, default_slippage, perp_dexs: reader)
        with pytest.raises(ToolError) as e:
            accounts.check_signer(accounts.resolve_trade("copy"))
        assert e.value.code == "SIGNER_NOT_AUTHORIZED"

    def test_an_approval_removed_meanwhile_is_found_before_a_close_too(self):
        # The key may now trade another account: a close signed with it would act there.
        reader = RoleReader({"role": "agent", "data": {"user": MAIN}}, {"role": "agent", "data": {"user": COPY}})
        accounts, main = checked(config(), reader)
        accounts.check_signer(main)
        with pytest.raises(ToolError) as e:
            accounts.check_signer(main, reduces_risk=True)
        assert e.value.code == "SIGNER_NOT_AUTHORIZED" and "an API wallet of another account" in str(e.value)

    @pytest.mark.parametrize("account", ["main", "copy"])  # copy is a vault
    @pytest.mark.parametrize("answer", [
        {"role": "agent", "data": {"user": ""}}, {"role": "agent", "data": {"user": "not-an-address"}},
        {"role": "agent", "data": {"user": 5}}, {"role": "agent", "data": "x"}, {"role": "agent"},
    ])
    def test_an_answer_in_no_documented_shape_is_refused(self, account, answer):
        reader = RoleReader(answer)
        accounts = registry(config(), KEYS, lambda base_url, acct, default_slippage, perp_dexs: reader)
        with pytest.raises(ToolError) as e:
            accounts.check_signer(accounts.resolve_trade(account))
        assert e.value.code == "SIGNER_NOT_AUTHORIZED"

    def test_the_own_key_of_a_vault_config_is_refused_too(self):
        clients = FakeClients()
        own = parse_config(f'''
[accounts.copy]
address = "{signer_address(KEYS["copy"])}"
key_env = "HL_KEY_COPY"
trading = true
vault = true
max_order_usd = 100
max_leverage = 5
''', "test.toml")
        accounts = registry(own, {"copy": KEYS["copy"]}, clients)
        with pytest.raises(ToolError) as e:
            accounts.check_signer(accounts.resolve_trade("copy"))
        assert "the account's own key" in str(e.value) and clients.made == []

    def test_a_failed_lookup_lets_only_a_call_that_reduces_risk_use_the_confirmed_result(self):
        # Hyperliquid's info down: a close still works, an order that adds risk does not.
        down = NetworkException("connection reset")
        reader = RoleReader(down, {"role": "agent", "data": {"user": MAIN}}, down, down)
        accounts, main = checked(config(), reader)
        with pytest.raises(NetworkException):            # nothing confirmed yet
            accounts.check_signer(main, reduces_risk=True)
        accounts.check_signer(main)
        accounts.check_signer(main, reduces_risk=True)   # the confirmed result
        with pytest.raises(NetworkException):
            accounts.check_signer(main)
        assert reader.asked == [SIGNER] * 4

    def test_an_invalid_key_is_refused_without_showing_it(self):
        bad = "0x" + "ff" * 32  # above the curve order: no private key
        accounts, main = checked(config(), RoleReader(), keys={**KEYS, "main": bad})
        with pytest.raises(ToolError) as e:
            accounts.check_signer(main)
        assert e.value.code == "SIGNER_NOT_AUTHORIZED" and "ff" * 32 not in str(e.value)
        # eth_account's error holds the key as a number: it must not be reachable at all.
        assert e.value.__cause__ is None and e.value.__context__ is None



def shared_state():
    """A started shared state in a folder of its own, removed with it (the trade tools need one)."""
    import tempfile
    import weakref
    from pathlib import Path
    from fractrade_hl_simple.mcp_server.state import SharedState
    folder = tempfile.mkdtemp()
    state = SharedState(Path(folder) / "state")
    state.start()
    import shutil
    weakref.finalize(state, shutil.rmtree, folder, True)
    return state


class TestPlanningClients:
    def test_the_preview_uses_only_the_selected_accounts_traded_dexes_without_keys(self):
        conf = parse_config(f'''
[accounts.main]
address = "{MAIN}"
trading = true
key_env = "MAIN_KEY"
max_order_usd = 100
max_leverage = 5
[accounts.whale]
address = "{WHALE}"
perp_dexs = ["xyz"]
''', "test.toml")
        clients = FakeClients()
        accounts = AccountRegistry(conf, {}, clients)
        target = accounts.resolve_read("main")
        reader = accounts.read_client(target)
        planner = accounts.planning_client(target)
        assert planner is not reader
        assert clients.dexes == [("xyz",), ()]
        assert all(account is None for _, account in clients.made)
        # Session-enabled dexes are also traded. Once scopes match, the reader is reused.
        accounts.enable_perp_dexs(MAINNET, ["xyz"])
        assert accounts.planning_client(target) is reader
        assert accounts.account_perp_dexs(target.account) == ("xyz",)
        assert accounts._traders == {}
