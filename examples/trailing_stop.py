#!/usr/bin/env python
"""
Simple Trailing Stop Example for HyperliquidClient

This example demonstrates how to implement a basic trailing stop using asyncio.
A trailing stop automatically adjusts the stop loss as the price moves in your favor.
"""

import asyncio
import logging
from typing import Optional

from fractrade_hl_simple import HyperliquidClient, HyperliquidAccount

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("trailing_stop")

async def trailing_stop(
    client: HyperliquidClient,
    symbol: str,
    trail_percent: float,
    check_interval: float = 5.0
) -> None:
    """Run a trailing stop for a position.
    
    Args:
        client: The HyperliquidClient instance
        symbol: The trading symbol (e.g., "BTC")
        trail_percent: The trailing percentage (e.g., 2.0 for 2%)
        check_interval: How often to check the price in seconds
    """
    # Check if position exists
    if not client.has_position(symbol):
        logger.error(f"No position found for {symbol}")
        return
    
    # Get position direction
    position_direction = client.get_position_direction(symbol)
    position_size = client.get_position_size(symbol)
    
    logger.info(f"Starting trailing stop for {symbol} ({position_direction} position) with {trail_percent}% trail")
    
    # Get current price and position size
    current_price = client.get_price(symbol)
    position_size_abs = abs(float(position_size))
    
    # Set initial stop loss if none exists
    sl_price = client.get_stop_loss_price(symbol)
    if sl_price is None:
        # Calculate initial stop loss price
        if position_direction == "long":
            sl_price = current_price * (1 - trail_percent / 100)
        else:
            sl_price = current_price * (1 + trail_percent / 100)
            
        # Create initial stop loss order
        is_buy = position_direction == "short"
        logger.info(f"Creating initial stop loss at {sl_price:.2f}")
        client.stop_loss(symbol, position_size_abs, float(sl_price), is_buy=is_buy)
    else:
        logger.info(f"Using existing stop loss at {float(sl_price):.2f}")
    
    # Store the reference price (highest for long, lowest for short)
    reference_price = current_price
    
    try:
        while True:
            # Check if position still exists
            if not client.has_position(symbol):
                logger.info(f"Position for {symbol} closed, stopping trailing stop")
                break
                
            # Get current price and stop loss
            current_price = client.get_price(symbol)
            sl_price = client.get_stop_loss_price(symbol)
            
            if sl_price is None:
                logger.warning(f"Stop loss for {symbol} not found, stopping")
                break
            
            sl_price_float = float(sl_price)
                
            # Update stop loss if price moved in our favor
            if position_direction == "long":
                if current_price > reference_price:
                    # Update reference price and calculate new stop loss
                    reference_price = current_price
                    new_sl_price = reference_price * (1 - trail_percent / 100)
                    
                    # Only update if the new stop loss is higher
                    if new_sl_price > sl_price_float:
                        logger.info(f"Updating stop loss to {new_sl_price:.2f}")
                        client.update_stop_loss(symbol, new_sl_price)
            else:  # Short position
                if current_price < reference_price:
                    # Update reference price and calculate new stop loss
                    reference_price = current_price
                    new_sl_price = reference_price * (1 + trail_percent / 100)
                    
                    # Only update if the new stop loss is lower
                    if new_sl_price < sl_price_float:
                        logger.info(f"Updating stop loss to {new_sl_price:.2f}")
                        client.update_stop_loss(symbol, new_sl_price)
            
            # Wait before checking again
            await asyncio.sleep(check_interval)
    except Exception as e:
        logger.error(f"Error in trailing stop: {str(e)}")


async def main():
    # Create client from environment variables
    account = HyperliquidAccount.from_env()
    client = HyperliquidClient(account=account)
    
    # Configuration
    symbol = "BTC"  # Change to your symbol
    trail_percent = 2.0  # 2% trailing stop
    
    # Ensure you have an open position
    if not client.has_position(symbol):
        print(f"No position for {symbol}. Please open a position first.")
        return
    
    print(f"Starting trailing stop for {symbol} with {trail_percent}% trail")
    print("Press Ctrl+C to stop")
    
    try:
        await trailing_stop(client, symbol, trail_percent)
    except KeyboardInterrupt:
        print("Trailing stop terminated by user")


if __name__ == "__main__":
    asyncio.run(main())


