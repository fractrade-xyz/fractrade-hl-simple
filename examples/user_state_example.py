from decimal import Decimal
import os
import argparse
from dotenv import load_dotenv
from fractrade_hl_simple import HyperliquidClient

# Load .env file
load_dotenv()

def main():
    # Set up argument parser
    parser = argparse.ArgumentParser(description='Get Hyperliquid user state for an address')
    parser.add_argument('--address', type=str, help='Hyperliquid address to check (optional)')
    args = parser.parse_args()

    # Initialize client (automatically uses env variables)
    client = HyperliquidClient()
    
    # Get state for specified address or default to authenticated user
    address = args.address
    state = client.get_user_state(address)
    
    # Print account summary
    print("\nAccount Summary:")
    print(f"  Account Value: ${float(state.margin_summary.account_value):,.2f}")
    print(f"  Total Position Value: ${float(state.margin_summary.total_ntl_pos):,.2f}")

    # get the perp balance for any account
    perp_balance = client.get_perp_balance("0xf967239debef10dbc78e9bbbb2d8a16b72a614eb")
    print(f"  Perp Balance for address 0xf967239debef10dbc78e9bbbb2d8a16b72a614eb: ${float(perp_balance):,.2f}")
    
    # Show positions if any exist
    if state.asset_positions:
        print("\nOpen Positions:")
        for pos in state.asset_positions:
            p = pos.position
            entry = f"${float(p.entry_price):.1f}" if p.entry_price else "N/A"
            print(f"  {p.symbol:4} {float(p.size):+.3f} @ {entry} (PnL: ${float(p.unrealized_pnl):,.2f})")
    else:
        print("\nNo open positions")

if __name__ == "__main__":
    main() 