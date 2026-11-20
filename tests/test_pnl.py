"""get_pnl: trades from fills (flat -> position -> flat), their fees, funding and totals."""

import time
from decimal import Decimal as D
from unittest.mock import MagicMock

from fractrade_hl_simple import pnl
from fractrade_hl_simple.hyperliquid import HyperliquidClient
from fractrade_hl_simple.models import Fill, FillHistory, FundingPayment, Leverage, Position

START = 1_000_000


def fill(time, side, size, start, price="100", pnl="0", fee="0.1", symbol="BTC", token="USDC"):
    return Fill(symbol=symbol, side=side, price=D(price), size=D(size), closed_pnl=D(pnl), direction="",
                order_id=1, crossed=True, time=time, hash="", fee=D(fee), start_position=D(start), fee_token=token)


def funding(time, usdc, size="1", symbol="BTC"):
    return FundingPayment(time=time, symbol=symbol, market="perp", size=D(size), funding_rate=D("0.0001"),
                          usdc=D(usdc))


def position(symbol, size, unrealized):
    return Position(symbol=symbol, entry_price=D(100), size=D(size), leverage=Leverage(type="cross", value=D(5)),
                    liquidation_price=None, margin_used=D(10), position_value=D(100), return_on_equity=D(0),
                    unrealized_pnl=D(unrealized))


class TestTrades:
    def test_entries_adds_and_partial_exits_are_one_trade_with_its_fees_and_funding(self):
        trades = pnl.perp_trades([
            fill(START + 1, "B", "1", "0", price="100"),
            fill(START + 2, "B", "1", "1", price="110"),
            fill(START + 3, "A", "0.5", "2", price="120", pnl="7.5"),
            fill(START + 4, "A", "1.5", "1.5", price="130", pnl="37.5"),
        ], [funding(START + 2, "-1", size="2")], START)
        trade, = trades
        assert (trade.side, trade.status, trade.fills, trade.max_size) == ("long", "closed", 4, D(2))
        assert (trade.entry_price, trade.exit_price) == (D(105), D("127.5"))
        assert (trade.realized_pnl, trade.fees, trade.funding, trade.net_pnl) == (D(45), D("0.4"), D(-1), D("43.6"))
        assert (trade.opened_at, trade.closed_at) == (START + 1, START + 4)

    def test_a_flip_in_one_fill_closes_the_trade_and_opens_the_next(self):
        # Short 26.23, then one buy of 114.07: 26.23 close the short (all of the fill's closed
        # PnL), 87.84 open a long; the fee is split by size.
        trades = pnl.perp_trades([
            fill(START + 1, "A", "26.23", "0", price="50"),
            fill(START + 2, "B", "114.07", "-26.23", price="47", pnl="78.69", fee="11.407"),
        ], [], START)
        short, long = trades
        assert (short.side, short.status, short.realized_pnl, short.exit_price) == ("short", "closed", D("78.69"), D(47))
        assert (long.side, long.status, long.realized_pnl, long.max_size, long.entry_price) == (
            "long", "open", D(0), D("87.84"), D(47))
        assert short.fees == D("0.1") + D("2.623") and long.fees == D("8.784")

    def test_a_trade_open_when_the_range_started(self):
        trade, = pnl.perp_trades([fill(START + 5, "B", "2", "-3", price="90", pnl="20")], [], START)
        assert (trade.side, trade.status, trade.opened_before_start, trade.opened_at) == ("short", "open", True, START)
        assert (trade.entry_price, trade.exit_price, trade.max_size) == (None, D(90), D(3))

    def test_a_trade_on_a_dex_with_other_collateral_is_never_added_to_usdc(self):
        # Its fees, closed PnL and funding are all in that dex's collateral (HIP-3 allows others).
        trades = pnl.perp_trades([
            fill(START + 1, "B", "1", "0", fee="0.5", token="USDH", symbol="hx:ABC"),
            fill(START + 2, "A", "1", "1", fee="0.5", token="USDH", pnl="10", symbol="hx:ABC"),
            fill(START + 3, "B", "1", "0"), fill(START + 4, "A", "1", "1", pnl="2"),
        ], [], START)
        report = pnl.report(trades, START, START + 9, False, None)
        assert [(t.symbol, t.currency, t.net_pnl) for t in trades] == [("hx:ABC", "USDH", D(9)), ("BTC", "USDC", D("1.8"))]
        assert (report.net_pnl, report.fees) == (D("1.8"), D("0.2"))
        assert report.other_currencies["USDH"]["net_pnl"] == D(9)

    def test_open_trades_get_the_positions_unrealized_pnl(self):
        # Also a position without fills in the range: held from before.
        trades = pnl.perp_trades([fill(START + 1, "B", "1", "0")], [], START,
                                 positions=[position("BTC", "1", "12.5"), position("ETH", "-2", "-3")])
        btc, eth = sorted(trades, key=lambda t: t.symbol)
        assert btc.unrealized_pnl == D("12.5")
        assert (eth.status, eth.opened_before_start, eth.unrealized_pnl, eth.fills) == ("open", True, D(-3), 0)
        assert pnl.report(trades, START, START + 9, False, None).unrealized_pnl == D("9.5")


class TestSpot:
    def test_per_market_with_fees_by_token(self):
        fills = [fill(START + 1, "B", "100", "0", fee="0.07", token="FRAC", symbol="@50"),
                 fill(START + 2, "A", "100", "100", fee="0.02", pnl="-1.5", symbol="@50"),
                 fill(START + 3, "B", "1", "0")]
        for f in fills[:2]:
            f.market = "spot"
        market, = pnl.spot_markets(fills, {"@50": "FRAC"})
        assert (market.symbol, market.realized_pnl, market.fees, market.fills) == (
            "FRAC", D("-1.5"), {"FRAC": D("0.07"), "USDC": D("0.02")}, 2)


class TestClient:
    def client(self):
        client = HyperliquidClient.__new__(HyperliquidClient)
        client._address = lambda address: "0x" + "1" * 40
        client.market_specs = {"BTC": {"collateral": "USDC"}}
        client.get_fill_history = MagicMock(return_value=FillHistory(fills=[fill(START + 1, "B", "1", "0")],
                                                                     truncated=True, truncated_reason="retention_window"))
        client.get_user_funding = MagicMock(return_value=[])
        client.get_positions = MagicMock(return_value=[position("BTC", "1", "4")])
        return client

    def test_a_range_ending_now_has_the_unrealized_pnl_and_says_when_history_was_cut(self):
        report = self.client().get_pnl(START)
        assert report.unrealized_pnl == D(4) and (report.truncated, report.truncated_reason) == (True, "retention_window")
        # A range in the past reads no positions: an open trade has no unrealized PnL then.
        client = self.client()
        report = client.get_pnl(START, START + 100)
        client.get_positions.assert_not_called()
        assert report.end_time == START + 100 and report.trades[0].unrealized_pnl is None

    def test_a_position_opened_while_the_report_is_read_is_no_trade_from_before_the_range(self):
        # Read after the fills, its fill was not in the range: it showed as held since the start.
        client, opened = self.client(), []

        def positions(address):
            opened.append(int(time.time() * 1000))  # the position's fill: now
            return [position("BTC", "1", "4")]

        client.get_positions = MagicMock(side_effect=positions)
        client.get_fill_history = MagicMock(side_effect=lambda start, end, address: FillHistory(
            fills=[fill(t, "B", "1", "0") for t in opened if t <= end], truncated=False))
        trade, = client.get_pnl(START).trades
        assert (trade.status, trade.opened_before_start, trade.unrealized_pnl) == ("open", False, D(4))
