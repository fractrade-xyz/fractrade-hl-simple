"""What a caller passes, checked and rounded as Hyperliquid takes it: sizes, prices, order ids
and client order ids."""
import math
import operator
import re
import secrets
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

from hyperliquid.utils.types import Cloid

from .exceptions import InvalidArgumentException

MIN_ORDER_USD = Decimal(10)  # Hyperliquid's minimum order value
TimeInForce = Literal["Gtc", "Ioc", "Alo"]
TIMES_IN_FORCE = ("Gtc", "Ioc", "Alo")  # of a limit order (exchange endpoint docs)
_CLOID = re.compile(r"0x[0-9a-fA-F]{32}")  # a 128 bit hex string (exchange endpoint docs)
_DIGITS = re.compile(r"[0-9]+")


def round_down(value: float, decimals: int) -> float:
    """`value` rounded toward zero to `decimals` decimals, so an order is never larger than
    requested (float round() can round up)."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN))


def round_price(price: float, max_decimals: int) -> float:
    """`price` rounded as Hyperliquid takes it: 5 significant figures and at most `max_decimals`
    decimals (6 - szDecimals for perps, 8 - szDecimals for spot). An integer is valid whatever
    its significant figures, so from 100000 on the price is rounded to an integer: a limit of
    100006 stays 100006 (5 significant figures would make it 100010)."""
    if abs(price) >= 100_000:
        return round(float(price), 0)
    return round(float(f"{price:.5g}"), max(0, max_decimals))


def positive_number(value: Any) -> bool:
    """True for a finite number above 0 (int, float, Decimal, numpy numbers); not for text or bool."""
    if isinstance(value, (bool, str, bytes)):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def order_id(value: Any) -> int:
    """An order id (oid) as the SDK sends it, a number: an integer of any type (int, numpy's),
    or the text of one. A float is refused: int(10.9) would be another order."""
    if isinstance(value, str):
        if _DIGITS.fullmatch(value.strip()):
            return int(value)
    elif not isinstance(value, bool):
        try:
            oid = operator.index(value)  # integer types only, no float or Decimal
        except TypeError:
            pass
        else:
            if oid >= 0:
                return oid
    raise InvalidArgumentException(f"order_id must be a whole number, got {value!r}")


def cloid(value: Any) -> Cloid:
    """A client order id as the SDK sends it, from its text: "0x" and 32 hex digits. The SDK's
    Cloid checks only the prefix and the length, not that it is hex."""
    if not (isinstance(value, str) and _CLOID.fullmatch(value)):
        raise InvalidArgumentException(f"cloid must be 0x followed by 32 hex digits, got {value!r}")
    return Cloid(value)


def order_cloid(value: Any) -> Cloid:
    """The client order id to place an order with: the caller's, else a new random one."""
    return Cloid("0x" + secrets.token_hex(16)) if value is None else cloid(value)


def plain(value: Any) -> str:
    """A number as a person writes it in a message: 0.00007, not 7e-05; 100, not 1E+2."""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (ArithmeticError, ValueError):
        return str(value)

