"""
Integration tests for spot trading — real trades on Hyperliquid.

Requires authentication (.env with HYPERLIQUID_PRIVATE_KEY and HYPERLIQUID_PUBLIC_ADDRESS).
Uses FRAC token for testing with minimal position sizes.
"""
import pytest
import time
from decimal import Decimal
from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount


@pytest.fixture(scope="module")
def client():
    account = HyperliquidAccount.from_env()
    return HyperliquidClient(account=account)


# ── Spot price and data ───────────────────────────────────────────────

class TestSpotData:
    def test_get_spot_price(self, client):
        """Get spot price for FRAC."""
        price = client.get_spot_price("FRAC")
        assert isinstance(price, float)
        assert price > 0
        print(f"FRAC spot price: ${price:.6f}")

    def test_get_spot_price_not_found(self, client):
        """Unknown token raises ValueError."""
        with pytest.raises(ValueError, match="No spot pair found"):
            client.get_spot_price("DOESNOTEXIST999")

    def test_resolve_spot_pair(self, client):
        """Token name resolves to pair name."""
        assert client._resolve_spot_pair("FRAC") == "FRAC/USDC"
        assert client._resolve_spot_pair("HYPE") == "HYPE/USDC"
        assert client._resolve_spot_pair("PURR") == "PURR/USDC"

    def test_spot_balance(self, client):
        """Check spot balance has USDC."""
        balance = client.get_spot_balance(simple=False)
        assert "USDC" in balance.tokens
        usdc = float(balance.tokens["USDC"].amount)
        assert usdc > 0, "Need USDC on spot wallet for trading tests"
        print(f"Spot USDC: ${usdc:.2f}")


# ── Transfers ─────────────────────────────────────────────────────────

class TestTransfers:
    def test_transfer_to_perp_and_back(self, client):
        """Transfer USDC from spot to perp and back.

        Note: This test will fail with API wallets (transfers require main wallet key).
        """
        amount = 1.0

        try:
            result = client.transfer_to_perp(amount)
            assert result.get("status") == "ok"
            print(f"Transferred ${amount} to perp")

            time.sleep(2)

            result = client.transfer_to_spot(amount)
            assert result.get("status") == "ok"
            print(f"Transferred ${amount} back to spot")
        except ValueError as e:
            if "Must deposit before" in str(e) or "API wallet" in str(e).lower():
                pytest.skip("Transfer not supported with API wallets — requires main wallet key")
            raise

    def test_transfer_invalid_amount(self, client):
        """Negative amount raises ValueError."""
        with pytest.raises(ValueError, match="positive"):
            client.transfer_to_spot(-1.0)
        with pytest.raises(ValueError, match="positive"):
            client.transfer_to_perp(0)


# ── Spot market buy/sell ──────────────────────────────────────────────

class TestSpotMarketOrders:
    TOKEN = "FRAC"
    SIZE = 500.0  # 500 FRAC tokens, ~$12.50 (spot has $10 minimum order value)

    def test_market_buy_and_sell(self, client):
        """Market buy FRAC, verify balance, then sell."""
        token, size = self.TOKEN, self.SIZE

        try:
            # Get initial FRAC balance
            balance_before = client.get_spot_balance(simple=False)
            frac_before = float(balance_before.tokens.get("FRAC", Decimal(0)).amount) if "FRAC" in balance_before.tokens else 0

            # Market buy
            order = client.spot_buy(token, size)
            assert order.symbol == token
            assert order.status == "filled"
            assert order.is_buy is True
            print(f"Bought {token}: {order}")

            time.sleep(2)

            # Verify FRAC balance increased
            balance_after = client.get_spot_balance(simple=False)
            frac_after = float(balance_after.tokens["FRAC"].amount)
            assert frac_after > frac_before, f"FRAC balance should increase: {frac_before} -> {frac_after}"
            print(f"FRAC balance: {frac_before} -> {frac_after}")

            # Market sell the same amount
            order = client.spot_sell(token, size)
            assert order.symbol == token
            assert order.status == "filled"
            assert order.is_buy is False
            print(f"Sold {token}: {order}")

            time.sleep(2)

            # Verify FRAC balance decreased back
            balance_final = client.get_spot_balance(simple=False)
            frac_final = float(balance_final.tokens.get("FRAC", Decimal(0)).amount) if "FRAC" in balance_final.tokens else 0
            assert frac_final < frac_after
            print(f"FRAC balance after sell: {frac_final}")

        finally:
            self._cleanup(client, token)

    @staticmethod
    def _cleanup(client, token):
        try:
            client.spot_cancel_all_orders(token)
        except Exception as e:
            print(f"Cleanup: {e}")


# ── Spot limit orders ─────────────────────────────────────────────────

class TestSpotLimitOrders:
    TOKEN = "FRAC"
    SIZE = 500.0  # 500 FRAC tokens (spot has $10 minimum order value)

    def test_limit_buy_and_cancel(self, client):
        """Place limit buy below market, verify in open orders, cancel."""
        token, size = self.TOKEN, self.SIZE

        try:
            price = client.get_spot_price(token)
            limit_price = round(price * 0.80, 6)  # 20% below market — won't fill

            order = client.spot_buy(token, size, limit_price=limit_price)
            assert order.symbol == token
            assert order.status == "open"
            assert order.is_buy is True
            print(f"Limit buy placed: {order}")

            time.sleep(2)

            # Verify in open orders
            open_orders = client.get_spot_open_orders(token)
            found = next((o for o in open_orders if o.order_id == order.order_id), None)
            assert found is not None, "Limit order not found in open orders"
            assert found.is_buy is True
            assert found.symbol == token
            print(f"Found in open orders: {found}")

            # Cancel specific order
            cancelled = client.spot_cancel_order(order.order_id, token)
            assert cancelled is True
            print("Cancelled successfully")

            time.sleep(2)

            # Verify gone
            open_orders = client.get_spot_open_orders(token)
            found = next((o for o in open_orders if o.order_id == order.order_id), None)
            assert found is None, "Order should be cancelled"

        finally:
            self._cleanup(client, token)

    def test_limit_sell_and_cancel_all(self, client):
        """Place limit sell above market, cancel all."""
        token, size = self.TOKEN, self.SIZE

        try:
            # Need to own FRAC first — buy some
            buy_order = client.spot_buy(token, size)
            time.sleep(2)

            # Use the filled size for the sell (may differ slightly from requested)
            sell_size = float(buy_order.filled_size) if buy_order.filled_size else size

            price = client.get_spot_price(token)
            limit_price = round(price * 2.0, 6)  # 2x above market — won't fill

            order = client.spot_sell(token, sell_size, limit_price=limit_price)
            assert order.status == "open"
            assert order.is_buy is False
            print(f"Limit sell placed: {order}")

            time.sleep(2)

            # Verify in open orders
            open_orders = client.get_spot_open_orders(token)
            assert len(open_orders) >= 1
            print(f"Open orders: {len(open_orders)}")

            # Cancel all
            client.spot_cancel_all_orders(token)
            time.sleep(2)

            open_orders = client.get_spot_open_orders(token)
            assert len(open_orders) == 0, "All orders should be cancelled"
            print("All orders cancelled")

            # Sell the FRAC we bought
            client.spot_sell(token, sell_size)

        finally:
            self._cleanup(client, token)

    @staticmethod
    def _cleanup(client, token):
        try:
            client.spot_cancel_all_orders(token)
        except Exception as e:
            print(f"Cleanup: {e}")


# ── Multiple limit orders ─────────────────────────────────────────────

class TestSpotMultipleOrders:
    TOKEN = "FRAC"
    SIZE = 500.0  # 500 FRAC tokens (spot has $10 minimum order value)

    def test_multiple_limit_buys(self, client):
        """Place multiple limit buys at different prices, verify all, cancel all."""
        token, size = self.TOKEN, self.SIZE

        try:
            price = client.get_spot_price(token)

            # Place 3 limit orders at different prices
            orders = []
            for pct in [0.70, 0.75, 0.80]:
                lp = round(price * pct, 6)
                order = client.spot_buy(token, size, limit_price=lp)
                assert order.status == "open"
                orders.append(order)
                print(f"Placed limit buy @ ${lp:.6f}: {order.order_id}")

            time.sleep(2)

            # Verify all in open orders
            open_orders = client.get_spot_open_orders(token)
            order_ids = {o.order_id for o in open_orders}
            for o in orders:
                assert o.order_id in order_ids, f"Order {o.order_id} not found"
            print(f"All {len(orders)} orders found in open orders")

            # Cancel all
            client.spot_cancel_all_orders(token)
            time.sleep(2)

            open_orders = client.get_spot_open_orders(token)
            assert len(open_orders) == 0
            print("All cancelled")

        finally:
            try:
                client.spot_cancel_all_orders(token)
            except Exception:
                pass
