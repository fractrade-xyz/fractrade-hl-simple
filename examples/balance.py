from fractrade_hl_simple import HyperliquidClient
from decimal import Decimal
import sys

def print_wallet_balances(address: str):
    """Print detailed balance information for a wallet address."""
    
    # Initialize client (unauthenticated is fine for balance checking)
    client = HyperliquidClient()
    
    # Get detailed information
    perp_state = client.get_perp_balance(address, simple=False)
    spot_state = client.get_spot_balance(address, simple=False)
    
    # Calculate USDC balance
    usdc_balance = Decimal('0')
    if spot_state.tokens and "USDC" in spot_state.tokens:
        usdc_balance = spot_state.tokens["USDC"].amount
    
    # Total balance is perp + USDC
    total_balance = perp_state['state'].margin_summary.account_value + usdc_balance
    
    print(f"=== Balance Overview ===")
    print(f"Perpetual Balance: ${perp_state['state'].margin_summary.account_value:,.2f}")
    print(f"USDC Balance: ${usdc_balance:,.2f}")
    print(f"Total Balance: ${total_balance:,.2f}")
    print()
    
    print(f"=== Detailed Information ===")
    print("\nPerpetual Trading:")
    print(f"Margin Used: ${perp_state['state'].margin_summary.total_margin_used:,.2f}")
    
    # Get positions from asset_positions
    if perp_state['state'].asset_positions:
        print("\nOpen Positions:")
        for asset_pos in perp_state['state'].asset_positions:
            pos = asset_pos.position
            direction = "LONG" if float(pos.size) > 0 else "SHORT"
            if pos.entry_price:  # Check if entry price exists
                print(f"- {pos.symbol}: {direction} {abs(float(pos.size))} @ ${float(pos.entry_price):,.2f}")
                print(f"  PnL: ${float(pos.unrealized_pnl):,.2f}")
                if pos.liquidation_price:
                    print(f"  Liquidation: ${float(pos.liquidation_price):,.2f}")
                print(f"  Position Value: ${float(pos.position_value):,.2f}")
    else:
        print("\nNo open positions")
    
    # Show USDC details if any is held
    if usdc_balance > 0:
        print("\nUSDC Details:")
        usdc_token = spot_state.tokens["USDC"]
        print(f"Total: ${float(usdc_token.amount):,.2f}")
        if usdc_token.hold > 0:
            print(f"Hold: ${float(usdc_token.hold):,.2f}")
            print(f"Available: ${float(usdc_token.amount - usdc_token.hold):,.2f}")

def main():
    if len(sys.argv) != 2:
        print("Usage: python balance.py <wallet_address>")
        print("Example: python balance.py 0x123...")
        sys.exit(1)
        
    wallet_address = sys.argv[1]
    
    # Basic address validation
    if not wallet_address.startswith("0x") or len(wallet_address) != 42:
        print("Error: Invalid Ethereum address format")
        print("Address should start with '0x' and be 42 characters long")
        sys.exit(1)
    
    try:
        print_wallet_balances(wallet_address)
    except Exception as e:
        print(f"Error: Failed to fetch balances: {str(e)}")
        import traceback
        traceback.print_exc()  # This will help us see the full error stack
        sys.exit(1)

if __name__ == "__main__":
    main() 