"""Tests for new features: leverage, order tracking, bulk orders, slippage, funding history, portfolio."""
import copy
import pickle
from types import SimpleNamespace

import inspect

import pytest
import requests
from unittest.mock import patch, MagicMock, PropertyMock
from decimal import Decimal
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid
from fractrade_hl_simple import api, exceptions
from fractrade_hl_simple.exceptions import (
    ActionOutcomeUnknownException, AddressRequiredException, ConfigurationException, HyperliquidException,
    InvalidArgumentException, NetworkException,
    OrderOutcomeUnknownException, OrderRejectedException, ProtectionOrderException,
    ProtectionReplacementIncompleteException, UnknownSymbolException, UnsupportedAccountModeException,
)
from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.models import Fill, HyperliquidAccount, Order, OrderType, coin_market

# The SDK's Info, answering metaAndAssetCtxs as Hyperliquid does: a client is not created
# without its markets.
INFO_ANSWERS = {"return_value.post.return_value": [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]},
                                                   [{}]]}



@pytest.fixture
def mock_client():
    """Create a fully mocked authenticated client for unit tests."""
    HyperliquidClient._cached_market_specs = None
    HyperliquidClient._cached_market_specs_at = 0
    with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS) as mock_info_cls, \
         patch('fractrade_hl_simple.hyperliquid.Exchange') as mock_exchange_cls:
        mock_info = mock_info_cls.return_value
        mock_exchange = mock_exchange_cls.return_value

        # Mock info.post for market specs initialization (_fetch_market_specs uses info.post)
        mock_info.post.return_value = [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "onlyIsolated": False},
                {"name": "ETH", "szDecimals": 4, "maxLeverage": 50, "onlyIsolated": False},
            ]},
            [
                {"funding": "0.0001", "openInterest": "1000", "markPx": "85000"},
                {"funding": "0.00005", "openInterest": "5000", "markPx": "3200"},
            ],
        ]

        client = HyperliquidClient(max_retries=0, retry_delay=0)

        # Mock the exchange and info objects on the client
        client.exchange = mock_exchange
        client.info = mock_info
        mock_info.name_to_coin = {name: name for name in client.market_specs}  # the SDK's symbol table

        # Set up auth
        client.account = MagicMock()
        client.account.public_address = "0x1234567890abcdef1234567890abcdef12345678"
        client.account.private_key = "0xdead"
        client.public_address = client.account.public_address
        client.exchange_account = MagicMock()

        yield client


# ── Configurable Slippage ──────────────────────────────────────────────

class TestConfigurableSlippage:
    def test_default_slippage_init(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient(default_slippage=0.02)
        assert client.default_slippage == 0.02

    def test_invalid_slippage_init(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            with pytest.raises(ValueError, match="default_slippage"):
                HyperliquidClient(default_slippage=0.0)
            with pytest.raises(ValueError, match="default_slippage"):
                HyperliquidClient(default_slippage=0.6)

    def test_a_retry_delay_of_any_number_type_is_used(self):
        # A Decimal passed the check and then made time.sleep raise TypeError at the first retry.
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient(max_retries=1, retry_delay=Decimal("0.001"))
        fn = MagicMock(side_effect=[requests.ConnectionError("down"), "ok"])
        assert client._with_retry(fn) == "ok"

    @pytest.mark.parametrize("setting", [{"max_retries": -1}, {"max_retries": 1.5}, {"max_retries": True},
                                         {"retry_delay": -1}, {"retry_delay": float("nan")}])
    def test_retry_settings_out_of_range_are_refused(self, setting):
        # max_retries=-1 sent nothing, then reported the order's outcome as unknown.
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            with pytest.raises(InvalidArgumentException, match=next(iter(setting))):
                HyperliquidClient(**setting)

    def test_slippage_applied_to_market_buy(self, mock_client):
        mock_client.default_slippage = 0.02
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"totalSz": "0.001", "avgPx": "86700", "oid": 1}}]}},
        }

        mock_client.buy("BTC", 0.001)

        # Verify the limit_px passed to exchange.order uses 2% slippage
        call_kwargs = mock_client.exchange.order.call_args
        limit_px = call_kwargs.kwargs.get("limit_px") or call_kwargs[1].get("limit_px")
        if limit_px is None:
            limit_px = call_kwargs[0][3]  # positional arg
        # 85000 * 1.02 = 86700
        assert 86600 < limit_px < 86800

    def test_slippage_override_per_call(self, mock_client):
        mock_client.default_slippage = 0.05
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"filled": {"totalSz": "0.001", "avgPx": "85850", "oid": 1}}]}},
        }

        mock_client.buy("BTC", 0.001, slippage=0.01)

        call_kwargs = mock_client.exchange.order.call_args
        limit_px = call_kwargs.kwargs.get("limit_px") or call_kwargs[1].get("limit_px")
        if limit_px is None:
            limit_px = call_kwargs[0][3]
        # 85000 * 1.01 = 85850
        assert 85800 < limit_px < 85900

    def test_invalid_slippage_per_call(self, mock_client):
        mock_client.info.all_mids.return_value = {"BTC": "85000.0"}
        with pytest.raises(ValueError, match="slippage"):
            mock_client.buy("BTC", 0.001, slippage=0.0)
        with pytest.raises(ValueError, match="slippage"):
            mock_client.buy("BTC", 0.001, slippage=0.6)


# ── Client configuration ──────────────────────────

ACCOUNT = HyperliquidAccount(private_key="0x" + "11" * 32,
                             public_address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A")


class TestClientConfiguration:
    def test_hyperliquid_env_selects_the_network(self, monkeypatch):
        # Documented in README and .env.example but never read before; base_url still wins.
        monkeypatch.setenv("HYPERLIQUID_ENV", "testnet")
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            assert HyperliquidClient().base_url == constants.TESTNET_API_URL
            assert HyperliquidClient(base_url=constants.MAINNET_API_URL).base_url == constants.MAINNET_API_URL

    @pytest.mark.parametrize("url, network", [
        (constants.MAINNET_API_URL, "mainnet"),
        (constants.TESTNET_API_URL, "testnet"),
        (constants.LOCAL_API_URL, None),
    ])
    def test_base_url_is_one_of_the_sdk_constants(self, url, network):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS) as info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange') as exchange_cls:
            client = HyperliquidClient(account=ACCOUNT, base_url=url)
        assert (client.base_url, client.env) == (url, network)
        assert info_cls.call_args.args[0] == url
        assert exchange_cls.call_args.args[1] == url  # the URL the SDK decides the signing network by

    @pytest.mark.parametrize("url", [constants.MAINNET_API_URL + "/", "https://api.hyperliquid.xyz/info", "mainnet"])
    def test_any_other_url_raises_instead_of_signing_for_testnet(self, url):
        # The SDK signs for mainnet only when base_url is exactly MAINNET_API_URL.
        with pytest.raises(ValueError, match="MAINNET_API_URL"):
            HyperliquidClient(base_url=url)

    def test_env_still_works_but_is_deprecated(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            with pytest.warns(DeprecationWarning, match="base_url"):
                client = HyperliquidClient(env="testnet")
            with pytest.warns(DeprecationWarning):
                positional = HyperliquidClient(ACCOUNT, "testnet")
        assert (client.base_url, client.env) == (constants.TESTNET_API_URL, "testnet")
        assert positional.base_url == constants.TESTNET_API_URL
        with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match="not both"):
            HyperliquidClient(env="mainnet", base_url=constants.MAINNET_API_URL)

    @pytest.mark.parametrize("arguments, loads", [
        ({"account": ACCOUNT, "base_url": constants.MAINNET_API_URL}, False),
        ({"account": ACCOUNT}, True),                          # network from HYPERLIQUID_ENV
        ({"base_url": constants.MAINNET_API_URL}, True),       # credentials from the environment
    ])
    def test_dotenv_is_loaded_only_when_settings_come_from_the_environment(self, arguments, loads):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'), \
             patch('fractrade_hl_simple.hyperliquid.load_env_file') as load:
            HyperliquidClient(**arguments)
        assert load.called is loads

    def test_requests_have_a_timeout(self):
        # Without one, a stalled connection blocks the caller forever.
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS) as info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange') as exchange_cls:
            HyperliquidClient(account=ACCOUNT, request_timeout=5)
        assert info_cls.call_args.kwargs["timeout"] == 5
        assert exchange_cls.call_args.kwargs["timeout"] == 5


# ── Post-only orders ───────────────────────────────────────

class TestPostOnly:
    def test_post_only_limit_order_is_sent_as_alo(self, mock_client):
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        order = mock_client.buy("BTC", 0.001, limit_price=80000.0, post_only=True)
        assert mock_client.exchange.order.call_args.kwargs["order_type"] == {"limit": {"tif": "Alo"}}
        assert order.time_in_force == "Alo"


# ── Order sizes ──────────────────────────────────────────────

class TestOrderSizes:
    def test_size_is_rounded_down_never_up(self, mock_client):
        # ETH has 4 size decimals; float round() sent 0.1235, more than requested.
        mock_client.exchange.order.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}},
        }
        mock_client.buy("ETH", 0.12346, limit_price=3000.0)
        assert mock_client.exchange.order.call_args.kwargs["sz"] == 0.1234

    def test_unknown_symbol_is_rejected_before_sending(self, mock_client):
        # Unknown symbols used to be sent with 0 size decimals (1.7 became 2.0).
        with pytest.raises(ValueError, match="Unknown symbol"):
            mock_client.buy("NEWCOIN", 1.7, limit_price=1.0)
        mock_client.exchange.order.assert_not_called()


# ── Leverage Management ────────────────────────────────────────────────

class TestLeverageManagement:
    def test_set_leverage_cross(self, mock_client):
        mock_client.exchange.update_leverage.return_value = {"status": "ok"}
        result = mock_client.set_leverage("BTC", 10)
        mock_client.exchange.update_leverage.assert_called_once_with(10, "BTC", True)
        assert result == {"status": "ok"}

    def test_set_leverage_isolated(self, mock_client):
        mock_client.exchange.update_leverage.return_value = {"status": "ok"}
        result = mock_client.set_leverage("ETH", 5, is_cross=False)
        mock_client.exchange.update_leverage.assert_called_once_with(5, "ETH", False)
        assert result == {"status": "ok"}

    def test_set_leverage_invalid_value(self, mock_client):
        with pytest.raises(ValueError, match="Leverage must be >= 1"):
            mock_client.set_leverage("BTC", 0)

    def test_set_leverage_exceeds_max(self, mock_client):
        with pytest.raises(ValueError, match="Max leverage"):
            mock_client.set_leverage("BTC", 100)

    def test_set_leverage_requires_auth(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
            client.market_specs = {}
        with pytest.raises(RuntimeError):
            client.set_leverage("BTC", 10)

    def test_add_isolated_margin(self, mock_client):
        mock_client.exchange.update_isolated_margin.return_value = {"status": "ok"}
        result = mock_client.add_isolated_margin("ETH", 100.0)
        mock_client.exchange.update_isolated_margin.assert_called_once_with(100.0, "ETH")
        assert result == {"status": "ok"}

    def test_add_isolated_margin_refuses_an_unknown_symbol_before_sending(self, mock_client):
        # The SDK failed with a bare KeyError while signing, no library exception.
        with pytest.raises(UnknownSymbolException):
            mock_client.add_isolated_margin("BTCX", 10.0)
        mock_client.exchange.update_isolated_margin.assert_not_called()

    def test_add_isolated_margin_rejects_a_negative_amount(self, mock_client):
        # The exchange would remove margin from the position instead.
        with pytest.raises(ValueError, match="amount must be greater than 0"):
            mock_client.add_isolated_margin("ETH", -100.0)
        mock_client.exchange.update_isolated_margin.assert_not_called()


# ── Order Tracking (Fills) ─────────────────────────────────────────────

SAMPLE_FILLS = [
    {
        "coin": "BTC",
        "side": "B",
        "px": "85000.5",
        "sz": "0.001",
        "closedPnl": "0",
        "dir": "Open Long",
        "oid": 12345,
        "crossed": True,
        "time": 1700000000000,
        "hash": "0xabc123",
        "fee": "0.085",
        "tid": 900001,  # Hyperliquid's trade id, in every fill
    },
    {
        "coin": "ETH",
        "side": "A",
        "px": "3200.0",
        "sz": "0.1",
        "closedPnl": "10.5",
        "dir": "Close Short",
        "oid": 67890,
        "crossed": False,
        "tid": 900002,
        "time": 1700000001000,
        "hash": "0xdef456",
        "fee": "0.032",
    },
]


class TestOrderTracking:
    def test_get_fills_all(self, mock_client):
        mock_client.info.user_fills.return_value = SAMPLE_FILLS
        fills = mock_client.get_fills()
        assert len(fills) == 2
        assert all(isinstance(f, Fill) for f in fills)
        assert fills[0].symbol == "BTC"
        assert fills[0].price == Decimal("85000.5")
        assert fills[0].order_id == 12345
        assert fills[0].crossed is True
        assert fills[0].fee == Decimal("0.085")

    def test_get_fills_filtered(self, mock_client):
        mock_client.info.user_fills.return_value = SAMPLE_FILLS
        fills = mock_client.get_fills("ETH")
        assert len(fills) == 1
        assert fills[0].symbol == "ETH"
        assert fills[0].direction == "Close Short"

    def test_get_fills_by_time(self, mock_client):
        mock_client.info.user_fills_by_time.return_value = [SAMPLE_FILLS[0]]
        fills = mock_client.get_fills_by_time(start_time=1700000000000)
        assert len(fills) == 1
        assert fills[0].symbol == "BTC"
        mock_client.info.user_fills_by_time.assert_called_once()
        address, start, end = mock_client.info.user_fills_by_time.call_args.args
        assert (address, start) == (mock_client.public_address, 1700000000000)
        assert end > start  # without end_time, the range ends when the call starts

    def test_get_fills_by_time_pages_past_2000_fills(self, mock_client):
        # One response holds at most 2000 fills, the oldest from startTime first (checked
        # live, 2026-11-13). The next page starts at the last fill's time, which is inclusive.
        def fill(tid, time_ms):
            return {**SAMPLE_FILLS[0], "tid": tid, "time": time_ms}

        first = [fill(i, 1700000000000 + i) for i in range(2000)]
        second = [first[-1]] + [fill(i, 1700000000000 + i) for i in range(2000, 2100)]
        mock_client.info.user_fills_by_time.side_effect = [first, second]
        fills = mock_client.get_fills_by_time(start_time=1700000000000)
        assert len(fills) == 2100
        assert mock_client.info.user_fills_by_time.call_args.args[1] == 1700000001999

    def test_get_fills_by_time_with_end(self, mock_client):
        mock_client.info.user_fills_by_time.return_value = SAMPLE_FILLS
        fills = mock_client.get_fills_by_time(
            start_time=1700000000000, end_time=1700000002000, symbol="BTC"
        )
        assert len(fills) == 1

    def test_get_order_status(self, mock_client):
        mock_client.info.query_order_by_oid.return_value = {"status": "filled", "oid": 12345}
        result = mock_client.get_order_status(12345)
        assert result["status"] == "filled"
        mock_client.info.query_order_by_oid.assert_called_once_with(
            mock_client.public_address, 12345
        )

    def test_get_fills_requires_auth(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
        with pytest.raises(RuntimeError):
            client.get_fills()

    def test_fill_without_fee(self, mock_client):
        fill_no_fee = {
            "coin": "BTC", "side": "B", "px": "85000", "sz": "0.001",
            "closedPnl": "0", "dir": "Open Long", "oid": 111,
            "crossed": True, "time": 1700000000000, "hash": "0x111",
        }
        mock_client.info.user_fills.return_value = [fill_no_fee]
        fills = mock_client.get_fills()
        assert fills[0].fee is None


# ── Fill history and its completeness ─────────────────────

class KeptFills:
    """userFillsByTime over the fills Hyperliquid still keeps: at most `page` per response,
    the oldest from start, start and end inclusive."""

    def __init__(self, times, page):
        self.fills = [{**SAMPLE_FILLS[0], "time": t, "tid": i} for i, t in enumerate(times)]
        self.page = page

    def __call__(self, address, start, end):
        return [f for f in self.fills if start <= f["time"] <= end][:self.page]


class TestFillHistory:
    @pytest.fixture
    def client(self, mock_client):
        mock_client._FILLS_PER_PAGE, mock_client._RETAINED_FILLS = 3, 10  # small limits, same logic
        return mock_client

    def kept(self, client, times):
        client.info.user_fills_by_time.side_effect = KeptFills(times, client._FILLS_PER_PAGE)

    def test_a_kept_fill_before_the_range_proves_it_complete(self, client):
        self.kept(client, [50, *range(100, 109)])  # 10 kept, the cap, but one is older than the range
        history = client.get_fill_history(100)
        assert ([f.time for f in history.fills], history.truncated) == (list(range(100, 109)), False)
        assert [f.trade_id for f in history.fills] == list(range(1, 10))

    def test_fewer_kept_fills_than_the_cap_prove_it_complete(self, client):
        self.kept(client, range(100, 109))  # 9 kept: Hyperliquid never dropped one
        assert not client.get_fill_history(50).truncated

    def test_at_the_cap_without_older_fills_the_range_may_be_cut(self, client):
        self.kept(client, range(100, 110))  # 10 kept, none before the range: older ones may be gone
        history = client.get_fill_history(50)
        assert (history.truncated, history.truncated_reason, len(history.fills)) == (True, "retention_window", 10)

    def test_a_range_in_the_past_counts_the_kept_fills_after_it(self, client):
        self.kept(client, range(100, 110))
        assert client.get_fill_history(50, end_time=104).truncated_reason == "retention_window"  # 5 + 5 later
        self.kept(client, range(100, 109))
        assert not client.get_fill_history(50, end_time=104).truncated  # 5 + 4 later

    def test_more_fills_in_one_millisecond_than_a_response_holds(self, client):
        self.kept(client, [100] * 5 + [101])
        history = client.get_fill_history(100)
        assert (history.truncated_reason, len(history.fills)) == ("same_millisecond_overflow", 3)

    def test_from_the_epoch_the_count_still_decides(self, client):
        self.kept(client, range(100, 110))  # nothing can be older than 0, but 10 kept: some may be gone
        assert client.get_fill_history(0).truncated_reason == "retention_window"
        self.kept(client, range(100, 109))
        assert not client.get_fill_history(0).truncated

    def test_a_count_that_cannot_be_completed_proves_nothing(self, client):
        self.kept(client, [*range(100, 105), 200, 200, 200, 200])  # after the range: one millisecond overflows
        assert client.get_fill_history(50, end_time=150).truncated_reason == "retention_window"

    def test_fills_arriving_during_the_call_are_counted(self, client):
        # 9 kept in the range, then a new fill arrives (and may push the oldest one out).
        self.kept(client, [*range(100, 109), 1001])
        with patch("fractrade_hl_simple.hyperliquid.time.time", return_value=1.0):  # now: 1000 ms
            assert client.get_fill_history(50).truncated_reason == "retention_window"

    def test_a_range_ending_after_now_is_not_counted_twice(self, client):
        self.kept(client, range(995, 1004))  # 9 kept, some after now: in the range and in the arrivals
        with patch("fractrade_hl_simple.hyperliquid.time.time", return_value=1.0):  # now: 1000 ms
            assert not client.get_fill_history(50, end_time=5000).truncated

    def test_fills_timed_after_this_computers_clock_are_counted(self, client):
        # The servers' clock may be ahead: a fill stamped 10 days after "now" still counts.
        self.kept(client, [*range(100, 109), 1000 + 10 * 86_400_000])
        with patch("fractrade_hl_simple.hyperliquid.time.time", return_value=1.0):
            assert client.get_fill_history(50).truncated_reason == "retention_window"

    def test_the_trade_id_is_the_identity(self, client):
        self.kept(client, [100, 101])
        fills = client.info.user_fills_by_time.side_effect.fills
        fills.append(dict(fills[0]))  # the same trade twice in one response
        assert [f.trade_id for f in client.get_fill_history(100).fills] == [0, 1]
        del fills[1]["tid"]  # Hyperliquid documents it for every fill: missing is an API change
        with pytest.raises(KeyError):
            client.get_fill_history(100)

    @pytest.mark.parametrize("start, end", [(200, 100), (10**15, None)])  # reversed; in the future
    def test_a_range_that_ends_before_it_starts(self, client, start, end):
        self.kept(client, [100])
        with pytest.raises(InvalidArgumentException):
            client.get_fill_history(start, end_time=end)

    def test_a_symbol(self, client):
        self.kept(client, range(100, 103))
        client.info.user_fills_by_time.side_effect.fills[1]["coin"] = "ETH"
        assert [f.symbol for f in client.get_fill_history(100, symbol="ETH").fills] == ["ETH"]


# ── Funding payments ─────────────────────────────────────────

def _payment(time, coin, usdc="-0.5"):
    return {"time": time, "hash": "0x" + "0" * 64, "delta": {
        "type": "funding", "coin": coin, "usdc": usdc, "szi": "-2.0", "fundingRate": "0.0000125", "nSamples": None}}


class TestUserFunding:
    def test_pages_through_the_range_with_every_payment_once(self, mock_client):
        mock_client._FUNDING_PER_PAGE = 3
        kept = [_payment(t, coin) for t in (100, 200, 300) for coin in ("BTC", "xyz:NVDA")]  # two per hour

        def user_funding(address, start, end):
            return [r for r in kept if start <= r["time"] <= end][:3]

        mock_client.info.user_funding_history.side_effect = user_funding
        payments = mock_client.get_user_funding(0, 1000)
        assert [(p.time, p.symbol, p.market) for p in payments] == [
            (100, "BTC", "perp"), (100, "xyz:NVDA", "hip3_perp"), (200, "BTC", "perp"),
            (200, "xyz:NVDA", "hip3_perp"), (300, "BTC", "perp"), (300, "xyz:NVDA", "hip3_perp")]
        assert (payments[0].size, payments[0].funding_rate, payments[0].usdc) == (
            Decimal("-2.0"), Decimal("0.0000125"), Decimal("-0.5"))

    def test_a_range_that_ends_before_it_starts(self, mock_client):
        with pytest.raises(InvalidArgumentException):
            mock_client.get_user_funding(200, 100)

    @pytest.mark.parametrize("times", [[100, 100, 100, 100], [100, 100, 100, 101]])
    def test_more_payments_at_one_time_than_a_response_holds_is_an_error_not_a_gap(self, mock_client, times):
        mock_client._FUNDING_PER_PAGE = 3
        kept = [_payment(t, f"COIN{i}") for i, t in enumerate(times)]
        mock_client.info.user_funding_history.side_effect = (
            lambda address, start, end: [r for r in kept if start <= r["time"] <= end][:3])
        with pytest.raises(HyperliquidException, match="cannot be read"):
            mock_client.get_user_funding(0, 1000)

    def test_the_new_functions_are_importable_from_the_package(self):
        import fractrade_hl_simple
        for name in ("get_user_funding", "get_fill_history", "get_markets", "get_spot_markets", "get_account_summary",
                     "SpotMarket"):
            assert callable(getattr(fractrade_hl_simple, name)) and name in fractrade_hl_simple.__all__

    def test_one_symbol_and_the_documented_leading_space(self, mock_client):
        mock_client.info.user_funding_history.return_value = [_payment(100, "BTC", " -3.625312"), _payment(100, "ETH")]
        payments = mock_client.get_user_funding(0, symbol="BTC")
        assert [(p.symbol, p.usdc) for p in payments] == [("BTC", Decimal("-3.625312"))]


# ── Spot symbols ───────────────────────────────────────────

# Real entries from mainnet spotMeta (2026-11-13). Token indexes are not list positions
# (SPCXD's index 610 is beyond the list), and @207 is HYPE quoted in USDT0, not USDC.
SPOT_META = {
    "tokens": [
        {"name": "USDC", "index": 0, "szDecimals": 8},
        {"name": "PURR", "index": 1, "szDecimals": 0},
        {"name": "HYPE", "index": 150, "szDecimals": 2},
        {"name": "USDT0", "index": 268, "szDecimals": 2},
        {"name": "SPCXD", "index": 610, "szDecimals": 2},
    ],
    "universe": [
        {"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True},
        {"tokens": [610, 0], "name": "@465", "index": 465, "isCanonical": False},
        {"tokens": [150, 268], "name": "@207", "index": 207, "isCanonical": False},
    ],
}


class TestSpotMarkets:
    def test_figures_are_matched_to_their_pair_by_coin_not_by_position(self, mock_client):
        # Hyperliquid lists the contexts by pair index, pairs no longer listed included (1190
        # contexts for 351 pairs, 2026-11-14): by position, HYPE/USDC got another pair's price.
        contexts = [
            {"coin": "@207", "markPx": "67.667", "midPx": "67.6995", "prevDayPx": "66.0", "dayNtlVlm": "95674.5"},
            {"coin": "@98", "markPx": "0.5", "midPx": None, "prevDayPx": "0.5", "dayNtlVlm": "0.0"},  # not listed
            {"coin": "@465", "markPx": "1.02", "midPx": None, "prevDayPx": "1.0", "dayNtlVlm": "0.0"},
            {"coin": "PURR/USDC", "markPx": "0.087973", "midPx": "0.0880285", "prevDayPx": "0.09",
             "dayNtlVlm": "929947.04"},
        ]
        mock_client.info.spot_meta_and_asset_ctxs.return_value = [SPOT_META, contexts]
        markets = mock_client.get_spot_markets()
        assert [(m.symbol, m.coin, m.base, m.quote, m.size_decimals) for m in markets] == [
            ("PURR", "PURR/USDC", "PURR", "USDC", 0), ("SPCXD", "@465", "SPCXD", "USDC", 2),
            ("HYPE/USDT0", "@207", "HYPE", "USDT0", 2)]
        assert (markets[2].mark_price, markets[2].mid_price, markets[2].day_volume) == (
            Decimal("67.667"), Decimal("67.6995"), Decimal("95674.5"))
        assert markets[1].mid_price is None  # a pair without orders has no mid price

    def test_the_symbols_are_the_ones_the_spot_methods_take(self, mock_client):
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.spot_meta_and_asset_ctxs.return_value = [
            SPOT_META, [{"coin": pair["name"], "markPx": "1"} for pair in SPOT_META["universe"]]]
        assert ([m.symbol for m in mock_client.get_spot_markets()]
                == list(mock_client._spot_coin_symbols().values()))


class TestSpotSymbols:
    def test_fills_of_a_pair_no_longer_listed_are_kept_under_hyperliquids_name(self, mock_client):
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.user_fills.return_value = [{**SAMPLE_FILLS[0], "coin": coin} for coin in ("@207", "@98", "BTC")]
        assert [f.symbol for f in mock_client.get_spot_fills()] == ["HYPE/USDT0", "@98"]

    def test_a_usdc_pair_asked_for_by_its_full_name_is_reported_as_without_a_filter(self, mock_client):
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.name_to_coin = {"PURR/USDC": "PURR/USDC", "HYPE/USDT0": "@207"}
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": "PURR/USDC", "side": "B", "limitPx": "0.08", "sz": "10.0", "origSz": "10.0", "oid": 1,
             "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc"}]
        for token in ("PURR", "PURR/USDC"):
            assert [o.symbol for o in mock_client.get_spot_open_orders(token)] == ["PURR"]
        assert [o.symbol for o in mock_client.get_spot_open_orders()] == ["PURR"]

    def test_spot_fills(self, mock_client):
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.user_fills.return_value = [
            {**SAMPLE_FILLS[0], "coin": coin} for coin in ("PURR/USDC", "@465", "@207", "BTC")
        ]
        fills = mock_client.get_spot_fills()
        assert [f.symbol for f in fills] == ["PURR", "SPCXD", "HYPE/USDT0"]
        # Filtering accepts the full pair name too.
        assert [f.symbol for f in mock_client.get_spot_fills("PURR/USDC")] == ["PURR"]

    def test_spot_open_orders(self, mock_client):
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": coin, "side": "B", "limitPx": "1.0", "sz": "10.0", "origSz": "10.0",
             "oid": oid, "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc"}
            for oid, coin in enumerate(("@465", "@207", "BTC"), start=1)
        ]
        orders = mock_client.get_spot_open_orders()
        assert [o.symbol for o in orders] == ["SPCXD", "HYPE/USDT0"]

    def test_spot_open_orders_filtered_by_a_pair_not_quoted_in_usdc(self, mock_client):
        # The symbol get_spot_open_orders() reports must also work as a filter.
        mock_client.info.name_to_coin = {"SPCXD/USDC": "@465", "HYPE/USDT0": "@207"}
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": coin, "side": "B", "limitPx": "1.0", "sz": "10.0", "origSz": "10.0",
             "oid": oid, "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc"}
            for oid, coin in enumerate(("@465", "@207"), start=1)
        ]
        orders = mock_client.get_spot_open_orders("HYPE/USDT0")
        assert [o.symbol for o in orders] == ["HYPE/USDT0"]

    def test_spot_trigger_orders_keep_their_trigger_fields(self, mock_client):
        # frontendOpenOrders has one format for perp and spot; spot orders used to become plain limits.
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": "@465", "side": "A", "limitPx": "0.9", "sz": "10.0", "origSz": "10.0", "oid": 7,
             "timestamp": 1700000000000, "orderType": "Stop Market", "tif": None, "isTrigger": True,
             "triggerPx": "0.95", "triggerCondition": "Price below 0.95", "reduceOnly": False,
             "isPositionTpsl": False}]
        order, = mock_client.get_spot_open_orders()
        assert (order.symbol, order.market, order.type, order.trigger_price, order.trigger_condition) == (
            "SPCXD", "spot", "stop_loss", Decimal("0.95"), "Price below 0.95")
        assert order.order_type.trigger == {"triggerPx": Decimal("0.95"), "isMarket": True, "tpsl": "sl"}

    @pytest.mark.parametrize("order_type, trigger", [
        ("Stop Market", {"isMarket": True, "tpsl": "sl"}), ("Stop Limit", {"isMarket": False, "tpsl": "sl"}),
        ("Take Profit Market", {"isMarket": True, "tpsl": "tp"}),
        ("Take Profit Limit", {"isMarket": False, "tpsl": "tp"}),
    ])
    def test_trigger_orders_keep_tpsl_and_whether_they_execute_as_market(self, mock_client, order_type, trigger):
        # tpsl and tif came out as True: dacite's int cast applied to bool and turned every
        # string into True.
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": "BTC", "side": "A", "limitPx": "79000.0", "sz": "0.1", "origSz": "0.1", "oid": 3,
             "timestamp": 1700000000000, "orderType": order_type, "tif": None, "isTrigger": True,
             "triggerPx": "80000.0", "triggerCondition": "Price below 80000", "reduceOnly": True}]
        order, = mock_client.get_open_orders()
        assert order.order_type.trigger == {"triggerPx": Decimal("80000.0"), **trigger}

    def test_limit_orders_keep_their_time_in_force(self, mock_client):
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": "BTC", "side": "B", "limitPx": "70000.0", "sz": "0.1", "origSz": "0.1", "oid": 4,
             "timestamp": 1700000000000, "orderType": "Limit", "tif": "Alo", "isTrigger": False}]
        order, = mock_client.get_open_orders()
        assert order.order_type.limit == {"tif": "Alo", "px": Decimal("70000.0")}

    def test_a_limit_order_has_no_trigger_price(self, mock_client):
        # Hyperliquid sends triggerPx "0.0" with isTrigger false; placed limit orders have None too.
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": "BTC", "side": "B", "limitPx": "70000.0", "sz": "0.1", "origSz": "0.1", "oid": 4,
             "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc", "isTrigger": False, "triggerPx": "0.0"}]
        order, = mock_client.get_open_orders()
        assert (order.type, order.trigger_price) == ("limit", None)

    def test_placed_orders_keep_their_order_type_strings(self):
        response = {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 12}}]}}}
        order = HyperliquidClient._order_from_response(
            response, "BTC", False, 0.1, 80000.0, {"trigger": {"triggerPx": 80000.0, "isMarket": True, "tpsl": "sl"}},
            True, "BTC stop loss", cloid=Cloid("0x" + "0" * 32))
        assert order.order_type.trigger == {"triggerPx": Decimal("80000.0"), "isMarket": True, "tpsl": "sl"}
        assert (order.type, order.trigger_price, order.reduce_only) == ("stop_loss", Decimal("80000.0"), True)


class TestSpotOrders:
    @staticmethod
    def purr(mock_client):
        mock_client.info.name_to_coin = {**mock_client.info.name_to_coin, "PURR/USDC": "PURR/USDC"}
        mock_client.info.coin_to_asset = {"PURR/USDC": 10000}
        mock_client.info.asset_to_sz_decimals = {10000: 0}
        mock_client.info.all_mids.return_value = {"PURR/USDC": "0.2"}
        mock_client.exchange.order.return_value = {"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"filled": {"oid": 1, "totalSz": "50", "avgPx": "0.2"}}]}}}

    def test_a_market_order_never_rests_on_the_book(self, mock_client):
        # Without a limit price it was sent Gtc at the mid plus slippage: what did not fill
        # at once stayed on the book as a buy far above the price.
        self.purr(mock_client)
        mock_client.create_spot_order("PURR", 50, True)
        sent = mock_client.exchange.order.call_args.kwargs
        assert (sent["order_type"], sent["limit_px"]) == ({"limit": {"tif": "Ioc"}}, 0.21)
        mock_client.create_spot_order("PURR", 50, True, limit_price=0.19)
        assert mock_client.exchange.order.call_args.kwargs["order_type"] == {"limit": {"tif": "Gtc"}}

    @pytest.mark.parametrize("size, price", [(float("inf"), None), (50, float("nan")), (50, 0), (50, 1e-12)])
    def test_a_size_or_price_that_is_no_positive_number_is_refused_before_sending(self, mock_client, size, price):
        # A NaN or 0 price was sent; an infinite size raised a raw decimal error.
        self.purr(mock_client)
        with pytest.raises(InvalidArgumentException):
            mock_client.create_spot_order("PURR", size, True, limit_price=price)
        mock_client.exchange.order.assert_not_called()


# ── Coin classes ──────────────────────────────────────────

class TestCoinClasses:
    @pytest.mark.parametrize("coin, market", [
        ("BTC", "perp"), ("kPEPE", "perp"), ("xyz:NVDA", "hip3_perp"), ("@107", "spot"), ("PURR/USDC", "spot"),
        ("HYPE/USDT0", "spot"), ("#12", "outcome"), ("+5", "outcome"), ("o5", "outcome"), ("", "unknown"),
        ("two words", "unknown"), ("xyz:", "unknown"),
    ])
    def test_every_kind_of_coin_name(self, coin, market):
        assert coin_market(coin) == market

    def test_orders_and_fills_know_their_market_from_the_raw_coin(self, mock_client):
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": coin, "side": "B", "limitPx": "1.0", "sz": "1.0", "origSz": "1.0", "oid": oid,
             "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc"}
            for oid, coin in enumerate(("BTC", "@107", "#12"), start=1)]
        assert [o.market for o in mock_client.get_open_orders()] == ["perp", "spot", "outcome"]
        mock_client.info.user_fills.return_value = [{**SAMPLE_FILLS[0], "coin": coin} for coin in ("xyz:NVDA", "@207")]
        assert [f.market for f in mock_client.get_fills()] == ["hip3_perp", "spot"]

    def test_spot_reads_rename_the_coin_but_keep_the_market(self, mock_client):
        mock_client.info.spot_meta.return_value = SPOT_META
        mock_client.info.user_fills.return_value = [{**SAMPLE_FILLS[0], "coin": "@207"}]
        assert [(f.symbol, f.market) for f in mock_client.get_spot_fills()] == [("HYPE/USDT0", "spot")]
        mock_client.info.frontend_open_orders.return_value = [
            {"coin": "@465", "side": "B", "limitPx": "1.0", "sz": "1.0", "origSz": "1.0", "oid": 1,
             "timestamp": 1700000000000, "orderType": "Limit", "tif": "Gtc"}]
        assert [(o.symbol, o.market) for o in mock_client.get_spot_open_orders()] == [("SPCXD", "spot")]

    def test_pickles_keep_the_market_and_older_ones_get_unknown(self):
        fill = Fill(symbol="HYPE", side="B", price=Decimal("1"), size=Decimal("1"), closed_pnl=Decimal("0"),
                    direction="Buy", order_id=1, crossed=True, time=0, hash="", market="spot")
        for copied in (pickle.loads(pickle.dumps(fill)), copy.copy(fill), copy.deepcopy(fill)):
            assert copied == fill and copied.market == "spot"
        _, values = fill.__getstate__()
        older = Fill.__new__(Fill)  # pickled before the market field: its raw coin is not known
        older.__setstate__((None, {k: v for k, v in values.items() if k not in ("market", "trade_id")}))
        assert (older.symbol, older.market, older.trade_id) == ("HYPE", "unknown", None)

    def test_an_order_pickled_with_a_text_id_gets_its_number(self):
        order = Order(order_id=5, symbol="BTC", is_buy=True, size=Decimal("1"), order_type=OrderType(limit={}),
                      status="open", created_at=0)
        _, values = order.__getstate__()
        older = Order.__new__(Order)  # pickled when order_id was text, before the cloid field
        older.__setstate__((None, {**{k: v for k, v in values.items() if k != "cloid"}, "order_id": "5"}))
        assert (older.order_id, older.cloid) == (5, None)

    def test_a_subclass_keeps_its_own_attributes_when_copied(self):
        class TaggedFill(Fill):
            pass

        fill = TaggedFill(symbol="BTC", side="B", price=Decimal("1"), size=Decimal("1"), closed_pnl=Decimal("0"),
                          direction="Open Long", order_id=1, crossed=True, time=0, hash="")
        fill.tag = "bot-1"
        for copied in (copy.copy(fill), copy.deepcopy(fill)):
            assert (copied.tag, copied.market) == ("bot-1", "perp")

    def test_construction_is_unchanged_and_the_field_can_be_given(self):
        fill = Fill(symbol="BTC", side="B", price=Decimal("1"), size=Decimal("1"), closed_pnl=Decimal("0"),
                    direction="Open Long", order_id=1, crossed=True, time=0, hash="")
        assert fill.market == "perp"
        assert Fill(symbol="HYPE", side="B", price=Decimal("1"), size=Decimal("1"), closed_pnl=Decimal("0"),
                    direction="Buy", order_id=1, crossed=True, time=0, hash="", market="spot").market == "spot"


# ── User role ─────────────────────────────────────────────────────────

class TestUserRole:
    """get_user_role is what the MCP server's signer check asks."""

    def test_asks_hyperliquid_about_the_address_given_else_the_clients_own(self, mock_client):
        answer = {"role": "agent", "data": {"user": "0x" + "1" * 40}}
        mock_client.info.user_role.return_value = answer
        assert mock_client.get_user_role("0x" + "2" * 40) == answer
        mock_client.get_user_role()
        assert [c.args for c in mock_client.info.user_role.call_args_list] == [
            ("0x" + "2" * 40,), (mock_client.public_address,)]

    def test_an_invalid_address_is_refused_before_any_request(self, mock_client):
        with pytest.raises(ValueError):
            mock_client.get_user_role("0x12")
        mock_client.info.user_role.assert_not_called()


# ── Bulk Orders ────────────────────────────────────────────────────────

class TestBulkOrders:
    def test_bulk_order(self, mock_client):
        mock_client.exchange.bulk_orders.return_value = {
            "status": "ok",
            "response": {"data": {"statuses": [
                {"resting": {"oid": 1}},
                {"resting": {"oid": 2}},
            ]}},
        }
        orders = [
            {"symbol": "BTC", "is_buy": True, "size": 0.001, "limit_price": 80000.0},
            {"symbol": "ETH", "is_buy": False, "size": 0.01, "limit_price": 3500.0},
        ]
        result = mock_client.bulk_order(orders)
        assert result["status"] == "ok"
        mock_client.exchange.bulk_orders.assert_called_once()

        # Verify order requests were formatted correctly
        call_args = mock_client.exchange.bulk_orders.call_args[0][0]
        assert len(call_args) == 2
        assert call_args[0]["coin"] == "BTC"
        assert call_args[0]["is_buy"] is True
        assert call_args[1]["coin"] == "ETH"
        assert call_args[1]["is_buy"] is False

    def test_bulk_cancel(self, mock_client):
        mock_client.exchange.bulk_cancel.return_value = {"status": "ok"}
        cancels = [
            {"symbol": "BTC", "order_id": "12345"},
            {"symbol": "ETH", "order_id": 67890},
        ]
        result = mock_client.bulk_cancel(cancels)
        assert result["status"] == "ok"
        mock_client.exchange.bulk_cancel.assert_called_once_with([
            {"coin": "BTC", "oid": 12345},
            {"coin": "ETH", "oid": 67890},
        ])

    def test_bulk_cancel_refuses_an_unknown_symbol_before_sending(self, mock_client):
        # The SDK failed with a bare KeyError while signing, no library exception.
        with pytest.raises(UnknownSymbolException):
            mock_client.bulk_cancel([{"symbol": "BTC", "order_id": 1}, {"symbol": "xyz:NVDA", "order_id": 2}])
        mock_client.exchange.bulk_cancel.assert_not_called()

    @pytest.mark.parametrize("change", [{"time_in_force": "IOC"}, {"is_buy": "yes"}, {"reduce_only": 1}])
    def test_bulk_order_refuses_invalid_flags_before_sending(self, mock_client, change):
        # They were signed and sent; the exchange then refused the whole action.
        with pytest.raises(InvalidArgumentException, match=next(iter(change))):
            mock_client.bulk_order([self.ORDERS[0], {**self.ORDERS[1], **change}])
        mock_client.exchange.bulk_orders.assert_not_called()

    def test_bulk_order_requires_auth(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
        with pytest.raises(RuntimeError):
            client.bulk_order([])

    def test_bulk_order_validates_orders(self, mock_client):
        with pytest.raises(InvalidArgumentException, match="size"):
            mock_client.bulk_order([{"symbol": "BTC"}])  # missing required fields
        with pytest.raises(InvalidArgumentException, match="cloid"):
            mock_client.bulk_order([{**self.ORDERS[0], "cloid": "0x" + "g" * 32}])
        mock_client.exchange.bulk_orders.assert_not_called()

    ORDERS = [
        {"symbol": "BTC", "is_buy": True, "size": 0.001, "limit_price": 80000.0},
        {"symbol": "ETH", "is_buy": False, "size": 0.01, "limit_price": 3500.0},
    ]

    @staticmethod
    def answer(*statuses):
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": list(statuses)}}}

    def test_bulk_order_sends_the_callers_cloids_and_returns_each_outcome(self, mock_client):
        cloid = "0x" + "ab" * 16
        mock_client.exchange.bulk_orders.return_value = self.answer(
            {"resting": {"oid": 1}}, {"error": "Insufficient margin to place order."})
        result = mock_client.bulk_order([{**self.ORDERS[0], "cloid": cloid}, self.ORDERS[1]])
        sent = mock_client.exchange.bulk_orders.call_args[0][0]
        assert sent[0]["cloid"].to_raw() == cloid and "cloid" not in sent[1]
        # The first order is placed even though the second was rejected.
        assert result == self.answer({"resting": {"oid": 1}}, {"error": "Insufficient margin to place order."})

    @pytest.mark.parametrize("answer", [
        pytest.param({"status": "ok"}, id="no-statuses"),
        pytest.param(answer({"resting": {"oid": 1}}), id="one-status-for-two-orders"),
        pytest.param(answer({"resting": {}}, {"resting": {"oid": 2}}), id="status-without-oid"),
        pytest.param(answer("waitingForFill", {"resting": {"oid": 2}}), id="undocumented-status"),
    ])
    def test_an_answer_without_a_documented_status_per_order_is_an_unknown_outcome(self, mock_client, answer):
        mock_client.exchange.bulk_orders.return_value = answer
        with pytest.raises(ActionOutcomeUnknownException, match="bulk order"):
            mock_client.bulk_order(self.ORDERS)

    def test_a_bulk_order_refused_as_a_whole_is_a_rejection(self, mock_client):
        mock_client.exchange.bulk_orders.return_value = {"status": "err", "response": "User or API Wallet does not exist."}
        with pytest.raises(OrderRejectedException, match="does not exist"):
            mock_client.bulk_order(self.ORDERS)


# ── Funding History ────────────────────────────────────────────────────

class TestFundingHistory:
    def test_get_funding_history(self, mock_client):
        mock_client.info.funding_history.return_value = [
            {"time": 1700000000000, "fundingRate": "0.0001", "premium": "0.00005"},
            {"time": 1700003600000, "fundingRate": "-0.00005", "premium": "-0.00002"},
        ]
        result = mock_client.get_funding_history("BTC", start_time=1700000000000)
        assert len(result) == 2
        assert result[0]["funding_rate"] == 0.0001
        assert result[0]["time"] == 1700000000000
        assert result[1]["premium"] == -0.00002
        mock_client.info.funding_history.assert_called_once_with("BTC", 1700000000000, None)

    def test_get_funding_history_unknown_symbol_before_any_request(self, mock_client):
        with pytest.raises(UnknownSymbolException):
            mock_client.get_funding_history("BTCC", start_time=1700000000000)
        assert not mock_client.info.funding_history.called

    def test_get_funding_history_with_end_time(self, mock_client):
        mock_client.info.funding_history.return_value = []
        mock_client.get_funding_history("ETH", 1700000000000, 1700100000000)
        mock_client.info.funding_history.assert_called_once_with(
            "ETH", 1700000000000, 1700100000000
        )


# ── Funding Rate URL Fix ──────────────────────────────────────────────

class TestFundingRatesFix:
    def test_uses_info_post(self, mock_client):
        """Verify get_funding_rates uses self.info.post instead of raw requests."""
        mock_client.info.meta.return_value = {"universe": [{"name": "BTC"}]}
        mock_client.info.post.return_value = [
            ["BTC", [["HlPerp", {"fundingRate": "0.0001"}]]]
        ]

        result = mock_client.get_funding_rates()

        mock_client.info.post.assert_any_call("/info", {"type": "predictedFundings"})
        assert len(result) == 1
        assert result[0]["symbol"] == "BTC"
        assert result[0]["funding_rate"] == 0.0001

    def test_an_unknown_symbol_and_a_missing_rate_are_told_apart(self, mock_client):
        mock_client.info.meta.return_value = {"universe": [{"name": "BTC"}, {"name": "ETH"}]}
        mock_client.info.post.return_value = [["BTC", [["HlPerp", {"fundingRate": "0.0001"}]]]]
        with pytest.raises(UnknownSymbolException, match="Unknown symbol 'BTCC'"):
            mock_client.get_funding_rates("BTCC")
        with pytest.raises(HyperliquidException, match="no predicted funding rate for ETH") as error:
            mock_client.get_funding_rates("ETH")
        assert not isinstance(error.value, ValueError)  # not an input error


# ── Meta + Asset Context ──────────────────────────────────────────────

class TestMetaAssetContext:
    def test_fetch_market_specs_uses_meta_and_asset_ctxs(self):
        # Clear class-level cache to ensure fresh fetch
        HyperliquidClient._cached_market_specs = None
        HyperliquidClient._cached_market_specs_at = 0
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS) as mock_info_cls, \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            mock_info = mock_info_cls.return_value
            mock_info.post.return_value = [
                {"universe": [
                    {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "onlyIsolated": False},
                ]},
                [{"funding": "0.0001", "openInterest": "1000", "markPx": "85000"}],
            ]
            client = HyperliquidClient(max_retries=0, cache_market_specs=False)
            assert "BTC" in client.market_specs
            assert client.market_specs["BTC"]["size_decimals"] == 5
            assert client.market_specs["BTC"]["max_leverage"] == 50
            assert client.market_specs["BTC"]["funding"] == "0.0001"
            assert client.market_specs["BTC"]["mark_price"] == "85000"


# ── Markets with their current figures ─────────────────────────────────

class TestMarkets:
    def test_every_dex_with_its_figures_and_open_interest_cap(self, mock_client):
        mock_client.perp_dexs = ["", "xyz"]
        responses = {
            ("metaAndAssetCtxs", ""): [
                {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 56},
                              {"name": "OLD", "szDecimals": 0, "maxLeverage": 3, "isDelisted": True}]},
                [{"funding": "0.0000105649", "openInterest": "38128.62678", "prevDayPx": "69307.0",
                  "dayNtlVlm": "3797040662.35", "premium": "-0.0003884154", "oraclePx": "67196.1",
                  "markPx": "67166.1", "midPx": "67169.5", "impactPxs": ["67169.0", "67170.0"]},
                 {"funding": "0.0", "openInterest": "0.0", "prevDayPx": "0.1", "dayNtlVlm": "0.0",
                  "premium": None, "oraclePx": "0.1", "markPx": "0.1", "midPx": None, "impactPxs": None}]],
            ("perpsAtOpenInterestCap", ""): ["OLD"],
            ("metaAndAssetCtxs", "xyz"): [
                {"universe": [{"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25, "marginMode": "noCross"}]},
                [{"funding": "0.00001", "openInterest": "10", "prevDayPx": "2400", "dayNtlVlm": "1000",
                  "premium": "0.0", "oraclePx": "2401", "markPx": "2400.5", "midPx": "2400.4"}]],
            ("perpsAtOpenInterestCap", "xyz"): [],
        }
        mock_client.info.post.side_effect = lambda path, payload: responses[(payload["type"], payload["dex"])]
        btc, old, gold = mock_client.get_markets()
        assert (btc.symbol, btc.dex, btc.size_decimals, btc.max_leverage, btc.margin_mode, btc.delisted) == (
            "BTC", None, 5, 40, "cross", False)
        assert (btc.mark_price, btc.mid_price, btc.funding, btc.day_volume_usd) == (
            Decimal("67166.1"), Decimal("67169.5"), Decimal("0.0000105649"), Decimal("3797040662.35"))
        assert (old.delisted, old.at_open_interest_cap, old.mid_price, old.premium) == (True, True, None, None)
        assert (gold.dex, gold.margin_mode, gold.at_open_interest_cap) == ("xyz", "no_cross", False)

    def test_a_hip3_market_at_its_cap_with_or_without_the_dex_prefix(self, mock_client):
        # The docs show the cap list only for the first dex; both spellings count.
        mock_client.perp_dexs = ["xyz"]
        for capped in (["xyz:GOLD"], ["GOLD"]):
            mock_client.info.post.side_effect = lambda path, payload, capped=capped: (
                [{"universe": [{"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25}]}, [{"markPx": "2400"}]]
                if payload["type"] == "metaAndAssetCtxs" else capped)
            assert mock_client.get_markets()[0].at_open_interest_cap
        mock_client.perp_dexs = [""]
        mock_client.info.post.side_effect = lambda path, payload: (
            [{"universe": [{"name": "GOLD", "szDecimals": 4, "maxLeverage": 25}]}, [{"markPx": "1"}]]
            if payload["type"] == "metaAndAssetCtxs" else ["xyz:GOLD"])
        assert not mock_client.get_markets()[0].at_open_interest_cap  # another dex's GOLD

    def test_a_context_missing_for_a_market_is_an_error(self, mock_client):
        # Hyperliquid documents one context per market; a mismatch is an API change, not hidden.
        mock_client.info.post.side_effect = lambda path, payload: (
            [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}, []]
            if payload["type"] == "metaAndAssetCtxs" else [])
        with pytest.raises(ValueError):
            mock_client.get_markets()


# ── Account modes and balances ─────────────────────────────────────────
# Shapes as returned on 2026-11-13, amounts invented. In the unified account the default dex
# is empty, the xyz dex holds the position, and the spot USDC already includes its PnL; the
# xyz account value is only the part of that USDC backing the position.

def _position(coin):
    return {"type": "oneWay", "position": {
        "coin": coin, "szi": "50.0", "entryPx": "160.4", "positionValue": "8000.0",
        "unrealizedPnl": "-20.0", "returnOnEquity": "-0.05", "marginUsed": "400.0",
        "leverage": {"type": "cross", "value": 20}, "liquidationPx": "142.0"}}


def _perp_state(account_value, positions=()):
    summary = {"accountValue": account_value, "totalNtlPos": "8000.0" if positions else "0.0",
               "totalRawUsd": "-7595.0" if positions else "0.0",
               "totalMarginUsed": "400.0" if positions else "0.0"}
    return {"marginSummary": summary, "crossMarginSummary": summary, "withdrawable": "5.0",
            "crossMaintenanceMarginUsed": "200.0", "assetPositions": list(positions), "time": 1}


def _spot_state(usdc_total, usdc_hold, other=()):
    usdc = {"coin": "USDC", "token": 0, "total": usdc_total, "hold": usdc_hold, "entryNtl": "0.0"}
    return {"balances": [usdc, *other]}

HYPE = {"coin": "HYPE", "token": 150, "total": "2.0", "hold": "0.0", "entryNtl": "70.0"}


class TestAccountBalances:
    @pytest.mark.parametrize("raw, mode", [
        ("disabled", "standard"), ("default", "standard"), ("unifiedAccount", "unified"),
        ("portfolioMargin", "portfolio_margin"), ("dexAbstraction", "unknown"), ("newMode", "unknown"),
    ])
    def test_account_mode(self, mock_client, raw, mode):
        mock_client.info.query_user_abstraction_state.return_value = raw
        assert mock_client.get_account_mode() == mode

    def test_unified_account_counts_the_spot_usdc_once(self, mock_client):
        mock_client.perp_dexs = ["", "xyz"]
        mock_client.info.query_user_abstraction_state.return_value = "unifiedAccount"
        perp = {"": _perp_state("0.0"), "xyz": _perp_state("405.0", [_position("xyz:NVDA")])}
        mock_client.info.user_state.side_effect = lambda address, dex="": perp[dex]
        mock_client.info.spot_user_state.return_value = _spot_state("1000.0", "400.0", [HYPE])
        mock_client.info.all_mids.return_value = {"HYPE": "40.0"}
        mock_client.info.name_to_coin = {}

        assert mock_client.get_perp_balance() == Decimal("1000.0")
        assert mock_client.get_all_balances() == Decimal("1080.0")
        detail = mock_client.get_all_balances(simple=False)
        assert detail["total_balance"] == Decimal("1080.0")
        assert detail["account_mode"] == "unified"
        assert detail["perp"]["balance"] == detail["spot"].tokens["USDC"].amount == Decimal("1000.0")
        assert [p.symbol for p in detail["perp"]["positions"]] == ["xyz:NVDA"]

    def test_standard_account_adds_perp_and_spot(self, mock_client):
        mock_client.info.query_user_abstraction_state.return_value = "disabled"
        mock_client.info.user_state.return_value = _perp_state("1000.0", [_position("BTC")])
        mock_client.info.spot_user_state.return_value = _spot_state("50.0", "0.0")
        mock_client.info.all_mids.return_value = {"BTC": "80000.0"}
        mock_client.info.name_to_coin = {}

        assert mock_client.get_perp_balance() == Decimal("1000.0")
        assert mock_client.get_all_balances() == Decimal("1050.0")
        assert mock_client.get_all_balances(simple=False)["total_balance"] == Decimal("1050.0")

    @pytest.mark.parametrize("raw", ["portfolioMargin", "dexAbstraction"])
    def test_no_single_balance_for_other_modes(self, mock_client, raw):
        mock_client.info.query_user_abstraction_state.return_value = raw
        with pytest.raises(UnsupportedAccountModeException):
            mock_client.get_perp_balance()
        with pytest.raises(UnsupportedAccountModeException):
            mock_client.get_all_balances()


class TestAccountSummary:
    def _unified(self, mock_client, mode="unifiedAccount"):
        mock_client.perp_dexs = ["", "xyz"]
        mock_client.info.query_user_abstraction_state.return_value = mode
        perp = {"": _perp_state("0.0"), "xyz": _perp_state("405.0", [_position("xyz:NVDA")])}
        mock_client.info.user_state.side_effect = lambda address, dex="": perp[dex]
        outcome = {"coin": "+12", "token": 99999, "total": "3.0", "hold": "0.0", "entryNtl": "0.0"}
        mock_client.info.spot_user_state.return_value = _spot_state("1000.0", "400.0", [HYPE, outcome])
        mock_client.info.all_mids.return_value = {"HYPE": "40.0"}
        mock_client.info.name_to_coin = {}

    def test_unified_the_shared_collateral_and_what_is_free(self, mock_client):
        self._unified(mock_client)
        summary = mock_client.get_account_summary()
        assert (summary.mode, summary.account_value, summary.available_to_trade, summary.margin_used) == (
            "unified", Decimal("1080.0"), Decimal("600.0"), Decimal("400.0"))  # 1000 USDC + 2 HYPE at 40
        assert summary.account_value == mock_client.get_all_balances()
        usdc, hype, outcome = summary.balances
        assert (usdc.token, usdc.total, usdc.hold, usdc.available, usdc.price) == (
            "USDC", Decimal("1000.0"), Decimal("400.0"), Decimal("600.0"), Decimal("1"))
        assert (hype.usd_value, outcome.market, outcome.price, outcome.usd_value) == (Decimal("80.0"), "outcome", None, None)
        default, xyz = summary.dexes
        assert (default.dex, xyz.dex, xyz.account_value, xyz.withdrawable, xyz.margin_used) == (
            None, "xyz", None, None, Decimal("400.0"))  # dex figures are only the part backing positions
        assert [p.symbol for p in xyz.positions] == ["xyz:NVDA"]
        assert set(summary.sources) == {"account_value", "available_to_trade", "margin_used", "dexes", "unpriced"}
        assert summary.sources["unpriced"] == "+12 (not in account_value)"

    def test_unpriced_tokens_and_the_dexes_read_are_named(self, mock_client):
        self._unified(mock_client)
        settled = {"coin": "o7", "token": 99998, "total": "1.0", "hold": "0.0", "entryNtl": "0.0"}
        unknown = {"coin": "NEWTOKEN", "token": 777, "total": "5.0", "hold": "0.0", "entryNtl": "0.0"}
        mock_client.info.spot_user_state.return_value = _spot_state("1000.0", "400.0", [HYPE, unknown, settled])
        summary = mock_client.get_account_summary()
        assert [(b.token, b.market, b.price) for b in summary.balances[2:]] == [
            ("NEWTOKEN", "spot", None), ("o7", "outcome", None)]
        assert summary.account_value == Decimal("1080.0")  # the unpriced ones left out, and said so
        assert summary.sources["unpriced"] == "NEWTOKEN, o7 (not in account_value)"
        assert summary.sources["dexes"] == "default, xyz (other dexes are not read)"

    def test_margin_used_is_the_total_not_the_cross_margin(self, mock_client):
        self._unified(mock_client)
        state = _perp_state("405.0", [_position("xyz:NVDA")])
        state["crossMarginSummary"] = {**state["crossMarginSummary"], "totalMarginUsed": "150.0"}  # 250 isolated
        mock_client.info.user_state.side_effect = lambda address, dex="": state if dex else _perp_state("0.0")
        xyz = mock_client.get_account_summary().dexes[1]
        assert (xyz.margin_used, xyz.cross_maintenance_margin_used) == (Decimal("400.0"), Decimal("200.0"))

    def test_a_clearinghouse_response_in_another_shape_is_no_empty_account(self, mock_client):
        self._unified(mock_client)
        mock_client.info.user_state.side_effect = lambda address, dex="": {"error": "unexpected"}
        with pytest.raises(KeyError):  # an API change or a bug shows as itself, never as zero balances
            mock_client.get_account_summary()

    def test_standard_each_dex_with_its_own_collateral(self, mock_client):
        mock_client.perp_dexs = ["", "xyz"]
        mock_client.info.query_user_abstraction_state.return_value = "disabled"
        perp = {"": _perp_state("1000.0", [_position("BTC")]), "xyz": _perp_state("200.0")}
        mock_client.info.user_state.side_effect = lambda address, dex="": perp[dex]
        mock_client.info.spot_user_state.return_value = _spot_state("50.0", "0.0")
        mock_client.info.all_mids.return_value = {}
        mock_client.info.name_to_coin = {}
        summary = mock_client.get_account_summary()
        assert (summary.mode, summary.account_value, summary.available_to_trade, summary.margin_used) == (
            "standard", Decimal("1250.0"), None, Decimal("400.0"))
        assert [(d.dex, d.account_value, d.withdrawable) for d in summary.dexes] == [
            (None, Decimal("1000.0"), Decimal("5.0")), ("xyz", Decimal("200.0"), Decimal("5.0"))]

    @pytest.mark.parametrize("raw, mode", [("portfolioMargin", "portfolio_margin"), ("dexAbstraction", "unknown")])
    def test_other_modes_have_balances_but_no_totals(self, mock_client, raw, mode):
        self._unified(mock_client, raw)
        summary = mock_client.get_account_summary()
        assert (summary.mode, summary.account_value, summary.available_to_trade, summary.margin_used) == (
            mode, None, None, None)
        assert [b.token for b in summary.balances] == ["USDC", "HYPE", "+12"] and len(summary.dexes) == 2

    def test_another_wallet(self, mock_client):
        self._unified(mock_client)
        mock_client.get_account_summary(OTHER)
        assert mock_client.info.spot_user_state.call_args.args[0] == OTHER
        assert {call.args[0] for call in mock_client.info.user_state.call_args_list} == {OTHER}


# ── Reading any wallet without its key ───────────────────────

OTHER = "0x" + "ab" * 20


@pytest.fixture
def keyless(mock_client):
    """A client without an account, as HyperliquidClient(trust_env=False) makes it."""
    mock_client.account = None
    mock_client.info.user_state.return_value = _perp_state("0.0")
    for name in ("frontend_open_orders", "user_fills", "user_fills_by_time"):
        getattr(mock_client.info, name).return_value = []
    mock_client.info.query_order_by_oid.return_value = {"status": "unknownOid"}
    return mock_client


# (method, positional arguments, the SDK call that must get the address)
READS = [
    ("get_positions", (), "user_state"),
    ("get_open_orders", (), "frontend_open_orders"),
    ("get_spot_open_orders", (), "frontend_open_orders"),
    ("get_open_order_by_id", ("BTC", 1), "frontend_open_orders"),
    ("get_order_status", (1,), "query_order_by_oid"),
    ("get_fills", (), "user_fills"),
    ("get_fills_by_time", (0, 1), "user_fills_by_time"),
    ("get_spot_fills", (), "user_fills"),
    ("get_stop_loss_price", ("BTC",), "frontend_open_orders"),
    ("get_take_profit_price", ("BTC",), "frontend_open_orders"),
    ("get_position_size", ("BTC",), "user_state"),
    ("get_position_direction", ("BTC",), "user_state"),
    ("has_position", ("BTC",), "user_state"),
    ("has_active_orders", (), "frontend_open_orders"),
]
ADDRESS_FIRST = ["get_user_state", "get_account_mode", "get_perp_balance", "get_spot_balance",
                 "get_all_balances", "get_portfolio"]  # took an address already


KEY = ACCOUNT.private_key


def client_from(**kwargs):
    with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
         patch('fractrade_hl_simple.hyperliquid.Exchange'):
        return HyperliquidClient(**kwargs)


def no_env_file():
    """Keep the repository's own .env out: the client and from_env() both load one."""
    from contextlib import ExitStack
    stack = ExitStack()
    for module in ("hyperliquid", "models"):
        stack.enter_context(patch(f"fractrade_hl_simple.{module}.load_env_file"))
    return stack


class TestAccountSetup:
    """A key that is given must work: it used to become a silent read-only client, logged at
    DEBUG only, so the mistake showed up at the first order."""

    @pytest.mark.parametrize("account, problem", [
        ({"private_key": KEY}, "must be a HyperliquidAccount"),
        (HyperliquidAccount(private_key=KEY), "public_address is required"),
        (HyperliquidAccount(private_key=KEY, public_address="0x1234"), "public_address must be 0x"),
        (HyperliquidAccount(private_key="", public_address=ACCOUNT.public_address), "private_key is required"),
        (HyperliquidAccount(private_key=KEY[:-1], public_address=ACCOUNT.public_address), "64 hex"),  # eth_account takes it
        (HyperliquidAccount(private_key="0x" + "zz" * 32, public_address=ACCOUNT.public_address), "64 hex"),
        (HyperliquidAccount(private_key="0x" + "00" * 32, public_address=ACCOUNT.public_address), "not a valid"),
    ])
    def test_an_unusable_account_raises_at_creation(self, account, problem):
        with pytest.raises(InvalidArgumentException, match=problem) as error:
            client_from(account=account)
        assert KEY[2:-1] not in str(error.value) and isinstance(error.value, ValueError)

    def test_a_key_without_0x_is_fine(self):
        account = HyperliquidAccount(private_key=KEY[2:], public_address=ACCOUNT.public_address)
        assert client_from(account=account).is_authenticated()

    @pytest.mark.parametrize("variables, problem", [
        ({"HYPERLIQUID_PRIVATE_KEY": KEY}, "HYPERLIQUID_PUBLIC_ADDRESS"),
        ({"HYPERLIQUID_PRIVATE_KEY": KEY[:-1], "HYPERLIQUID_PUBLIC_ADDRESS": ACCOUNT.public_address}, "64 hex"),
    ])
    def test_a_key_in_the_environment_that_cannot_sign_raises(self, monkeypatch, variables, problem):
        monkeypatch.delenv("HYPERLIQUID_PUBLIC_ADDRESS", raising=False)
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        with no_env_file(), pytest.raises(ConfigurationException, match=problem) as error:
            client_from()
        assert KEY[2:-1] not in str(error.value)

    def test_without_a_key_in_the_environment_the_client_reads_public_data(self, monkeypatch):
        monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)
        with no_env_file():
            assert not client_from().is_authenticated()

    def test_a_working_key_in_the_environment(self, monkeypatch):
        monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", KEY)
        monkeypatch.setenv("HYPERLIQUID_PUBLIC_ADDRESS", ACCOUNT.public_address)
        with no_env_file():
            assert client_from().public_address == ACCOUNT.public_address


class TestReadingWithoutKey:
    def test_trust_env_false_takes_nothing_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", ACCOUNT.private_key)
        monkeypatch.setenv("HYPERLIQUID_PUBLIC_ADDRESS", ACCOUNT.public_address)
        monkeypatch.setenv("HYPERLIQUID_ENV", "testnet")
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange') as exchange_cls, \
             patch('fractrade_hl_simple.hyperliquid.load_env_file') as load:
            reader = HyperliquidClient(trust_env=False)
            trader = HyperliquidClient(account=ACCOUNT, trust_env=False)
        assert not load.called
        assert not reader.is_authenticated() and reader.base_url == constants.MAINNET_API_URL
        assert trader.is_authenticated() and trader.base_url == constants.MAINNET_API_URL
        assert exchange_cls.call_count == 1  # only the client with an account can sign
        with pytest.raises(RuntimeError, match="requires authentication"):
            reader.buy("BTC", 0.001)

    @pytest.mark.parametrize("method, args, sdk", READS)
    def test_every_read_reads_the_wallet_it_is_given(self, keyless, method, args, sdk):
        getattr(keyless, method)(*args, address=OTHER)
        assert getattr(keyless.info, sdk).call_args.args[0] == OTHER

    @pytest.mark.parametrize("method, args", [(m, a) for m, a, _ in READS] + [(m, ()) for m in ADDRESS_FIRST])
    def test_without_an_address_every_read_raises_the_same_error(self, keyless, method, args):
        with pytest.raises(AddressRequiredException) as error:
            getattr(keyless, method)(*args)
        assert isinstance(error.value, ValueError) and isinstance(error.value, RuntimeError)  # as before

    def test_an_invalid_address_is_refused_before_any_request(self, keyless):
        with pytest.raises(ValueError, match="Invalid address format"):
            keyless.get_positions(address="0x1234")
        assert not keyless.info.user_state.called

    def test_the_clients_own_address_is_checked_too(self, mock_client):
        mock_client.public_address = "0xINVALID"
        with pytest.raises(ValueError, match="Invalid address format"):
            mock_client.get_order_status(1)
        assert not mock_client.info.query_order_by_oid.called

    @pytest.mark.parametrize("write", [
        lambda c: c.close("BTC"),
        lambda c: c.stop_loss("BTC", 0.001, 80000.0),
        lambda c: c.take_profit("BTC", 0.001, 90000.0),
    ])
    def test_writes_that_read_first_still_raise_the_write_error(self, keyless, write):
        with pytest.raises(RuntimeError, match="requires authentication") as error:
            write(keyless)
        assert not isinstance(error.value, AddressRequiredException)


    @pytest.mark.parametrize("function, args", [
        ("get_positions", ()), ("get_open_orders", ()), ("get_fills", ()),
        ("get_fills_by_time", (0, 1, None)), ("get_order_status", (1,)),
        ("get_spot_open_orders", ()), ("get_spot_fills", ()),
    ])
    def test_api_functions_pass_the_address_on(self, function, args):
        client = MagicMock()
        getattr(api, function)(*args, client=client, address=OTHER)
        assert getattr(client, function).call_args.kwargs["address"] == OTHER


class TestApiMatchesTheClient:
    """Each api function takes every parameter of the client method it calls, with the same
    default: a parameter added to the client and forgotten in the api is a missing feature
    (api.buy lacked reduce_only, post_only and slippage)."""

    WRAPPERS = [name for name, function in inspect.getmembers(api, inspect.isfunction)
                if function.__module__ == api.__name__ and hasattr(HyperliquidClient, name)]
    DEPRECATED = {"stop_price", "take_profit_price"}  # old names of trigger_price

    @pytest.mark.parametrize("name", WRAPPERS)
    def test_every_parameter_of_the_client_method(self, name):
        wrapper = inspect.signature(getattr(api, name)).parameters
        method = inspect.signature(getattr(HyperliquidClient, name)).parameters
        for parameter in (p for p in method.values() if p.name != "self"):
            assert parameter.name in wrapper, f"api.{name} lacks {parameter.name}"
            if parameter.default is not inspect.Parameter.empty:
                assert wrapper[parameter.name].default == parameter.default, f"api.{name}: default of {parameter.name}"
        assert set(wrapper) - set(method) <= {"account", "client"} | self.DEPRECATED

    @pytest.mark.parametrize("name", ["buy", "sell", "close", "open_long_position"])
    def test_every_parameter_reaches_the_client(self, name):
        client = MagicMock()
        arguments = {"buy": dict(reduce_only=True, post_only=True, slippage=0.01, cloid="0x" + "1" * 32),
                     "sell": dict(reduce_only=True, post_only=True, slippage=0.01, cloid="0x" + "1" * 32),
                     "close": dict(slippage=0.01),
                     "open_long_position": dict(limit_price=80000.0, slippage=0.01)}[name]
        getattr(api, name)("BTC", *([] if name == "close" else [0.1]), client=client, **arguments)
        assert all(getattr(client, name).call_args.kwargs[k] == v for k, v in arguments.items())

    @pytest.mark.parametrize("name, old_name", [("stop_loss", "stop_price"), ("take_profit", "take_profit_price")])
    def test_the_old_name_of_trigger_price_still_works_with_a_warning(self, name, old_name):
        client = MagicMock()
        with pytest.warns(DeprecationWarning, match=old_name):
            getattr(api, name)("BTC", 0.1, client=client, **{old_name: 80000.0})
        assert getattr(client, name).call_args.args == ("BTC", 0.1, 80000.0, None)
        getattr(api, name)("BTC", 0.1, 79000.0, client=client)  # positional, as before
        assert getattr(client, name).call_args.args[2] == 79000.0
        with pytest.raises(InvalidArgumentException):
            getattr(api, name)("BTC", 0.1, 80000.0, client=client, **{old_name: 80000.0})


# ── Exceptions ─────────────────────────────────────────────────────────

_ORDER = SimpleNamespace(order_id="7", status="open")  # stands in for an Order


_SPECIAL_ARGUMENTS = {
    UnsupportedAccountModeException: ("portfolio_margin",),
    ActionOutcomeUnknownException: ("transfer to spot", "read timed out"),
    OrderOutcomeUnknownException: ("BTC", "0x" + "1" * 32, "read timed out"),
    ProtectionOrderException: ("stop_loss", {"entry": _ORDER}),
    ProtectionReplacementIncompleteException: (_ORDER, ["5", "6"], "were kept"),
    exceptions.PositionNotClosedException: ("BTC", SimpleNamespace(order_id="7", filled_size=Decimal("0.6")), Decimal("0.4")),
    exceptions.ClosePositionsIncompleteException: ({"BTC": _ORDER}, {"ETH": "rejected"}),  # exceptions compare by identity
    exceptions.MakerOrderIncompleteException: ("BTC", Decimal("0.0004"), Decimal("85000"), Decimal("0.0006")),
    exceptions.ActionRejectedException: ("BTC leverage update", "Invalid leverage value"),
    exceptions.OrderRejectedException: ("BTC order", "Order must have minimum value of $10."),
    exceptions.InsufficientMarginException: ("BTC order", "Insufficient margin to place order."),
    exceptions.OrdersNotCanceledException: ([{"symbol": "BTC", "order_id": 6, "reason": "x"}],),
}
ALL_EXCEPTIONS = [c for c in vars(exceptions).values()
                  if isinstance(c, type) and issubclass(c, exceptions.HyperliquidException)]


class TestExceptions:
    # Errors cross process boundaries (multiprocessing, task queues); a lost
    # OrderOutcomeUnknownException would hide "do not resend".
    @pytest.mark.parametrize("cls", ALL_EXCEPTIONS, ids=lambda c: c.__name__)
    def test_copies_keep_type_message_attributes_and_notes(self, cls):
        error = cls(*_SPECIAL_ARGUMENTS.get(cls, ()))
        error.add_note("while closing BTC")
        error.caller_tag = "bot-1"
        for copied in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
            assert type(copied) is cls and copied.args == error.args and str(copied) == str(error)
            assert vars(copied) == vars(error)

    def test_a_network_error_keeps_its_oserror_fields(self):
        error = NetworkException(5, "io error", "file")
        for copied in (pickle.loads(pickle.dumps(error)), copy.copy(error), copy.deepcopy(error)):
            assert (copied.errno, copied.strerror, copied.filename, str(copied)) == (
                5, "io error", "file", str(error))

    def test_copies_keep_the_message_and_share_only_when_shallow(self):
        order = SimpleNamespace(order_id="7", status="open")
        error = ProtectionReplacementIncompleteException(order, ["5"])
        message = str(error)
        order.order_id = "9"  # changed after raising: the copies still report the original
        assert str(pickle.loads(pickle.dumps(error))) == message
        assert copy.copy(error).old_order_ids is error.old_order_ids
        assert copy.deepcopy(error).old_order_ids is not error.old_order_ids


# ── Portfolio ──────────────────────────────────────────────────────────

class TestPortfolio:
    def test_get_portfolio(self, mock_client):
        mock_client.info.post.return_value = {"pnl": "1000.0", "volume": "50000.0"}

        result = mock_client.get_portfolio()
        assert result["pnl"] == "1000.0"

        mock_client.info.post.assert_any_call(
            "/info", {"type": "portfolio", "user": mock_client.public_address}
        )

    def test_spot_reads_report_network_errors_as_library_exceptions(self, mock_client):
        mock_client.info.spot_meta.side_effect = requests.ConnectionError("down")
        with pytest.raises(NetworkException):
            mock_client.get_spot_fills()

    def test_get_portfolio_network_errors_are_not_input_errors(self, mock_client):
        mock_client.info.post.side_effect = requests.ConnectionError("down")
        with pytest.raises(NetworkException) as error:
            mock_client.get_portfolio()
        assert not isinstance(error.value, ValueError)

    def test_get_portfolio_with_address(self, mock_client):
        mock_client.info.post.return_value = {}

        other = "0x" + "ab" * 20
        mock_client.get_portfolio(other)
        mock_client.info.post.assert_any_call("/info", {"type": "portfolio", "user": other})

    def test_get_portfolio_requires_auth_or_address(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient.__new__(HyperliquidClient)
            client.max_retries = 0
            client.retry_delay = 0
            client.base_url = "https://api.hyperliquid.xyz"
        with pytest.raises(RuntimeError):
            client.get_portfolio()
