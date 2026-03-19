"""
Simple trailing stop example.

A trailing stop adjusts the stop loss as the price moves in your favor.
This script polls the price every few seconds and updates the stop loss
using client.trailing_stop() which is a one-shot update.

Requires authentication and an open position.
"""

import logging
import time

from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("trailing_stop")


def run_trailing_stop(client, symbol, trail_percent, check_interval=5.0):
    """Run a trailing stop loop for an open position.

    Args:
        client: Authenticated HyperliquidClient
        symbol: Trading pair (e.g., "BTC")
        trail_percent: Trail distance as percentage (e.g., 2.0 for 2%)
        check_interval: Seconds between price checks
    """
    if not client.has_position(symbol):
        logger.error(f"No position found for {symbol}")
        return

    direction = client.get_position_direction(symbol)
    logger.info(f"Starting trailing stop for {symbol} ({direction}) with {trail_percent}% trail")

    # Set initial stop loss if none exists
    sl_price = client.get_stop_loss_price(symbol)
    if sl_price is None:
        logger.info("No existing stop loss, creating one...")
        order = client.trailing_stop(symbol, trail_percent)
        logger.info(f"Initial stop loss set: {order}")

    reference_price = client.get_price(symbol)

    while True:
        if not client.has_position(symbol):
            logger.info(f"Position for {symbol} closed, stopping")
            break

        current_price = client.get_price(symbol)
        sl_price = client.get_stop_loss_price(symbol)

        if sl_price is None:
            logger.warning("Stop loss disappeared, stopping")
            break

        sl_float = float(sl_price)

        # Only move the stop loss in our favor, never against us
        if direction == "long" and current_price > reference_price:
            reference_price = current_price
            new_sl = reference_price * (1 - trail_percent / 100)
            if new_sl > sl_float:
                logger.info(f"Price {current_price:,.2f} -> moving SL from {sl_float:,.2f} to {new_sl:,.2f}")
                client.update_stop_loss(symbol, new_sl)

        elif direction == "short" and current_price < reference_price:
            reference_price = current_price
            new_sl = reference_price * (1 + trail_percent / 100)
            if new_sl < sl_float:
                logger.info(f"Price {current_price:,.2f} -> moving SL from {sl_float:,.2f} to {new_sl:,.2f}")
                client.update_stop_loss(symbol, new_sl)

        time.sleep(check_interval)


def main():
    client = HyperliquidClient()

    symbol = "BTC"
    trail_percent = 2.0  # 2% trailing stop

    if not client.has_position(symbol):
        print(f"No position for {symbol}. Open a position first.")
        return

    print(f"Starting trailing stop for {symbol} with {trail_percent}% trail")
    print("Press Ctrl+C to stop")

    try:
        run_trailing_stop(client, symbol, trail_percent)
    except KeyboardInterrupt:
        print("\nStopped by user")


if __name__ == "__main__":
    main()
