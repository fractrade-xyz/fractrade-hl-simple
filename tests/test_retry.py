import pytest
from unittest.mock import patch, MagicMock
import requests
import re
import threading

from hyperliquid.utils.error import ClientError, ServerError
from fractrade_hl_simple import exceptions
from fractrade_hl_simple.hyperliquid import HyperliquidClient, _signer_state
from fractrade_hl_simple.models import HyperliquidAccount
from fractrade_hl_simple.exceptions import (
    ClientErrorException,
    NetworkException,
    RateLimitException,
    ServerErrorException,
    TimeoutException,
    UnauthorizedException,
    ConfigurationException,
)

# The SDK's Info, answering metaAndAssetCtxs as Hyperliquid does: a client is not created
# without its markets.
INFO_ANSWERS = {"return_value.post.return_value": [{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]},
                                                   [{}]]}



@pytest.fixture
def client_with_retry():
    """Create a client with retry enabled and short delays for fast tests."""
    with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
         patch('fractrade_hl_simple.hyperliquid.Exchange'):
        client = HyperliquidClient(max_retries=3, retry_delay=0.01)
        return client


class TestRetryLogic:
    def test_succeeds_on_first_try(self, client_with_retry):
        fn = MagicMock(return_value="ok")
        result = client_with_retry._with_retry(fn, "arg1", key="val")
        assert result == "ok"
        fn.assert_called_once_with("arg1", key="val")

    def test_retries_on_connection_error(self, client_with_retry):
        fn = MagicMock(side_effect=[
            requests.ConnectionError("conn failed"),
            requests.ConnectionError("conn failed"),
            "success",
        ])
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            result = client_with_retry._with_retry(fn)
        assert result == "success"
        assert fn.call_count == 3

    def test_retries_on_timeout(self, client_with_retry):
        fn = MagicMock(side_effect=[
            requests.Timeout("timeout"),
            "success",
        ])
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            result = client_with_retry._with_retry(fn)
        assert result == "success"
        assert fn.call_count == 2

    def test_retries_on_rate_limit(self, client_with_retry):
        fn = MagicMock(side_effect=[
            RateLimitException(),
            "success",
        ])
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            result = client_with_retry._with_retry(fn)
        assert result == "success"

    # The SDK raises its own ServerError for HTTP 5xx. Reads retry it.
    def test_retries_on_sdk_server_error(self, client_with_retry):
        fn = MagicMock(side_effect=[
            ServerError(502, "Bad Gateway"),
            "success",
        ])
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            result = client_with_retry._with_retry(fn)
        assert result == "success"

    def test_raises_after_max_retries(self, client_with_retry):
        fn = MagicMock(side_effect=requests.ConnectionError("conn failed"))
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'):
            with pytest.raises(requests.ConnectionError):
                client_with_retry._with_retry(fn)
        assert fn.call_count == 4  # 1 initial + 3 retries

    def test_no_retry_on_value_error(self, client_with_retry):
        fn = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError, match="bad input"):
            client_with_retry._with_retry(fn)
        fn.assert_called_once()

    def test_no_retry_on_unauthorized(self, client_with_retry):
        fn = MagicMock(side_effect=UnauthorizedException())
        with pytest.raises(UnauthorizedException):
            client_with_retry._with_retry(fn)
        fn.assert_called_once()

    def test_no_retry_on_configuration_error(self, client_with_retry):
        fn = MagicMock(side_effect=ConfigurationException("bad config"))
        with pytest.raises(ConfigurationException):
            client_with_retry._with_retry(fn)
        fn.assert_called_once()

    def test_no_retry_on_runtime_error(self, client_with_retry):
        fn = MagicMock(side_effect=RuntimeError("not auth"))
        with pytest.raises(RuntimeError):
            client_with_retry._with_retry(fn)
        fn.assert_called_once()

    def test_exponential_backoff(self, client_with_retry):
        fn = MagicMock(side_effect=[
            requests.ConnectionError("fail"),
            requests.ConnectionError("fail"),
            requests.ConnectionError("fail"),
            requests.ConnectionError("fail"),  # final raise
        ])
        with patch('fractrade_hl_simple.hyperliquid.time.sleep') as mock_sleep:
            with pytest.raises(requests.ConnectionError):
                client_with_retry._with_retry(fn)
        # Verify exponential backoff: 0.01 * 2^0, 0.01 * 2^1, 0.01 * 2^2
        delays = [call.args[0] for call in mock_sleep.call_args_list]
        assert len(delays) == 3
        assert abs(delays[0] - 0.01) < 0.001
        assert abs(delays[1] - 0.02) < 0.001
        assert abs(delays[2] - 0.04) < 0.001

    def test_retry_disabled_with_zero(self):
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient(max_retries=0, retry_delay=0.01)
        fn = MagicMock(side_effect=requests.ConnectionError("fail"))
        with pytest.raises(requests.ConnectionError):
            client._with_retry(fn)
        fn.assert_called_once()


class TestErrorContract:
    """After its retries the library raises its own exceptions, each also the type raised
    before, with the original as __cause__."""

    @pytest.mark.parametrize("raised, expected, old_type", [
        (requests.ConnectionError("refused"), NetworkException, requests.ConnectionError),
        (requests.Timeout("read timed out"), TimeoutException, requests.Timeout),
        (ServerError(503, "<html>Service Unavailable</html>"), ServerErrorException, ServerError),
    ], ids=["connection", "timeout", "server-error"])
    def test_failures_after_the_retries_are_library_exceptions(self, client_with_retry, raised, expected, old_type):
        fn = MagicMock(side_effect=raised)
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(expected) as error:
            client_with_retry._with_retry(fn)
        assert fn.call_count == 4
        assert isinstance(error.value, old_type) and error.value.__cause__ is raised and error.value.retryable

    def test_a_server_error_keeps_its_status(self, client_with_retry):
        fn = MagicMock(side_effect=ServerError(503, "<html>Service Unavailable</html>"))
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(ServerErrorException) as error:
            client_with_retry._with_retry(fn)
        assert error.value.status_code == 503

    def test_a_body_that_is_not_json_is_a_server_error_and_retried(self, client_with_retry):
        # The SDK returns this dict instead of raising (SDK api.py).
        fn = MagicMock(return_value={"error": "Could not parse JSON: <html>Bad Gateway</html>"})
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(ServerErrorException) as error:
            client_with_retry._with_retry(fn)
        assert fn.call_count == 4 and error.value.status_code == 200

    def test_other_transport_errors_are_not_retried(self, client_with_retry):
        fn = MagicMock(side_effect=requests.exceptions.ChunkedEncodingError("connection broken"))
        with pytest.raises(NetworkException):
            client_with_retry._with_retry(fn)
        fn.assert_called_once()

    def test_a_rate_limit_keeps_the_sdk_error_as_cause(self, client_with_retry):
        limited = ClientError(429, None, "rate limited", {})
        fn = MagicMock(side_effect=limited)
        with patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(RateLimitException) as error:
            client_with_retry._with_retry(fn)
        assert error.value.__cause__ is limited and error.value.retryable

    def test_creating_a_client_retries_and_reports_network_failures(self):
        # The SDK's Info makes requests while it is created.
        with patch('fractrade_hl_simple.hyperliquid.Info', side_effect=requests.ConnectionError("down")) as info_cls, \
             patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(NetworkException):
            HyperliquidClient(max_retries=2, retry_delay=0, trust_env=False, cache_market_specs=False)
        assert info_cls.call_count == 3

    @pytest.mark.parametrize("cls", [c for c in vars(exceptions).values()
                                     if isinstance(c, type) and issubclass(c, exceptions.HyperliquidException)],
                             ids=lambda c: c.__name__)
    def test_every_exception_has_a_code_and_retryable(self, cls):
        assert re.fullmatch(r"[A-Z][A-Z_]+", cls.code) and isinstance(cls.retryable, bool)

    def test_another_4xx_is_a_rejection_with_the_sdk_fields(self, client_with_retry):
        rejected = ClientError(422, None, "Failed to deserialize the JSON body", {"x": "1"})
        fn = MagicMock(side_effect=rejected)
        with pytest.raises(ClientErrorException) as error:
            client_with_retry._with_retry(fn)
        fn.assert_called_once()  # not retried
        assert isinstance(error.value, ClientError) and error.value.__cause__ is rejected
        assert (error.value.status_code, error.value.error_message) == (422, "Failed to deserialize the JSON body")
        assert not error.value.retryable


class TestStartup:
    """Creating a client: its requests are retried and reported like any other."""

    def test_a_body_that_is_not_json_at_startup_is_a_server_error(self):
        # The real SDK Info, over a mocked HTTP layer: the SDK would turn the body into
        # {"error": "Could not parse JSON: ..."} and then fail on it while it starts.
        response = MagicMock(status_code=200, text="<html>Bad Gateway</html>")
        response.json.side_effect = ValueError("Expecting value")
        with patch("requests.Session.post", return_value=response) as post, \
             patch('fractrade_hl_simple.hyperliquid.time.sleep'), pytest.raises(ServerErrorException):
            HyperliquidClient(max_retries=1, retry_delay=0, trust_env=False, cache_market_specs=False)
        assert post.call_count == 2  # the first request of each attempt fails

    def test_the_metadata_the_sdk_fetched_while_starting_is_not_requested_again(self):
        # The real SDK Info over a mocked HTTP layer. The default dex's meta and the spot meta
        # were requested a second time, only to be kept for the Exchange.
        meta = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}
        answers = {"spotMeta": {"universe": [], "tokens": []}, "perpDexs": [None], "meta": meta,
                   "metaAndAssetCtxs": [meta, [{}]]}
        sent = []

        def post(session, url, json=None, **kwargs):
            sent.append(json["type"])
            return MagicMock(status_code=200, **{"json.return_value": answers[json["type"]]})

        with patch("requests.Session.post", autospec=True, side_effect=post):
            client = HyperliquidClient(trust_env=False, cache_market_specs=False)
            assert sorted(sent) == ["meta", "metaAndAssetCtxs", "perpDexs", "spotMeta"]
            assert (client._meta, client._spot_meta) == (meta, answers["spotMeta"])
            sent.clear()
            client.refresh_market_specs()
            assert sorted(sent) == ["meta", "metaAndAssetCtxs", "perpDexs", "spotMeta"]

    def test_the_exchange_starts_without_requests_and_signs_with_the_clients_info(self):
        account = HyperliquidAccount(private_key="0x" + "11" * 32,
                                     public_address="0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A")
        with patch('fractrade_hl_simple.hyperliquid.Info', **INFO_ANSWERS), \
             patch("requests.Session.post", side_effect=AssertionError("no request expected")):
            client = HyperliquidClient(account=account, trust_env=False, cache_market_specs=False)
        assert client.exchange.info is client.info

    def test_the_proxy_is_used_from_the_first_startup_request(self):
        # The SDK's Info sends requests while it is created, before a proxy could be set on it.
        seen = []

        def post(session, *args, **kwargs):
            seen.append(dict(session.proxies))
            raise requests.ConnectionError("stop after the first request")

        proxy = {"https": "http://proxy.example:8080"}
        with patch("requests.Session.post", autospec=True, side_effect=post), pytest.raises(NetworkException):
            HyperliquidClient(max_retries=0, trust_env=False, cache_market_specs=False, proxy=proxy)
        assert seen == [proxy]


class TestSignedActions:
    @pytest.mark.parametrize("idempotent", [False, True], ids=["order", "cancel"])
    def test_each_attempt_holds_the_signers_lock_but_not_the_wait_before_a_retry(self, client_with_retry, idempotent):
        # The lock lets orders and cancels of a key wait while a stop loss replacement runs.
        # A rate-limited order slept its backoff (up to 1 + 2 + 4 s) holding it, so everything
        # else of the key waited; a cancel did not take it at all, so it could cancel the new
        # stop loss in the middle of a replacement.
        client = client_with_retry
        client.exchange_account = MagicMock(address="0x" + "12" * 20)
        lock = _signer_state(client.exchange_account.address).operation_lock
        seen = []

        def free_for_another_thread():
            result = []

            def try_lock():
                result.append(lock.acquire(blocking=False))
                if result[0]:
                    lock.release()

            other = threading.Thread(target=try_lock)
            other.start()
            other.join()
            return result[0]

        def attempt():
            seen.append(("attempt", free_for_another_thread()))
            if len(seen) == 1:
                raise ClientError(429, None, "Too many requests", None)
            return {"status": "ok"}

        def sleep(seconds):
            if seconds >= client.retry_delay:  # the wait before the retry, not the nonce's
                seen.append(("wait", free_for_another_thread()))

        with patch("fractrade_hl_simple.hyperliquid.time.sleep", side_effect=sleep):
            assert client._signed("action", attempt, idempotent=idempotent) == {"status": "ok"}
        assert seen == [("attempt", False), ("wait", True), ("attempt", False)]
