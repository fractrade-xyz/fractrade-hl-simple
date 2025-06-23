import pytest
from unittest.mock import Mock, patch
from decimal import Decimal
from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount, get_user_state
from fractrade_hl_simple.models import UserState, Position, Order, MarginSummary
import os
from dotenv import load_dotenv

# Load test environment variables
load_dotenv()

@pytest.fixture
def mock_exchange():
    with patch('fractrade_hl_simple.hyperliquid.Exchange') as mock:
        yield mock

@pytest.fixture
def mock_info():
    with patch('fractrade_hl_simple.hyperliquid.Info') as mock:
        yield mock

@pytest.fixture
def client():
    return HyperliquidClient()  # Will use .env values automatically

@pytest.fixture
def sample_user_state():
    return {
        "address": "0x123",
        "margin_summary": {
            "account_value": "1000.0",
            "total_margin_used": "200.0",
            "total_ntl_pos": "500.0",
            "total_raw_usd": "1000.0"
        },
        "cross_margin_summary": {
            "account_value": "1000.0",
            "total_margin_used": "200.0",
            "total_ntl_pos": "500.0",
            "total_raw_usd": "1000.0"
        },
        "withdrawable": "800.0"
    }

class TestHyperliquidClient:
    def test_init(self):
        client = HyperliquidClient()
        assert client.is_authenticated()

    def test_get_user_state(self, client):
        state = client.get_user_state()
        assert isinstance(state, UserState)
        assert state.margin_summary is not None
        assert isinstance(state.margin_summary.account_value, Decimal)

    def test_get_perp_balance(self):
        client = HyperliquidClient()
        
        # Test with specific address
        address = "0xf967239debef10dbc78e9bbbb2d8a16b72a614eb"
        balance = client.get_perp_balance(address)
        
        # Basic validations
        assert balance is not None
        assert isinstance(balance, Decimal)
        assert balance >= 0
        
        # Test without address (should use public_address from env)
        balance_default = client.get_perp_balance()
        assert balance_default is not None
        assert isinstance(balance_default, Decimal)
        assert balance_default >= 0

def test_get_user_state():
    # Get test address from environment
    address = os.getenv("HYPERLIQUID_PUBLIC_ADDRESS")
    assert address is not None, "HYPERLIQUID_PUBLIC_ADDRESS must be set"
    
    # Get user state
    state = get_user_state(address)
    
    # Test basic structure
    assert state is not None
    assert hasattr(state, 'margin_summary')
    assert hasattr(state, 'cross_margin_summary')
    assert hasattr(state, 'asset_positions')
    assert hasattr(state, 'withdrawable')
    
    # Test margin summary
    assert isinstance(state.margin_summary.account_value, Decimal)
    assert isinstance(state.margin_summary.total_margin_used, Decimal)
    assert isinstance(state.margin_summary.total_ntl_pos, Decimal)
    assert isinstance(state.margin_summary.total_raw_usd, Decimal)
    
    # Test positions if any exist
    if state.asset_positions:
        pos = state.asset_positions[0].position
        assert isinstance(pos.coin, str)
        assert isinstance(pos.szi, Decimal)
        assert isinstance(pos.unrealized_pnl, Decimal)
        assert isinstance(pos.leverage.type, str)
        assert isinstance(pos.leverage.value, Decimal)
        assert pos.leverage.type in ["cross", "isolated"]
        
        # Test position properties
        assert isinstance(pos.is_long, bool)
        assert isinstance(pos.is_short, bool)
        assert (pos.is_long and not pos.is_short) or (pos.is_short and not pos.is_long)

def test_get_user_state_with_account():
    # Create account from environment
    account = HyperliquidAccount.from_env()
    
    # Get user state using account's public address
    state = get_user_state(account.public_address)
    
    # Basic validation
    assert state is not None
    assert isinstance(state.withdrawable, Decimal)
    assert isinstance(state.margin_summary.account_value, Decimal)

def test_get_user_state_invalid_address():
    # Test with invalid address
    with pytest.raises(ValueError, match="Invalid address"):  # More specific exception
        get_user_state("0xasdf")

def test_get_positions(client, mock_exchange, sample_user_state):
    mock_exchange.return_value.get_user_state.return_value = sample_user_state
    positions = client.get_positions()
    assert isinstance(positions, list)
    if positions:
        pos = positions[0]
        assert isinstance(pos, Position)

def test_get_funding_rates_integration():
    """Integration test for get_funding_rates - calls real API and checks major coins."""
    client = HyperliquidClient()
    
    # Get all funding rates
    funding_rates = client.get_funding_rates()
    
    # Basic validation
    assert isinstance(funding_rates, list)
    assert len(funding_rates) > 0
    
    # Check structure of each item
    for item in funding_rates:
        assert 'symbol' in item
        assert 'funding_rate' in item
        assert isinstance(item['symbol'], str)
        assert isinstance(item['funding_rate'], float)
    
    # Check that major coins have non-zero funding rates
    major_coins = ['BTC', 'ETH', 'SOL']
    found_major_coins = []
    
    for item in funding_rates:
        if item['symbol'] in major_coins:
            found_major_coins.append(item['symbol'])
            # Check that funding rate is not zero (should be some small number)
            assert item['funding_rate'] != 0.0, f"Funding rate for {item['symbol']} should not be zero"
            # Check that funding rate is reasonable (between -1% and +1%)
            assert -0.01 <= item['funding_rate'] <= 0.01, f"Funding rate for {item['symbol']} should be between -1% and +1%"
    
    # Should find at least some of the major coins
    assert len(found_major_coins) > 0, f"Should find at least some of the major coins: {major_coins}"
    
    # Test getting specific symbol funding rate
    btc_rate = client.get_funding_rates("BTC")
    assert isinstance(btc_rate, float)
    assert btc_rate != 0.0
    assert -0.01 <= btc_rate <= 0.01
    
    # Test getting non-existent symbol
    with pytest.raises(ValueError, match="Symbol XYZ not found in funding rates"):
        client.get_funding_rates("XYZ")


