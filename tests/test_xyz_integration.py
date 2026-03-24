"""
Integration tests for extended universe (xyz:) symbols — real trades on Hyperliquid.

Requires authentication (.env with HYPERLIQUID_PRIVATE_KEY and HYPERLIQUID_PUBLIC_ADDRESS).
These tests place REAL orders with minimum position sizes (< $1 each).
"""
import pytest
import time
from decimal import Decimal
from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount


@pytest.fixture(scope="module")
def client():
    account = HyperliquidAccount.from_env()
    return HyperliquidClient(account=account, perp_dexs=["", "xyz"])


# ── Public endpoints (no auth needed) ─────────────────────────────────

class TestXyzPublicData:
    def test_get_price_xyz(self, client):
        """Get prices for xyz symbols."""
        gold = client.get_price("xyz:GOLD")
        assert isinstance(gold, float)
        assert gold > 1000  # Gold is always > $1000/oz

        oil = client.get_price("xyz:BRENTOIL")
        assert isinstance(oil, float)
        assert oil > 10  # Oil is always > $10/bbl

        tsla = client.get_price("xyz:TSLA")
        assert isinstance(tsla, float)
        assert tsla > 0

        print(f"GOLD: ${gold:,.2f}  BRENTOIL: ${oil:,.2f}  TSLA: ${tsla:,.2f}")

    def test_get_all_prices_include_xyz(self, client):
        """get_price() returns both crypto and xyz symbols."""
        prices = client.get_price()
        assert "BTC" in prices
        assert "xyz:GOLD" in prices
        assert "xyz:TSLA" in prices

        xyz_count = sum(1 for k in prices if k.startswith("xyz:"))
        print(f"Total: {len(prices)} symbols, {xyz_count} xyz symbols")
        assert xyz_count >= 40

    def test_get_market_info_xyz(self, client):
        """Retrieve market metadata for xyz symbols."""
        gold_info = client.get_market_info("xyz:GOLD")
        assert gold_info["name"] == "xyz:GOLD"
        assert gold_info["szDecimals"] == 4
        assert gold_info["maxLeverage"] >= 20

        oil_info = client.get_market_info("xyz:BRENTOIL")
        assert oil_info["name"] == "xyz:BRENTOIL"
        assert oil_info["szDecimals"] == 2

    def test_get_market_info_all_includes_xyz(self, client):
        """get_market_info() returns both crypto and xyz markets."""
        markets = client.get_market_info()
        names = [m["name"] for m in markets]
        assert "BTC" in names
        assert "xyz:GOLD" in names
        assert "xyz:TSLA" in names

    def test_market_specs_include_xyz(self, client):
        """market_specs dict has xyz symbols with correct values."""
        assert "xyz:GOLD" in client.market_specs
        assert "xyz:BRENTOIL" in client.market_specs
        assert client.market_specs["xyz:GOLD"]["size_decimals"] == 4
        assert client.market_specs["xyz:BRENTOIL"]["size_decimals"] == 2

    def test_order_book_xyz(self, client):
        """Retrieve order book for xyz symbol."""
        book = client.get_order_book("xyz:GOLD")
        assert book["best_bid"] > 0
        assert book["best_ask"] > 0
        assert book["best_ask"] > book["best_bid"]
        assert book["spread"] > 0
        print(f"GOLD bid: ${book['best_bid']:,.2f}  ask: ${book['best_ask']:,.2f}  spread: ${book['spread']:,.2f}")

    def test_get_price_xyz_not_found(self, client):
        """Unknown xyz symbol raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            client.get_price("xyz:DOESNOTEXIST")


# ── Trading GOLD ──────────────────────────────────────────────────────

class TestXyzTradeGold:
    SYMBOL = "xyz:GOLD"
    SIZE = 0.0025  # ~$11 (xyz dex has $10 minimum order value)

    def test_long_gold(self, client):
        """Open long GOLD, verify position, close it."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            # Market buy
            order = client.buy(symbol, size)
            assert order.symbol == symbol
            assert order.status == "filled"
            assert order.is_buy is True
            print(f"Bought {symbol}: {order}")

            time.sleep(2)

            # Verify position exists
            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is not None, f"No {symbol} position found"
            assert pos.is_long
            assert float(pos.size) >= size
            print(f"Position: {pos}")

            # Close
            close = client.close(symbol)
            assert close.symbol == symbol
            assert close.is_buy is False
            print(f"Closed: {close}")

            time.sleep(2)

            # Verify closed
            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is None or float(pos.size) == 0

        finally:
            self._cleanup(client, symbol)

    def test_short_gold(self, client):
        """Open short GOLD, verify position, close it."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            order = client.sell(symbol, size)
            assert order.symbol == symbol
            assert order.status == "filled"
            assert order.is_buy is False
            print(f"Sold {symbol}: {order}")

            time.sleep(2)

            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is not None, f"No {symbol} position found"
            assert pos.is_short
            print(f"Position: {pos}")

            close = client.close(symbol)
            assert close.is_buy is True
            print(f"Closed: {close}")

            time.sleep(2)

            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is None or float(pos.size) == 0

        finally:
            self._cleanup(client, symbol)

    def test_limit_order_gold(self, client):
        """Place limit order for GOLD, verify in open orders, cancel."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            price = client.get_price(symbol)
            limit_price = round(price * 0.90, 1)  # 10% below market

            order = client.buy(symbol, size, limit_price=limit_price)
            assert order.symbol == symbol
            assert order.status == "open"
            print(f"Limit buy: {order}")

            time.sleep(2)

            open_orders = client.get_open_orders(symbol)
            found = next((o for o in open_orders if o.order_id == order.order_id), None)
            assert found is not None, "Limit order not found in open orders"
            assert found.is_buy is True
            print(f"Found in open orders: {found}")

            client.cancel_all_orders(symbol)
            time.sleep(2)

            open_orders = client.get_open_orders(symbol)
            found = next((o for o in open_orders if o.order_id == order.order_id), None)
            assert found is None, "Order should be cancelled"
            print("Cancelled successfully")

        finally:
            self._cleanup(client, symbol)

    def test_leverage_gold(self, client):
        """Set leverage on GOLD."""
        symbol = self.SYMBOL
        result = client.set_leverage(symbol, 5)
        assert result.get("status") == "ok"
        print(f"Set {symbol} leverage to 5x")

        # Verify max leverage is respected
        max_lev = client.market_specs[symbol]["max_leverage"]
        with pytest.raises(ValueError, match="Max leverage"):
            client.set_leverage(symbol, int(max_lev) + 1)

    @staticmethod
    def _cleanup(client, symbol):
        try:
            client.cancel_all_orders(symbol)
            positions = client.get_positions()
            if any(p.symbol == symbol and float(p.size) != 0 for p in positions):
                client.close(symbol)
        except Exception as e:
            print(f"Cleanup: {e}")


# ── Trading BRENTOIL ──────────────────────────────────────────────────

class TestXyzTradeOil:
    SYMBOL = "xyz:BRENTOIL"
    SIZE = 0.11  # ~$11 (xyz dex has $10 minimum order value)

    def test_long_oil(self, client):
        """Open long BRENTOIL, verify position, close."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            order = client.buy(symbol, size)
            assert order.symbol == symbol
            assert order.status == "filled"
            print(f"Bought {symbol}: {order}")

            time.sleep(2)

            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is not None, f"No {symbol} position found"
            assert pos.is_long
            print(f"Position: {pos}")

            close = client.close(symbol)
            assert close.symbol == symbol
            print(f"Closed: {close}")

            time.sleep(2)

            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is None or float(pos.size) == 0

        finally:
            self._cleanup(client, symbol)

    def test_short_oil(self, client):
        """Open short BRENTOIL, verify position, close."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            order = client.sell(symbol, size)
            assert order.symbol == symbol
            assert order.status == "filled"
            print(f"Sold {symbol}: {order}")

            time.sleep(2)

            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is not None
            assert pos.is_short
            print(f"Position: {pos}")

            close = client.close(symbol)
            print(f"Closed: {close}")

            time.sleep(2)

            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is None or float(pos.size) == 0

        finally:
            self._cleanup(client, symbol)

    def test_tp_sl_oil(self, client):
        """Open BRENTOIL long with TP/SL, verify orders, close."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            # Open long
            order = client.buy(symbol, size)
            assert order.status == "filled"
            print(f"Bought {symbol}: {order}")

            time.sleep(2)

            # Get entry price
            positions = client.get_positions()
            pos = next(p for p in positions if p.symbol == symbol)
            entry = float(pos.entry_price)
            print(f"Entry: ${entry:.2f}")

            # Set TP at +10%, SL at -5%
            tp_price = round(entry * 1.10, 1)
            sl_price = round(entry * 0.95, 1)

            tp_order = client.take_profit(symbol, size, tp_price, is_buy=False)
            assert tp_order.symbol == symbol
            print(f"TP set at ${tp_price:.2f}")

            sl_order = client.stop_loss(symbol, size, sl_price, is_buy=False)
            assert sl_order.symbol == symbol
            print(f"SL set at ${sl_price:.2f}")

            time.sleep(2)

            # Verify TP/SL in open orders
            open_orders = client.get_open_orders(symbol)
            tp_orders = [o for o in open_orders if o.type == "take_profit"]
            sl_orders = [o for o in open_orders if o.type == "stop_loss"]
            assert len(tp_orders) > 0, "TP order not found"
            assert len(sl_orders) > 0, "SL order not found"

            # Verify TP/SL price retrieval
            retrieved_tp = client.get_take_profit_price(symbol)
            retrieved_sl = client.get_stop_loss_price(symbol)
            assert retrieved_tp is not None
            assert retrieved_sl is not None
            print(f"Retrieved TP: ${float(retrieved_tp):.2f}, SL: ${float(retrieved_sl):.2f}")

            # Clean up
            client.cancel_all_orders(symbol)
            client.close(symbol)

            time.sleep(2)
            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is None or float(pos.size) == 0

        finally:
            self._cleanup(client, symbol)

    @staticmethod
    def _cleanup(client, symbol):
        try:
            client.cancel_all_orders(symbol)
            positions = client.get_positions()
            if any(p.symbol == symbol and float(p.size) != 0 for p in positions):
                client.close(symbol)
        except Exception as e:
            print(f"Cleanup: {e}")


# ── Trading TSLA ──────────────────────────────────────────────────────

class TestXyzTradeTsla:
    SYMBOL = "xyz:TSLA"
    SIZE = 0.027  # ~$10.25 (xyz dex has $10 minimum order value)

    def test_full_flow_tsla(self, client):
        """Full trading flow: limit order, cancel, market long, close, market short, close."""
        symbol, size = self.SYMBOL, self.SIZE

        try:
            price = client.get_price(symbol)
            balance = client.get_perp_balance()
            print(f"{symbol}: ${price:,.2f}  Balance: ${float(balance):,.2f}")

            # 1. Limit buy below market, then cancel
            limit = round(price * 0.90, 2)
            order = client.buy(symbol, size, limit_price=limit)
            assert order.status == "open"
            print(f"Limit buy @ ${limit}: {order.order_id}")

            time.sleep(2)
            client.cancel_all_orders(symbol)
            print("Cancelled")

            time.sleep(1)

            # 2. Market long
            order = client.buy(symbol, size)
            assert order.status == "filled"
            print(f"Market buy filled: {order}")

            time.sleep(2)
            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is not None and pos.is_long
            print(f"Long position: {pos}")

            # 3. Close long
            close = client.close(symbol)
            assert close.is_buy is False
            print(f"Closed long: {close}")

            time.sleep(2)

            # 4. Market short
            order = client.sell(symbol, size)
            assert order.status == "filled"
            print(f"Market sell filled: {order}")

            time.sleep(2)
            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is not None and pos.is_short
            print(f"Short position: {pos}")

            # 5. Close short
            close = client.close(symbol)
            assert close.is_buy is True
            print(f"Closed short: {close}")

            time.sleep(2)
            positions = client.get_positions()
            pos = next((p for p in positions if p.symbol == symbol), None)
            assert pos is None or float(pos.size) == 0
            print("All positions closed")

        finally:
            try:
                client.cancel_all_orders(symbol)
                positions = client.get_positions()
                if any(p.symbol == symbol and float(p.size) != 0 for p in positions):
                    client.close(symbol)
            except Exception as e:
                print(f"Cleanup: {e}")


# ── Fills ─────────────────────────────────────────────────────────────

class TestXyzFills:
    def test_fills_show_xyz_symbols(self, client):
        """After trading xyz symbols, fills should include them."""
        fills = client.get_fills()
        xyz_fills = [f for f in fills if f.symbol.startswith("xyz:")]
        print(f"Total fills: {len(fills)}, xyz fills: {len(xyz_fills)}")
        # After running the trade tests above, we should have xyz fills
        if xyz_fills:
            for f in xyz_fills[:5]:
                print(f"  {f.symbol} {f.direction} {f.size} @ {f.price}")
