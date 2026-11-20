"""Trades and their PnL from an account's fills and funding payments (HyperliquidClient.get_pnl).

A trade of a perp market runs from flat to a position and back to flat: every fill that opens,
adds or reduces belongs to it. A fill that turns a position from long to short (or back) closes
the trade and opens the next one; its closed PnL belongs to the closing part, its fee is split
by size. The fills are taken in the order Hyperliquid lists them (oldest first, as executed:
within one millisecond their trade ids are not ordered).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from .models import PERP_MARKETS, Fill, FundingPayment, PnLReport, Position, SpotPnL, Trade, coin_market

USDC = "USDC"
UNKNOWN = "unknown"  # the currency of a HIP-3 trade without fills on a dex the client does not read


class _Building:
    """A trade while its fills are read."""

    def __init__(self, symbol: str, side: str, opened_at: int, before_start: bool, currency: str = USDC) -> None:
        self.symbol, self.side, self.opened_at, self.before_start = symbol, side, opened_at, before_start
        self.currency = currency  # its dex's collateral: the token its fees are paid in
        self.position = Decimal(0)  # the position after its last fill
        self.closed_at: Optional[int] = None
        self.closed_by_then = False  # closed_at inferred: flat before it (the next trade's opening)
        self.entry: List[Tuple[Decimal, Decimal]] = []  # (size, price) of the fills that opened or added
        self.exit: List[Tuple[Decimal, Decimal]] = []   # of those that reduced or closed
        self.max_size = Decimal(0)
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.fees_other: Dict[str, Decimal] = {}
        self.funding = Decimal(0)
        self.fills = 0

    def add_fee(self, fee: Decimal, token: Optional[str]) -> None:
        if (token or USDC) == self.currency:
            self.fees += fee
        else:  # never added to the trade's currency
            self.fees_other[token or USDC] = self.fees_other.get(token or USDC, Decimal(0)) + fee

    def holds(self, time: int) -> bool:
        """Whether it was open at `time` (its first and last fills included; an inferred end not)."""
        if self.closed_at is None:
            return self.opened_at <= time
        return self.opened_at <= time and (time < self.closed_at if self.closed_by_then else time <= self.closed_at)

    def trade(self, unrealized: Optional[Decimal]) -> Trade:
        return Trade(
            symbol=self.symbol, side=self.side, currency=self.currency,
            status="open" if self.closed_at is None else "closed",
            opened_at=self.opened_at, closed_at=self.closed_at, opened_before_start=self.before_start,
            entry_price=_average(self.entry), exit_price=_average(self.exit), max_size=self.max_size,
            realized_pnl=self.realized, fees=self.fees, fees_other=dict(self.fees_other), funding=self.funding,
            net_pnl=self.realized - self.fees + self.funding,
            unrealized_pnl=unrealized if self.closed_at is None else None, fills=self.fills)


def _average(parts: List[Tuple[Decimal, Decimal]]) -> Optional[Decimal]:
    size = sum((s for s, _ in parts), Decimal(0))
    return sum((s * p for s, p in parts), Decimal(0)) / size if size else None


def _side(position: Decimal) -> str:
    return "long" if position > 0 else "short"


def perp_trades(fills: Sequence[Fill], funding: Sequence[FundingPayment], start_time: int,
                positions: Optional[Sequence[Position]] = None,
                currencies: Optional[Dict[str, str]] = None) -> List[Trade]:
    """The trades of the perp fills, with the funding paid or received while each was open.

    `positions`: the account's positions now, when the range ends now: an open trade gets the
    unrealized PnL of a position of the same size (read after the fills: a position that
    changed in between gives none), and a position without fills in the range (opened before
    it) is an open trade too. Without them, open trades have no unrealized PnL.

    A trade's figures are in its currency, its dex's collateral: `currencies` (market ->
    collateral token, from the market specs), else the token its first fill's fee is paid in,
    else USDC for a market of Hyperliquid's own dex and "unknown" for a HIP-3 one.

    A trade made from funding alone (its fills are older than the range, or beyond the fills
    Hyperliquid keeps) ends at the latest when the next trade of its market opens from flat.
    """
    done: List[_Building] = []
    current: Dict[str, _Building] = {}
    made: List[_Building] = []
    known = currencies or {}

    def new(symbol: str, side: str, at: int, held: bool, fee_token: Optional[str] = None) -> _Building:
        currency = known.get(symbol) or fee_token or (USDC if coin_market(symbol) == "perp" else UNKNOWN)
        made.append(_Building(symbol, side, start_time if held else at, held, currency))
        return made[-1]

    for fill in sorted((f for f in fills if f.market in PERP_MARKETS), key=lambda f: f.time):  # stable: as listed
        signed = fill.size if fill.side == "B" else -fill.size
        before = fill.start_position if fill.start_position is not None else Decimal(0)
        after = before + signed
        if before != 0 and after != 0 and (before > 0) != (after > 0):  # a flip: close, then open
            parts = [(before, Decimal(0), fill.closed_pnl), (Decimal(0), after, Decimal(0))]
        else:
            parts = [(before, after, fill.closed_pnl)]
        for part_before, part_after, pnl in parts:
            size = abs(part_after - part_before)
            trade = current.get(fill.symbol)
            if trade is None:
                held = part_before != 0  # open when the range started
                trade = new(fill.symbol, _side(part_before if held else part_after), fill.time, held,
                            fill.fee_token)
                current[fill.symbol] = trade
            trade.fills += 1 if part_before == before else 0  # a flip counts once, for the closing trade
            (trade.exit if abs(part_after) < abs(part_before) else trade.entry).append((size, fill.price))
            trade.realized += pnl
            if fill.fee:
                trade.add_fee(fill.fee * size / fill.size, fill.fee_token)
            trade.max_size = max(trade.max_size, abs(part_before), abs(part_after))
            trade.position = part_after
            if part_after == 0:
                trade.closed_at = fill.time
                done.append(current.pop(fill.symbol))
    if positions is not None:  # held through the range without a fill (not one reopened after its fills)
        with_fills = {t.symbol for t in made}
        for position in positions:
            if position.size != 0 and position.symbol not in with_fills:
                trade = current[position.symbol] = new(position.symbol, _side(position.size), start_time, True)
                trade.max_size, trade.position = abs(position.size), position.size
    for payment in sorted((p for p in funding if p.market in PERP_MARKETS), key=lambda p: p.time):
        trade = _paid_by(payment, made)
        if trade is None:  # held from before the range, without fills
            trade = new(payment.symbol, _side(payment.size), start_time, True)
            later = [t.opened_at for t in made if t.symbol == payment.symbol and not t.before_start
                     and t.opened_at > payment.time]
            if later:  # flat by the time the next one opened
                trade.closed_at, trade.closed_by_then = min(later), True
            else:
                current[payment.symbol] = trade
        if not trade.entry and not trade.exit:  # no fills: its size is the funded one
            trade.max_size, trade.position = max(trade.max_size, abs(payment.size)), payment.size
        trade.funding += payment.usdc
    by_symbol = {p.symbol: p for p in positions or []}
    holder: Dict[str, _Building] = {}  # per market at most one trade gets the position's unrealized PnL
    for trade in made:
        now = by_symbol.get(trade.symbol)
        if now is not None and trade.closed_at is None and now.size != 0 and now.size == trade.position:
            holder[trade.symbol] = trade  # the latest made
    trades = []
    for trade in made:
        now = by_symbol.get(trade.symbol)
        trades.append(trade.trade(now.unrealized_pnl if now is not None and holder.get(trade.symbol) is trade
                                  else None))
    trades.sort(key=lambda t: (t.opened_at, t.symbol))
    return trades


def _paid_by(payment: FundingPayment, made: List[_Building]) -> Optional[_Building]:
    """The trade a funding payment belongs to: one open at its time (at a flip's millisecond
    both are: the one on the payment's side), else the latest one before it on that side;
    None if there is none (a position held from before the range)."""
    side = _side(payment.size) if payment.size != 0 else None
    before = [t for t in made if t.symbol == payment.symbol and t.opened_at <= payment.time]
    holding = [t for t in before if t.holds(payment.time)]
    candidates = [t for t in holding if side is None or t.side == side] or holding
    if not candidates:
        candidates = [t for t in before if side is None or t.side == side]
    return candidates[-1] if candidates else None  # made in order: the latest wins a tie


def spot_markets(fills: Sequence[Fill], names: Dict[str, str]) -> List[SpotPnL]:
    """Per spot market: Hyperliquid's closed PnL of its fills (in the quote token) and the fees
    by the token they were paid in (a buy pays in the base token). `names`: coin -> symbol."""
    markets: Dict[str, SpotPnL] = {}
    for fill in fills:
        if fill.market != "spot":
            continue
        symbol = names.get(fill.symbol, fill.symbol)
        market = markets.setdefault(symbol, SpotPnL(symbol=symbol, realized_pnl=Decimal(0), fees={}, fills=0))
        market.realized_pnl += fill.closed_pnl
        if fill.fee:
            token = fill.fee_token or USDC
            market.fees[token] = market.fees.get(token, Decimal(0)) + fill.fee
        market.fills += 1
    return sorted(markets.values(), key=lambda m: m.symbol)


def totals(trades: Sequence[Trade]) -> Dict[str, Optional[Decimal]]:
    """realized_pnl, fees, funding, net_pnl and unrealized_pnl (None if no trade has one)."""
    unrealized = [t.unrealized_pnl for t in trades if t.unrealized_pnl is not None]
    return {"realized_pnl": sum((t.realized_pnl for t in trades), Decimal(0)),
            "fees": sum((t.fees for t in trades), Decimal(0)),
            "funding": sum((t.funding for t in trades), Decimal(0)),
            "net_pnl": sum((t.net_pnl for t in trades), Decimal(0)),
            "unrealized_pnl": sum(unrealized, Decimal(0)) if unrealized else None}


def report(trades: List[Trade], start_time: int, end_time: int, truncated: bool,
           truncated_reason: Optional[str], spot: Optional[List[SpotPnL]] = None) -> PnLReport:
    """The totals of the USDC trades; those in another currency apart, by currency, never
    added to USDC; so are fees paid in a token other than the trade's."""
    fees_other: Dict[str, Decimal] = {}
    for trade in trades:
        for token, fee in trade.fees_other.items():
            fees_other[token] = fees_other.get(token, Decimal(0)) + fee
    usdc = [t for t in trades if t.currency == USDC]
    others = {currency: totals([t for t in trades if t.currency == currency])
              for currency in sorted({t.currency for t in trades} - {USDC})}
    figures = totals(usdc)
    return PnLReport(
        start_time=start_time, end_time=end_time, trades=trades,
        realized_pnl=figures["realized_pnl"] or Decimal(0), fees=figures["fees"] or Decimal(0), fees_other=fees_other,
        funding=figures["funding"] or Decimal(0), net_pnl=figures["net_pnl"] or Decimal(0),
        unrealized_pnl=figures["unrealized_pnl"], other_currencies=others,
        truncated=truncated, truncated_reason=truncated_reason, spot=spot)
