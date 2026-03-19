"""
Example showing how to inspect account state: balances, positions, and margin usage.

Works with both authenticated users and any public address.
"""

import argparse
from fractrade_hl_simple import HyperliquidClient


def main():
    parser = argparse.ArgumentParser(description="Get Hyperliquid user state")
    parser.add_argument("--address", type=str, help="Address to check (defaults to authenticated user)")
    args = parser.parse_args()

    client = HyperliquidClient()

    # Get state for specified address or authenticated user
    state = client.get_user_state(args.address)

    print("\nAccount Summary:")
    print(f"  Account Value:  ${float(state.margin_summary.account_value):,.2f}")
    print(f"  Margin Used:    ${float(state.margin_summary.total_margin_used):,.2f}")
    print(f"  Position Value: ${float(state.margin_summary.total_ntl_pos):,.2f}")
    print(f"  Withdrawable:   ${float(state.withdrawable):,.2f}")

    if state.asset_positions:
        print("\nOpen Positions:")
        for asset_pos in state.asset_positions:
            pos = asset_pos.position
            direction = "LONG" if pos.is_long else "SHORT"
            entry = f"${float(pos.entry_price):,.2f}" if pos.entry_price else "N/A"
            print(f"  {pos.symbol:8s} {direction:5s} {float(pos.size):+.4f} @ {entry}  "
                  f"PnL: ${float(pos.unrealized_pnl):,.2f}  "
                  f"Leverage: {float(pos.leverage.value)}x {pos.leverage.type}")
    else:
        print("\nNo open positions")


if __name__ == "__main__":
    main()
