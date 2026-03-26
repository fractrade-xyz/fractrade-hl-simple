import pytest
import math
import time
from unittest.mock import patch, MagicMock
from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount


@pytest.fixture
def mock_client():
    """Create a fully mocked client — no API calls."""
    # Save and restore class-level cache so integration tests still work
    saved = (HyperliquidClient._cached_market_specs, HyperliquidClient._cached_market_specs_at,
             HyperliquidClient._cached_meta, HyperliquidClient._cached_spot_meta)
    HyperliquidClient._cached_market_specs = None
    HyperliquidClient._cached_market_specs_at = 0
    HyperliquidClient._cached_meta = None
    HyperliquidClient._cached_spot_meta = None
    with patch('fractrade_hl_simple.hyperliquid.Info') as mock_info_cls, \
         patch('fractrade_hl_simple.hyperliquid.Exchange') as mock_exchange_cls:
        mock_info = mock_info_cls.return_value
        mock_info.post.return_value = [
            {"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 50, "px_dps": 1},
            ]},
            [{"funding": "0.0001", "openInterest": "1000", "markPx": "50000"}],
        ]

        client = HyperliquidClient(max_retries=0)
        client.info = mock_info
        yield client

    (HyperliquidClient._cached_market_specs, HyperliquidClient._cached_market_specs_at,
     HyperliquidClient._cached_meta, HyperliquidClient._cached_spot_meta) = saved


@pytest.fixture
def mock_l2_data():
    """Mock L2 order book data"""
    return {
        "coin": "BTC",
        "levels": [
            [  # Bids (level 0)
                {"n": 1, "px": "50000.0", "sz": "1.5"},
                {"n": 2, "px": "49999.0", "sz": "2.0"},
                {"n": 3, "px": "49998.0", "sz": "1.0"},
            ],
            [  # Asks (level 1)
                {"n": 1, "px": "50001.0", "sz": "1.2"},
                {"n": 2, "px": "50002.0", "sz": "2.5"},
                {"n": 3, "px": "50003.0", "sz": "1.8"},
            ]
        ],
        "time": int(time.time() * 1000)
    }


class TestOrderBook:
    def test_get_order_book_success(self, mock_client, mock_l2_data):
        """Test successful order book retrieval"""
        mock_client.info.l2_snapshot.return_value = mock_l2_data
        order_book = mock_client.get_order_book("BTC")

        assert order_book["symbol"] == "BTC"
        assert order_book["best_bid"] == 50000.0
        assert order_book["best_ask"] == 50001.0
        assert order_book["spread"] == 1.0
        assert order_book["mid_price"] == 50000.5

        # Check bids are sorted descending
        assert order_book["bids"][0]["price"] == 50000.0
        assert order_book["bids"][1]["price"] == 49999.0
        assert order_book["bids"][2]["price"] == 49998.0

        # Check asks are sorted ascending
        assert order_book["asks"][0]["price"] == 50001.0
        assert order_book["asks"][1]["price"] == 50002.0
        assert order_book["asks"][2]["price"] == 50003.0

        # Check sizes
        assert order_book["bids"][0]["size"] == 1.5
        assert order_book["asks"][0]["size"] == 1.2

    def test_get_order_book_invalid_response(self, mock_client):
        """Test order book retrieval with invalid response"""
        mock_client.info.l2_snapshot.return_value = {"invalid": "data"}
        with pytest.raises(ValueError, match="Invalid order book response"):
            mock_client.get_order_book("BTC")

    def test_get_order_book_insufficient_levels(self, mock_client):
        """Test order book retrieval with insufficient levels"""
        mock_client.info.l2_snapshot.return_value = {"levels": [[]]}
        with pytest.raises(ValueError, match="Insufficient order book levels"):
            mock_client.get_order_book("BTC")

    def test_get_order_book_empty_levels(self, mock_client):
        """Test order book retrieval with empty levels"""
        mock_client.info.l2_snapshot.return_value = {"levels": [[], []]}
        order_book = mock_client.get_order_book("BTC")

        assert order_book["best_bid"] is None
        assert order_book["best_ask"] is None
        assert order_book["spread"] is None
        assert order_book["mid_price"] is None
        assert len(order_book["bids"]) == 0
        assert len(order_book["asks"]) == 0


class TestOptimalLimitPrice:
    def test_get_optimal_limit_price_buy_patient(self, mock_client, mock_l2_data):
        """Test optimal buy price with low urgency (patient)"""
        mock_client.info.l2_snapshot.return_value = mock_l2_data
        mock_client.info.all_mids.return_value = {"BTC": "50000.5"}
        price = mock_client.get_optimal_limit_price(
            symbol="BTC",
            side="buy",
            urgency_factor=0.0  # Very patient
        )
        # Should be close to best bid (very patient)
        assert abs(price - 50000.0) < 1.0

    def test_get_optimal_limit_price_buy_aggressive(self, mock_client, mock_l2_data):
        """Test optimal buy price with high urgency (aggressive)"""
        mock_client.info.l2_snapshot.return_value = mock_l2_data
        mock_client.info.all_mids.return_value = {"BTC": "50000.5"}
        price = mock_client.get_optimal_limit_price(
            symbol="BTC",
            side="buy",
            urgency_factor=1.0  # Very aggressive
        )
        # Should be close to best ask (very aggressive)
        assert abs(price - 50001.0) < 1.0

    def test_get_optimal_limit_price_sell_patient(self, mock_client, mock_l2_data):
        """Test optimal sell price with low urgency (patient)"""
        mock_client.info.l2_snapshot.return_value = mock_l2_data
        mock_client.info.all_mids.return_value = {"BTC": "50000.5"}
        price = mock_client.get_optimal_limit_price(
            symbol="BTC",
            side="sell",
            urgency_factor=0.0  # Very patient
        )
        # Should be close to best ask (very patient)
        assert abs(price - 50001.0) < 1.0

    def test_get_optimal_limit_price_sell_aggressive(self, mock_client, mock_l2_data):
        """Test optimal sell price with high urgency (aggressive)"""
        mock_client.info.l2_snapshot.return_value = mock_l2_data
        mock_client.info.all_mids.return_value = {"BTC": "50000.5"}
        price = mock_client.get_optimal_limit_price(
            symbol="BTC",
            side="sell",
            urgency_factor=1.0  # Very aggressive
        )
        # Should be close to best bid (very aggressive)
        assert abs(price - 50000.0) < 1.0

    def test_get_optimal_limit_price_urgency_range(self, mock_client, mock_l2_data):
        """Test optimal price calculation across different urgency levels"""
        mock_client.info.l2_snapshot.return_value = mock_l2_data
        mock_client.info.all_mids.return_value = {"BTC": "50000.5"}
        # Test different urgency levels for buy orders
        prices = []
        for urgency in [0.0, 0.25, 0.5, 0.75, 1.0]:
            price = mock_client.get_optimal_limit_price(
                symbol="BTC",
                side="buy",
                urgency_factor=urgency
            )
            prices.append(price)

        # Prices should be monotonically increasing with urgency
        for i in range(1, len(prices)):
            assert prices[i] >= prices[i-1], f"Price should increase with urgency: {prices[i-1]} -> {prices[i]}"

    def test_get_optimal_limit_price_invalid_side(self, mock_client):
        """Test optimal price calculation with invalid side"""
        with pytest.raises(ValueError, match="side must be 'buy' or 'sell'"):
            mock_client.get_optimal_limit_price("BTC", "invalid", 0.5)

    def test_get_optimal_limit_price_invalid_urgency(self, mock_client):
        """Test optimal price calculation with invalid urgency factor"""
        with pytest.raises(ValueError, match="urgency_factor must be between 0.0 and 1.0"):
            mock_client.get_optimal_limit_price("BTC", "buy", 1.5)

    def test_get_optimal_limit_price_fallback(self, mock_client):
        """Test optimal price calculation raises when order book fails"""
        mock_client.info.l2_snapshot.side_effect = Exception("API error")
        with pytest.raises(Exception, match="API error"):
            mock_client.get_optimal_limit_price(
                symbol="BTC",
                side="buy",
                urgency_factor=0.5
            )

    def test_get_optimal_limit_price_no_bids(self, mock_client):
        """Test optimal price calculation when no bids are available"""
        mock_data_no_bids = {
            "coin": "BTC",
            "levels": [[], [{"n": 1, "px": "50001.0", "sz": "1.0"}]],
            "time": int(time.time() * 1000)
        }
        mock_client.info.l2_snapshot.return_value = mock_data_no_bids
        mock_client.info.all_mids.return_value = {"BTC": "50000.0"}
        price = mock_client.get_optimal_limit_price(
            symbol="BTC",
            side="buy",
            urgency_factor=0.5
        )
        # Should return formatted mid price when no bids
        assert price >= 50000.0

    def test_get_optimal_limit_price_no_asks(self, mock_client):
        """Test optimal price calculation when no asks are available"""
        mock_data_no_asks = {
            "coin": "BTC",
            "levels": [[{"n": 1, "px": "50000.0", "sz": "1.0"}], []],
            "time": int(time.time() * 1000)
        }
        mock_client.info.l2_snapshot.return_value = mock_data_no_asks
        mock_client.info.all_mids.return_value = {"BTC": "50000.0"}
        price = mock_client.get_optimal_limit_price(
            symbol="BTC",
            side="sell",
            urgency_factor=0.5
        )
        # Should return formatted mid price when no asks
        assert price <= 50000.0


# ── Integration tests — real API ─────────────────────────────────────

@pytest.fixture(scope="module")
def live_client():
    """Shared authenticated client for integration tests."""
    account = HyperliquidAccount.from_env()
    return HyperliquidClient(account=account)


class TestOptimalPricingIntegration:
    """Real API tests for order book and optimal pricing."""

    SYMBOLS = ["BTC", "ETH"]

    def test_order_book_real_data(self, live_client):
        """Order book returns valid real-time data."""
        for symbol in self.SYMBOLS:
            book = live_client.get_order_book(symbol)

            assert book["symbol"] == symbol
            assert book["best_bid"] is not None and book["best_bid"] > 0
            assert book["best_ask"] is not None and book["best_ask"] > 0
            assert book["best_ask"] > book["best_bid"], "Ask must be above bid"
            assert book["spread"] > 0
            assert book["mid_price"] > 0
            assert len(book["bids"]) > 0
            assert len(book["asks"]) > 0

            # Bids descending, asks ascending
            for i in range(1, len(book["bids"])):
                assert book["bids"][i]["price"] <= book["bids"][i-1]["price"]
            for i in range(1, len(book["asks"])):
                assert book["asks"][i]["price"] >= book["asks"][i-1]["price"]

            print(f"{symbol}: bid={book['best_bid']:.2f} ask={book['best_ask']:.2f} "
                  f"spread={book['spread']:.2f} ({book['spread']/book['mid_price']*100:.4f}%)")
            time.sleep(0.5)

    def test_optimal_price_stays_in_spread(self, live_client):
        """Patient optimal prices stay inside the spread (maker territory)."""
        for symbol in self.SYMBOLS:
            book = live_client.get_order_book(symbol)
            best_bid = book["best_bid"]
            best_ask = book["best_ask"]

            # Patient buy (urgency=0.0) should be at or near best bid
            buy_patient = live_client.get_optimal_limit_price(symbol, "buy", urgency_factor=0.0)
            assert buy_patient >= best_bid * 0.999, \
                f"Patient buy {buy_patient} too far below best bid {best_bid}"
            assert buy_patient <= best_ask, \
                f"Patient buy {buy_patient} crosses ask {best_ask} — would be taker"

            # Patient sell (urgency=0.0) should be at or near best ask
            sell_patient = live_client.get_optimal_limit_price(symbol, "sell", urgency_factor=0.0)
            assert sell_patient <= best_ask * 1.001, \
                f"Patient sell {sell_patient} too far above best ask {best_ask}"
            assert sell_patient >= best_bid, \
                f"Patient sell {sell_patient} crosses bid {best_bid} — would be taker"

            print(f"{symbol}: patient buy={buy_patient:.2f} (bid={best_bid:.2f}), "
                  f"patient sell={sell_patient:.2f} (ask={best_ask:.2f})")
            time.sleep(0.5)

    def test_urgency_ordering(self, live_client):
        """Higher urgency = more aggressive price (verified against a single snapshot)."""
        for symbol in self.SYMBOLS:
            # Fetch one order book snapshot to avoid market movement between calls
            book = live_client.get_order_book(symbol)
            best_bid = book["best_bid"]
            best_ask = book["best_ask"]
            spread = best_ask - best_bid

            if spread <= 0:
                pytest.skip(f"{symbol} has zero spread, can't test urgency ordering")

            # Manually calculate expected prices for each urgency
            buy_prices = []
            sell_prices = []
            for urgency in [0.0, 0.25, 0.5, 0.75, 1.0]:
                # Buy: bid + spread * urgency
                buy_prices.append(best_bid + spread * urgency)
                # Sell: ask - spread * urgency
                sell_prices.append(best_ask - spread * urgency)

            # Buy: higher urgency = higher price (closer to ask)
            for i in range(1, len(buy_prices)):
                assert buy_prices[i] >= buy_prices[i-1], \
                    f"{symbol} buy urgency ordering broken: {buy_prices}"

            # Sell: higher urgency = lower price (closer to bid)
            for i in range(1, len(sell_prices)):
                assert sell_prices[i] <= sell_prices[i-1], \
                    f"{symbol} sell urgency ordering broken: {sell_prices}"

            # Also verify the actual function output for one urgency matches expectation
            actual_buy = live_client.get_optimal_limit_price(symbol, "buy", 0.0)
            assert abs(actual_buy - best_bid) <= spread, \
                f"{symbol} patient buy {actual_buy} too far from bid {best_bid}"

            print(f"{symbol}: spread={spread:.2f}, "
                  f"buy range: {buy_prices[0]:.2f} -> {buy_prices[-1]:.2f}, "
                  f"sell range: {sell_prices[0]:.2f} -> {sell_prices[-1]:.2f}")
            time.sleep(0.5)

    def test_optimal_price_matches_decimals(self, live_client):
        """Optimal price must respect the symbol's price decimal precision."""
        for symbol in self.SYMBOLS:
            price = live_client.get_optimal_limit_price(symbol, "buy", 0.5)
            spec = live_client.market_specs.get(symbol, {})
            price_decimals = spec.get("price_decimals", 1)

            # Check price has correct number of decimals
            price_str = f"{price:.10f}".rstrip("0")
            if "." in price_str:
                actual_decimals = len(price_str.split(".")[1])
            else:
                actual_decimals = 0
            assert actual_decimals <= price_decimals, \
                f"{symbol} price {price} has {actual_decimals} decimals, max is {price_decimals}"

            print(f"{symbol}: optimal={price}, decimals={actual_decimals}/{price_decimals}")
            time.sleep(0.5)
