"""Hyperliquid's answers as this library reads them: the statuses of an order, a cancel or
another action, and the orders and fills of the info endpoint as models.

Nothing here sends a request. An answer that is in no documented format is never read as a
success or a failure: its outcome is unknown, and the functions raise or return None for it.
"""
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from dacite import from_dict
from hyperliquid.utils.types import Cloid

from .exceptions import (
    ActionOutcomeUnknownException,
    ActionRejectedException,
    InsufficientMarginException,
    OrderException,
    OrderRejectedException,
)
from .models import DACITE_CONFIG, Fill, Order

INSUFFICIENT_MARGIN = "Insufficient margin to place order."  # PerpMargin, API docs "Error responses"
POST_ONLY_CROSSED = "Post only order would have immediately matched"  # BadAloPx, followed by the bbo
# The error texts of rejected order statuses (info endpoint docs) that callers tell apart: an
# order recovered by its cloid after a lost answer is reported with the same text.
REJECTION_TEXTS = {"perpMarginRejected": INSUFFICIENT_MARGIN, "badAloPxRejected": POST_ONLY_CROSSED}
# The order statuses that end an order without it being filled (info endpoint docs, "Query
# order status by oid or cloid"). Only these names are final: any other name, e.g. one added
# later, leaves the order's outcome open.
REJECTED_STATUSES = frozenset({
    "rejected", "tickRejected", "minTradeNtlRejected", "perpMarginRejected", "reduceOnlyRejected",
    "badAloPxRejected", "iocCancelRejected", "badTriggerPxRejected", "marketOrderNoLiquidityRejected",
    "positionIncreaseAtOpenInterestCapRejected", "positionFlipAtOpenInterestCapRejected",
    "tooAggressiveAtOpenInterestCapRejected", "openInterestIncreaseRejected",
    "insufficientSpotBalanceRejected", "oracleRejected", "perpMaxPositionRejected",
})
CANCELED_STATUSES = frozenset({
    "canceled", "marginCanceled", "vaultWithdrawalCanceled", "openInterestCapCanceled",
    "selfTradeCanceled", "reduceOnlyCanceled", "siblingFilledCanceled", "delistedCanceled",
    "liquidatedCanceled", "scheduledCancel",
})
# Canceled by Hyperliquid, not by the order's owner (its cancel, the scheduled cancel).
REMOVED_BY_HYPERLIQUID = CANCELED_STATUSES - {"canceled", "scheduledCancel"}
# Times in force of orders that never rest on the book (frontendOpenOrders' tif).
MARKET_TIFS = frozenset({"Ioc", "FrontendMarket", "LiquidationMarket"})
# Hyperliquid's slippage tolerance for TP/SL market orders (its TP/SL docs).
TPSL_MARKET_SLIPPAGE_PCT = 10.0
# Per-order cancel error when the order is no longer open (exchange endpoint docs).
ORDER_GONE = "Order was never placed, already canceled, or filled"


def status_list(response: Any) -> Optional[List[Any]]:
    """The order statuses of an "ok" answer to an order action, else None."""
    try:
        statuses = response["response"]["data"]["statuses"] if response.get("status") == "ok" else None
    except (KeyError, TypeError, AttributeError):
        return None
    return statuses if isinstance(statuses, list) else None


def batch_error(statuses: Optional[List[Any]], count: int) -> Optional[str]:
    """The error text when one error answers a batch of several (Hyperliquid refused it
    as a whole, before doing any: its error responses docs), else None."""
    if count > 1 and statuses is not None and len(statuses) == 1 and isinstance(statuses[0], dict) \
            and set(statuses[0]) == {"error"} and isinstance(statuses[0]["error"], str):
        return statuses[0]["error"]
    return None


def is_documented_status(status: Any) -> bool:
    """True for an order status in the documented format: {"resting": {"oid"}},
    {"filled": {"oid", "totalSz", "avgPx"}} or {"error": text}, that one key alone (a status
    with two, or with keys of its own, could be read more than one way)."""
    if not isinstance(status, dict) or len(status) != 1 or next(iter(status)) not in ("resting", "filled", "error"):
        return False

    def order_id(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def amount(value: Any) -> bool:  # a decimal string or number, as the docs show them
        try:
            return not isinstance(value, bool) and Decimal(str(value)).is_finite()
        except (InvalidOperation, ValueError, TypeError):
            return False

    if isinstance(status.get("error"), str):
        return True
    resting, filled = status.get("resting"), status.get("filled")
    if isinstance(resting, dict) and order_id(resting.get("oid")):
        return True
    return (isinstance(filled, dict) and order_id(filled.get("oid"))
            and amount(filled.get("totalSz")) and amount(filled.get("avgPx")))


def documented_status(response: Any) -> Optional[Dict[str, Any]]:
    """The status of a single order's "ok" answer (an order, a modify) if it is the only one
    and in the documented format, else None: a second status would leave open which applies."""
    statuses = status_list(response)
    status = statuses[0] if statuses is not None and len(statuses) == 1 else None
    return status if is_documented_status(status) else None


def is_definite_order_response(response: Any) -> bool:
    """True if the exchange answered with the order's status: resting, filled, or an
    explicit rejection. Anything else leaves open whether the order was placed."""
    if isinstance(response, dict) and response.get("status") == "err":
        return True
    return documented_status(response) is not None


def order_status(response: Any, action: str) -> Dict[str, Any]:
    """The status of the order in the exchange's answer: {"resting": ...} or {"filled": ...}.

    Raises:
        InsufficientMarginException: The exchange refused the order for lack of margin.
        OrderRejectedException: The exchange refused it for another reason (its text).
        ActionOutcomeUnknownException: The answer is in no documented format: the order
            may still have been placed or changed.
    """
    if isinstance(response, dict) and response.get("status") == "err":  # refused as a whole
        raise OrderRejectedException(action, str(response.get("response")))
    status = documented_status(response)
    if status is None:
        raise ActionOutcomeUnknownException(action, f"answered in no documented format: {response}")
    if "error" in status:
        if status["error"].startswith(INSUFFICIENT_MARGIN):
            raise InsufficientMarginException(action, status["error"])
        raise OrderRejectedException(action, status["error"])
    return status


def order_from_response(response: Any, symbol: str, is_buy: bool, size: float, limit_price: float,
                        order_type: Dict[str, Any], reduce_only: bool, action: str,
                        market: Optional[str] = None, cloid: Optional[Cloid] = None,
                        is_position_tpsl: Optional[bool] = False) -> Order:
    """The Order of the exchange's answer to an order or a modify (the SDK sends a modify
    as batchModify): the first entry of its statuses, as the exchange endpoint documents
    it for orders. Rejections and unknown outcomes raise, see order_status. `cloid`: the
    client order id the order was sent with.

    Its fields are what the answer and the order as sent tell, as the exchange reports them
    for open orders: is_position_tpsl False (orders are sent without a TP/SL grouping; None
    for a modify, whose order may have one), trigger_condition "N/A" for an order without
    a trigger. The answer has no time and no trigger order's condition: created_at and
    that condition are None."""
    status = order_status(response, action)
    trigger = order_type.get("trigger")
    order_data: Dict[str, Any] = {
        "symbol": symbol,
        "is_buy": is_buy,
        "size": str(size),
        "order_type": order_type,
        "reduce_only": reduce_only,
        "time_in_force": order_type["limit"].get("tif", "Gtc") if "limit" in order_type else "Gtc",
        "created_at": None,
        "limit_price": str(limit_price),
        "type": "limit" if trigger is None else {"tp": "take_profit", "sl": "stop_loss"}.get(trigger.get("tpsl"), "unknown"),
    }
    if trigger is not None and "triggerPx" in trigger:
        order_data["trigger_price"] = str(trigger["triggerPx"])
    if trigger is None:
        order_data["trigger_condition"] = "N/A"
    order_data["is_position_tpsl"] = is_position_tpsl
    if market is not None:
        order_data["market"] = market
    if cloid is not None:
        order_data["cloid"] = cloid.to_raw()
    if "resting" in status:
        # The answer means nothing filled, as the official SDKs read it (0 with the size's
        # decimals, as the exchange's sizes give it). An order found open after a lost
        # answer has the size still open (_reconcile_order): the rest filled, or for a
        # reduce-only order may have been shrunk to the position, so its fills are not known.
        placed = Decimal(str(size))
        filled = placed - Decimal(str(status["resting"].get("open_size", placed)))
        order_data.update(order_id=status["resting"]["oid"], status="open", filled_size=str(filled),
                          filled_size_known=not (reduce_only and filled > 0))
    else:
        filled = status["filled"]
        order_data.update(order_id=filled["oid"], status="filled", filled_size=str(filled["totalSz"]),
                          average_fill_price=str(filled["avgPx"]))
    return from_dict(data_class=Order, data=order_data, config=DACITE_CONFIG)


def action_result(result: Any, action: str) -> Any:
    """The exchange's answer to an action other than an order (a transfer, a leverage or
    margin change): "ok" is done, "err" is refused (ActionRejectedException with its
    text); any other answer leaves open whether it was done (ActionOutcomeUnknownException)."""
    if isinstance(result, dict) and result.get("status") == "ok":
        return result
    if isinstance(result, dict) and result.get("status") == "err":
        raise ActionRejectedException(action, str(result.get("response")))
    raise ActionOutcomeUnknownException(action, f"answered in no documented format: {result}")


def cancel_outcomes(response: Any, count: int) -> List[Tuple[str, Optional[str]]]:
    """Outcome of each order in a cancel response, in request order: ("canceled", None)
    for the documented "success", ("gone", None) for the documented error of an order that
    is no longer open (never placed, already canceled or filled), ("failed", reason) for any
    other error, with the exchange's text, and ("unknown", detail) for an entry in no
    documented format: whether that order was canceled is not known. A single error for several cancels is
    Hyperliquid's answer for the whole batch (its error responses docs): every one failed
    with it.

    Raises OrderException if the response does not confirm the request (error status, a
    body that is not JSON, a status list of another length): then nothing is known.
    """
    statuses = status_list(response)
    refused = batch_error(statuses, count)
    if refused is not None:
        return [("failed", refused)] * count  # refused as a whole: nothing was canceled
    if statuses is None or len(statuses) != count:
        raise OrderException(f"Cancel was not confirmed by the exchange: {response}")
    outcomes: List[Tuple[str, Optional[str]]] = []
    for status in statuses:
        error = status["error"] if isinstance(status, dict) and set(status) == {"error"} else None
        if status == "success":
            outcomes.append(("canceled", None))
        elif isinstance(error, str) and error.startswith(ORDER_GONE):
            outcomes.append(("gone", None))
        elif isinstance(error, str):
            outcomes.append(("failed", error))
        else:  # neither canceled nor refused: what happened to that order is not known
            outcomes.append(("unknown", f"{status!r} is in no documented format"))
    return outcomes


def parse_open_order(order_data: Dict[str, Any], symbol: str, market: Optional[str] = None) -> Order:
    """An order of a frontendOpenOrders response, the same format for perp and spot:
    trigger orders (stop loss, take profit) with their trigger fields, else limit orders.
    `market` overrides the one derived from the symbol (spot orders are named by token)."""
    order_type = {}
    order_type_str = "unknown"

    if order_data.get("isTrigger", False):
        tpsl = None
        name = order_data.get("orderType") or ""  # Stop Market, Stop Limit, Take Profit Market/Limit
        if "Take Profit" in order_data.get("orderType", ""):
            tpsl = "tp"
            order_type_str = "take_profit"
        elif "Stop" in order_data.get("orderType", ""):
            tpsl = "sl"
            order_type_str = "stop_loss"

        order_type["trigger"] = {
            "triggerPx": order_data.get("triggerPx"),
            "isMarket": "Limit" not in name,
            "tpsl": tpsl
        }
    else:
        order_type["limit"] = {
            "tif": order_data.get("tif", "Gtc"),
            "px": order_data.get("limitPx")
        }
        order_type_str = "limit"

    is_buy = order_data.get("side") == "B"
    # "sz" is what is still open, "origSz" the size the order was placed with
    remaining = Decimal(str(order_data.get("sz", 0)))
    original = Decimal(str(order_data.get("origSz", remaining)))
    reduce_only = order_data.get("reduceOnly", False)
    # Hyperliquid also shrinks a resting reduce-only order to the position, so for one that
    # rested (not an IOC or a market order) the size no longer open need not be fills.
    rested = order_data.get("tif") not in MARKET_TIFS
    fills_known = not (reduce_only is True and rested and remaining < original)

    order_dict = {
        "order_id": order_data["oid"],
        "cloid": order_data.get("cloid"),
        "symbol": symbol,
        "is_buy": is_buy,
        "size": original,
        "order_type": order_type,
        "reduce_only": reduce_only,
        "status": "open",
        "time_in_force": order_data.get("tif", "Gtc") or "Gtc",
        "created_at": order_data.get("timestamp"),
        "filled_size": original - remaining,
        "filled_size_known": fills_known,
        "type": order_type_str,
        "children": order_data.get("children") or None,
        "is_position_tpsl": order_data.get("isPositionTpsl"),
        "trigger_condition": order_data.get("triggerCondition"),
    }

    if "limitPx" in order_data:
        order_dict["limit_price"] = Decimal(str(order_data["limitPx"]))
    if order_data.get("isTrigger") and "triggerPx" in order_data:  # else "0.0", a placeholder
        order_dict["trigger_price"] = Decimal(str(order_data["triggerPx"]))
    if market is not None:
        order_dict["market"] = market
    return from_dict(data_class=Order, data=order_dict, config=DACITE_CONFIG)


def parse_fill(raw: Dict[str, Any]) -> Fill:
    """A fill of the info endpoint (userFills, userFillsByTime) as a Fill."""
    return Fill(
        symbol=raw["coin"],
        side=raw.get("side", ""),
        price=Decimal(str(raw["px"])),
        size=Decimal(str(raw["sz"])),
        closed_pnl=Decimal(str(raw.get("closedPnl", "0"))),
        direction=raw.get("dir", ""),
        order_id=raw.get("oid", 0),
        crossed=raw.get("crossed", False),
        time=raw.get("time", 0),
        hash=raw.get("hash", ""),
        fee=Decimal(str(raw["fee"])) if "fee" in raw else None,
        start_position=Decimal(str(raw["startPosition"])) if "startPosition" in raw else None,
        fee_token=raw.get("feeToken"),
        trade_id=raw.get("tid"),
    )
