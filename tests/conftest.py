import pytest
import time
import os
from decimal import Decimal
from fractrade_hl_simple.models import UserState, Position, Order, MarginSummary
from dotenv import load_dotenv

load_dotenv()


@pytest.fixture(autouse=True)
def load_env():
    """Ensure required env vars are set for all tests."""
    assert os.getenv("HYPERLIQUID_PUBLIC_ADDRESS") is not None, \
        "HYPERLIQUID_PUBLIC_ADDRESS must be set in .env"
    assert os.getenv("HYPERLIQUID_ENV") is not None, \
        "HYPERLIQUID_ENV must be set in .env"


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


# ── Rate limit protection for integration tests ─────────────────────

@pytest.fixture(autouse=True, scope="session")
def rate_limit_guard():
    """Small delay between test session setup to avoid 429s on client init."""
    yield
    time.sleep(1)


@pytest.fixture(autouse=True)
def rate_limit_between_integration_tests(request):
    """Add a short delay after tests that hit the real API."""
    yield
    # Only delay for integration test files
    if "integration" in request.node.nodeid or "test_client" in request.node.nodeid:
        time.sleep(0.5)
