import pytest
from decimal import Decimal
from fractrade_hl_simple.models import UserState, Position, Order, MarginSummary
import os
from dotenv import load_dotenv

@pytest.fixture
def sample_order():
    return Order(
        order_id="123",
        symbol="BTC",
        is_buy=True,
        size=Decimal("1.0"),
        order_type={
            "limit": {"price": Decimal("50000.0"), "post_only": False},
            "market": None,
            "trigger": None
        },
        status="open",
        created_at=1234567890
    )

@pytest.fixture(autouse=True)
def load_env():
    load_dotenv()
    # Ensure required env vars are set
    assert os.getenv("HYPERLIQUID_PUBLIC_ADDRESS") is not None
    assert os.getenv("HYPERLIQUID_ENV") is not None

# Add more shared fixtures as needed 