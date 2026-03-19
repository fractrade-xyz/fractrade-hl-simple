import pytest
from unittest.mock import patch, MagicMock
import requests
from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.exceptions import (
    RateLimitException,
    ServerErrorException,
    UnauthorizedException,
    ConfigurationException,
)


@pytest.fixture
def client_with_retry():
    """Create a client with retry enabled and short delays for fast tests."""
    with patch('fractrade_hl_simple.hyperliquid.Info'), \
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

    def test_retries_on_server_error(self, client_with_retry):
        fn = MagicMock(side_effect=[
            ServerErrorException(),
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
        with patch('fractrade_hl_simple.hyperliquid.Info'), \
             patch('fractrade_hl_simple.hyperliquid.Exchange'):
            client = HyperliquidClient(max_retries=0, retry_delay=0.01)
        fn = MagicMock(side_effect=requests.ConnectionError("fail"))
        with pytest.raises(requests.ConnectionError):
            client._with_retry(fn)
        fn.assert_called_once()
