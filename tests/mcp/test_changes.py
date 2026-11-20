"""get_changes: one interval per call and its cursor, what is reported, and what the reads
cannot establish. The library client is faked: its fills, orders and positions as Hyperliquid
returns them, and the requests made."""

import json
import threading
from decimal import Decimal
from unittest.mock import patch

import pytest

pytest.importorskip("mcp")  # the server needs the [mcp] extra

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from fractrade_hl_simple.mcp_server.accounts import AccountRegistry  # noqa: E402
from fractrade_hl_simple.mcp_server.config import parse_config  # noqa: E402
from fractrade_hl_simple.mcp_server.convert import iso  # noqa: E402
from fractrade_hl_simple.mcp_server.server import build_server  # noqa: E402
from fractrade_hl_simple.mcp_server.tools import changes  # noqa: E402
from fractrade_hl_simple.mcp_server.tools.read import read_tools  # noqa: E402
from fractrade_hl_simple.models import (  # noqa: E402
    CumFunding, Fill, FillHistory, Leverage, Order, OrderType, Position,
)

MAIN = "0x" + "1" * 40
CONFIG = f'default_account = "main"\n[accounts.main]\naddress = "{MAIN}"\n'
NOW = 1_795_000_000_000
END = NOW - changes.MARGIN_MS             # the first call's interval: [START, END]
START = NOW - changes.FIRST_LOOK_MS


def fill(time, oid=7, symbol="BTC", side="B", size="1", price="100", start="0", crossed=True):
    return Fill(symbol=symbol, side=side, price=Decimal(price), size=Decimal(size), closed_pnl=Decimal(0),
                direction="Open Long" if side == "B" else "Close Long", order_id=oid, crossed=crossed, time=time,
                hash="0x0", fee=Decimal("0.01"), start_position=Decimal(start), fee_token="USDC",
                trade_id=time * 100 + oid)


def order(oid, symbol="BTC", size="200", open_size=None, kind="limit", price="110", reduce_only=True,
          is_buy=False, created=START + 1000, status="open", status_time=None):
    trigger = {"triggerPx": price, "isMarket": True, "tpsl": "sl" if kind == "stop_loss" else "tp"}
    reduced = open_size is not None and Decimal(open_size) < Decimal(size)
    return Order(order_id=oid, symbol=symbol, is_buy=is_buy, size=Decimal(size),
                 filled_size=Decimal(size) - Decimal(open_size if open_size is not None else size),
                 filled_size_known=not (reduce_only and reduced),  # as the library reads open orders
                 order_type=OrderType(trigger=trigger) if kind != "limit" else OrderType(limit={"tif": "Gtc"}),
                 reduce_only=reduce_only, status=status, status_time=status_time, time_in_force="Gtc",
                 created_at=created, limit_price=Decimal(price),
                 trigger_price=Decimal(price) if kind != "limit" else None, type=kind)


def position(symbol, size):
    return Position(symbol=symbol, entry_price=Decimal("100"), leverage=Leverage(type="cross", value=Decimal("5")),
                    liquidation_price=None, margin_used=Decimal("20"), position_value=abs(Decimal(size)) * 100,
                    return_on_equity=Decimal("0"), size=Decimal(size), unrealized_pnl=Decimal("0"),
                    cum_funding=CumFunding(all_time=Decimal(0), since_open=Decimal(0), since_change=Decimal(0)))


class Wallet:
    """A library client for one wallet, answering as Hyperliquid would at each request."""

    def __init__(self):
        self.fills = []        # every fill; get_fill_history answers those in the range asked
        self.open = [[]]       # the open orders, one list per get_open_orders call (the last one repeats)
        self.positions = []
        self.listed = []       # historicalOrders
        self.truncated = None  # get_fill_history's truncated_reason
        self.reads = 0         # get_open_orders calls
        self.perp_dexs = ["", "xyz"]
        self.market_specs = {name: {} for name in ("BTC", "ETH", "xyz:NVDA")}
        self.now = NOW
        self.pause = None      # (inside, release): the first get_open_orders sets inside, waits for release
        self.meanwhile = None  # called once, after the first answer of get_open_orders or get_positions

    def get_open_orders(self, symbol=None, *, address):
        self.reads += 1
        if self.pause is not None:
            inside, release = self.pause
            self.pause = None
            inside.set()
            assert release.wait(5)
        return self._then(list(self.open[min(self.reads - 1, len(self.open) - 1)]))

    def get_fill_history(self, start, end=None, symbol=None, *, address):
        end = self.now if end is None else end
        fills = sorted((f for f in self.fills if start <= f.time <= end), key=lambda f: f.time)
        return FillHistory(fills=fills, truncated=self.truncated is not None, truncated_reason=self.truncated)

    def get_positions(self, *, address):
        return self._then(list(self.positions))

    def _then(self, answer):
        meanwhile, self.meanwhile = self.meanwhile, None
        if meanwhile is not None:
            meanwhile()
        return answer

    def get_historical_orders(self, *, address):
        return list(self.listed)


class Feed:
    """A server with the read tools; calls keep its state, as in one session."""

    def __init__(self, wallet):
        conf = parse_config(CONFIG, "test.toml")
        self.wallet = wallet
        self.server = build_server(read_tools(conf, AccountRegistry(conf, {}, lambda *args: wallet)))

    def answer(self, at=NOW, **arguments):
        async def run():
            async with Client(self.server, mode="2026-07-28") as client:
                result = await client.call_tool("get_changes", arguments)
                return result.is_error, json.loads(result.content[0].text)
        self.wallet.now = at
        with patch.object(changes, "_now", return_value=at):
            return anyio.run(run)

    def call(self, at=NOW, **arguments):
        is_error, body = self.answer(at, **arguments)
        assert not is_error, body
        return body


def oids(rows):
    return [row["order_id"] for row in rows]


@pytest.fixture
def wallet():
    return Wallet()


class TestCursor:
    def test_every_event_is_reported_once_across_calls(self, wallet):
        # A fill already returned but after the interval's end (it may lag in other answers)
        # comes with the next call; nothing at either bound is lost.
        wallet.fills = [fill(START, oid=1), fill(END, oid=2), fill(END + 1, oid=3)]
        wallet.listed = [order(4, status="canceled", status_time=START), order(5, status="canceled", status_time=END + 1)]
        feed = Feed(wallet)
        first = feed.call()
        assert (oids(first["fills"]), oids(first["orders"]), first["cursor"]) == ([1, 2], [4], iso(END + 1))
        second = feed.call(at=NOW + 10_000)
        assert (oids(second["fills"]), oids(second["orders"])) == ([3], [5])
        # The cursor passed back reads the same interval as the remembered one.
        assert Feed(wallet).call(at=NOW + 10_000, since=first["cursor"])["fills"] == second["fills"]

    def test_a_filtered_call_leaves_the_unfiltered_cursor_alone(self, wallet):
        wallet.fills = [fill(END - 100, oid=1, symbol="BTC"), fill(END - 50, oid=2, symbol="ETH")]
        feed = Feed(wallet)
        assert oids(feed.call(symbol="BTC")["fills"]) == [1]
        assert oids(feed.call(at=NOW + 10_000)["fills"]) == [1, 2]

    def test_a_second_call_while_one_runs_is_busy(self, wallet):
        # Waiting would read the interval twice, or hold a server worker that trades need too.
        wallet.fills = [fill(END - 100)]
        inside, release = threading.Event(), threading.Event()
        wallet.pause = (inside, release)
        feed, answers = Feed(wallet), []
        first = threading.Thread(target=lambda: answers.append(feed.call()))
        first.start()
        assert inside.wait(5)
        is_error, body = feed.answer()
        assert is_error and (body["error"]["code"], body["error"]["retryable"]) == ("BUSY", True)
        release.set()
        first.join(5)
        assert oids(answers[0]["fills"]) == [7]


class TestReported:
    def test_fills_per_order_with_the_position_before_and_after(self, wallet):
        wallet.fills = [fill(START + 10, size="1", price="100", start="0", crossed=False),
                        fill(START + 20, size="3", price="104", start="1"),
                        fill(START + 30, oid=8, symbol="para:ABC", side="A", size="2", start="2"),  # a dex not read
                        fill(START + 40, oid=9, symbol="@107")]                                    # spot: left out
        wallet.positions = [position("BTC", "4")]
        body = Feed(wallet).call()
        group = body["fills"][0]
        assert (group["order_id"], group["size"], group["average_price"], group["maker_size"], group["taker_size"],
                group["position_before"], group["position_after"]) == (7, "4", "103", "1", "3", "0", "4")
        assert oids(body["fills"]) == [7, 8]
        assert body["positions"] == [
            {"symbol": "BTC", "before": "0", "after": "4", "now": "4", "now_read": True},
            {"symbol": "para:ABC", "before": "2", "after": "0", "now": None, "now_read": False}]

    def test_protection_hyperliquid_removed_is_reported_also_once_the_position_is_gone(self, wallet):
        # Seen live on ETH: a take profit limit cancelled by Hyperliquid (reduceOnlyCanceled) when
        # another one exceeded the position; and a stop cancelled with its filled sibling, now flat.
        wallet.listed = [order(5, status="reduceOnlyCanceled", status_time=START + 40),
                         order(6, kind="stop_loss", status="siblingFilledCanceled", status_time=START + 50),
                         order(7, status="canceled", status_time=START + 60),  # canceled by its owner
                         # an entry stop, no protection, that Hyperliquid canceled for lack of margin
                         order(8, kind="stop_loss", reduce_only=False, status="marginCanceled", status_time=START + 70)]
        body = Feed(wallet).call()
        assert oids(body["protection"]["removed"]) == [5, 6] and body["positions"] == []
        assert [row["removed_by_hyperliquid"] for row in body["orders"]] == [True, True, False, True]

    def test_coverage_of_each_open_position(self, wallet):
        wallet.positions = [position("BTC", "4"), position("ETH", "-2")]
        wallet.open = [[order(1, size="4", kind="stop_loss", price="90"), order(2, size="2", price="120")]]
        coverage = Feed(wallet).call()["protection"]["coverage"]
        assert [(c["symbol"], c["stop_size"], c["take_profit_size"], c["unprotected_size"]) for c in coverage] == [
            ("BTC", "4", "2", "0"), ("ETH", "0", "0", "2")]

    @pytest.mark.parametrize("gone", ["before the open orders are read", "between the two open-order reads"])
    def test_a_stop_that_fills_during_the_call_is_not_counted_as_covering(self, wallet, gone):
        wallet.positions = [position("BTC", "4")]
        if gone == "before the open orders are read":
            wallet.open = [[order(1, size="2", kind="stop_loss", price="90")]]

            def stop_fills():
                wallet.positions, wallet.open = [position("BTC", "2")], [[]]

            wallet.meanwhile = stop_fills
        else:  # a stop with less open than placed makes the feed read the open orders twice
            wallet.open = [[order(1, size="10", open_size="4", kind="stop_loss", price="90")], []]
        coverage = Feed(wallet).call()["protection"]["coverage"]
        # The position as read first, none of it under a stop by the latest read.
        assert [(c["size"], c["stop_size"], c["unprotected_size"]) for c in coverage] == [("4", "0", "4")]

    @pytest.mark.parametrize("fills, second_open, reported", [
        ([], "191", ("0", "9")),                                                     # shrunk by Hyperliquid
        ([fill(START + 2000, oid=9, side="A", size="9", start="200")], "191", None),  # partly filled: no shrink
        ([], "190", (None, None)),                     # a fill between the two open-order reads: cause unknown
    ], ids=["shrunk", "partly-filled", "unknown"])
    def test_a_take_profit_shrunk_by_hyperliquid_is_told_from_one_partly_filled(
            self, wallet, fills, second_open, reported):
        wallet.open = [[order(9, size="200", open_size="191")], [order(9, size="200", open_size=second_open)]]
        wallet.fills = fills
        rows = Feed(wallet).call()["protection"]["reduced"]
        assert [(row["filled_size"], row["shrunk_size"]) for row in rows] == ([reported] if reported else [])

    @pytest.mark.parametrize("cut", ["fills", "orders"])
    def test_a_history_hyperliquid_may_have_cut_is_reported_as_a_gap(self, wallet, cut):
        if cut == "fills":
            wallet.truncated = "retention_window"
        else:  # a full list of recent orders that does not reach back to the interval's start
            wallet.listed = [order(i, status="filled", status_time=END - i) for i in range(changes.HISTORICAL_ORDERS_LIMIT)]
        assert [gap["what"] for gap in Feed(wallet).call()["gaps"]] == [cut]
