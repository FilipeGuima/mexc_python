import asyncio
import logging
from datetime import datetime

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, CreateOrderRequest, OpenType, OrderType

# --- CONFIGURATION ---
# Your MEXC token (replace with your actual key if needed)
MEXC_TOKEN = "REDACTED"

# Default trading parameters
DEFAULT_PAIR = "SUI_USDT"
DEFAULT_EQUITY_PERC = 1.0  # Default percentage of equity to use for margin
DEFAULT_LEVERAGE = 20
DEFAULT_TP_PERC = 1

# --- SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize the API. Set testnet=False for live trading.
API = MexcFuturesAPI(token=MEXC_TOKEN, testnet=True)


# --- TRADING LOGIC (Copied from the bot) ---

async def open_trade_position(pair: str, side: OrderSide, equity_perc: float, leverage: int):
    try:
        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}
        usdt_asset = next((asset for asset in assets_response.data if asset.currency == "USDT"), None)
        if not usdt_asset: return {"success": False, "error": "USDT asset not found."}
        equity = usdt_asset.equity
        margin_in_usdt = equity * (equity_perc / 100.0)
        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}
        current_price = ticker_response.data.get('lastPrice')
        contract_details_response = await API.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}
        contract_size = contract_details_response.data.get('contractSize')
        position_size_usdt = margin_in_usdt * leverage
        value_of_one_contract_usdt = contract_size * current_price
        vol = int(position_size_usdt / value_of_one_contract_usdt)
        if vol == 0:
            return {"success": False, "error": "Calculated volume is zero. Increase equity percentage or leverage."}
        order_response = await API.create_market_order(pair, side, vol, leverage)
        if order_response.success:
            return {
                "success": True,
                "message": f"Opened {side.name}: {vol} contracts of {pair} using {margin_in_usdt:.2f} USDT as margin ({equity_perc}% equity @ {leverage}x leverage).",
                "orderId": order_response.data.orderId,
                "vol": vol
            }
        else:
            return {"success": False, "error": order_response.message}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def open_trade_with_tp(pair: str, side: OrderSide, equity_perc: float, leverage: int, tp_perc: float):
    open_result = await open_trade_position(pair, side, equity_perc, leverage)
    if not open_result["success"]:
        return open_result
    try:
        order_id = open_result["orderId"]
        vol = open_result["vol"]
        order_details_response = await API.get_order_by_order_id(str(order_id))
        if not order_details_response.success:
            return {"success": False, "error": "Position opened, but failed to get order details for TP."}
        entry_price = order_details_response.data.dealAvgPrice
        if entry_price == 0:
            ticker = await API.get_ticker(pair)
            entry_price = ticker.data.get('lastPrice')
        if side == OrderSide.OpenLong:
            tp_price = entry_price * (1 + tp_perc / 100.0)
            close_side = OrderSide.CloseLong
        else:
            tp_price = entry_price * (1 - tp_perc / 100.0)
            close_side = OrderSide.CloseShort
        contract_details = await API.get_contract_details(pair)
        tick_size = contract_details.data.get('tickSize', 0.1)
        rounded_tp_price = round(tp_price / tick_size) * tick_size if tick_size > 0 else tp_price
        tp_order_request = CreateOrderRequest(
            symbol=pair, side=close_side, vol=vol, leverage=leverage,
            price=rounded_tp_price, openType=OpenType.Isolated, type=OrderType.PriceLimited,
        )
        tp_order_response = await API.create_order(tp_order_request)
        if tp_order_response.success:
            return {"success": True, "message": f"{open_result['message']}\nTake-Profit set at {rounded_tp_price:.4f}."}
        else:
            return {"success": False, "error": f"Position opened, but failed to set TP: {tp_order_response.message}"}
    except Exception as e:
        return {"success": False, "error": f"Position opened, but an error occurred setting TP: {e}"}


async def partially_close_trade(symbol: str, percentage: float):
    try:
        if not 0 < percentage <= 100:
            return {"success": False, "error": "Percentage must be between 1 and 100."}

        positions_response = await API.get_open_positions(symbol=symbol)
        if not positions_response.success or not positions_response.data:
            return {"success": False, "error": f"No open position found for {symbol}."}

        closed_messages = []
        error_messages = []

        # Loop through ALL open positions for the symbol (handles hedge mode)
        for position in positions_response.data:
            vol_to_close = int(position.holdVol * (percentage / 100.0))
            if vol_to_close == 0:
                error_messages.append(f"Calculated volume for {position.symbol} was zero.")
                continue

            position_type_enum = PositionType(position.positionType)
            close_side = OrderSide.CloseLong if position_type_enum == PositionType.Long else OrderSide.CloseShort

            close_response = await API.create_market_order(
                position.symbol, close_side, vol_to_close, position.leverage
            )

            if close_response.success:
                pos_type_str = "LONG" if position_type_enum == PositionType.Long else "SHORT"
                closed_messages.append(f"Closed {percentage}% of {pos_type_str} ({vol_to_close} contracts).")
            else:
                error_messages.append(f"Failed to close position: {close_response.message}")

        # Combine messages for the final report
        final_message = ""
        if closed_messages:
            final_message += "Success:\n" + "\n".join(closed_messages)
        if error_messages:
            final_message += "\nErrors:\n" + "\n".join(error_messages)

        if not final_message:
            return {"success": False, "error": "No positions were closed."}

        return {"success": True, "message": final_message}

    except Exception as e:
        return {"success": False, "error": str(e)}


# --- STATISTICS LOGIC (Copied from the bot) ---

async def get_account_stats():
    # ... (This function is unchanged)
    try:
        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return "Could not fetch account assets."
        usdt_asset = next((asset for asset in assets_response.data if asset.currency == "USDT"), None)
        if not usdt_asset: return "USDT asset not found."
        history_response = await API.get_historical_positions()
        total_pnl = sum(pos.realised for pos in history_response.data) if history_response.success and history_response.data else 0
        return (
            f"--- Account Stats ---\n"
            f"Equity: {usdt_asset.equity:.2f} USDT\n"
            f"Available Balance: {usdt_asset.availableBalance:.2f} USDT\n"
            f"Unrealized PNL: {usdt_asset.unrealized:.2f} USDT\n"
            f"Realized PNL (50 trades): {total_pnl:.2f} USDT\n"
        )
    except Exception as e:
        return f"Error fetching account stats: {e}"

async def get_market_stats(pair: str):
    # ... (This function is unchanged)
    try:
        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return f"Could not fetch market data for {pair}."
        data = ticker_response.data
        price_change_perc = data.get('riseFallRate', 0) * 100
        return (
            f"--- Market Stats for {pair} ---\n"
            f"Last Price: {data.get('lastPrice')}\n"
            f"24h Change: {price_change_perc:.2f}%\n"
            f"24h High: {data.get('highPrice')}\n"
            f"24h Low: {data.get('lowPrice')}\n"
        )
    except Exception as e:
        return f"Error fetching market stats: {e}"

async def get_position_stats():
    # ... (This function is unchanged)
    try:
        positions_response = await API.get_open_positions()
        if not positions_response.success or not positions_response.data:
            return "No open positions."
        orders_response = await API.get_current_pending_orders()
        pending_orders = orders_response.data or []
        stats_message = "--- Open Positions ---\n"
        for pos in positions_response.data:
            pos_type = "LONG" if pos.positionType == PositionType.Long else "SHORT"
            tp_order = next((o for o in pending_orders if o.positionId == pos.positionId and o.orderType == OrderType.PriceLimited), None)
            tp_price_str = f"{tp_order.price}" if tp_order else "N/A"
            open_time = datetime.fromtimestamp(pos.createTime / 1000).strftime('%Y-%m-%d %H:%M:%S')
            stats_message += (
                f"--------------------\n"
                f"Pair: {pos.symbol}\n"
                f"Direction: {pos_type}\n"
                f"Volume: {pos.holdVol} contracts\n"
                f"Entry Price: {pos.openAvgPrice}\n"
                f"Take-Profit: {tp_price_str}\n"
                f"Margin: {pos.im:.2f} USDT\n"
                f"Opened At: {open_time}\n"
            )
        return stats_message
    except Exception as e:
        return f"Error fetching position stats: {e}"


# --- TERMINAL APPLICATION ---

def print_help():
    """Prints the help message to the console."""
    help_text = f"""
    --- MEXC Terminal Trader ---
    Commands are comma-separated. Default pair: {DEFAULT_PAIR}

    TRADING:
    long,<equity%>,<leverage>
    short,<equity%>,<leverage>
    longtp,<equity%>,<leverage>,<tp%>
    shorttp,<equity%>,<leverage>,<tp%>
    close,<percentage>

    VIEWING:
    stats
    positions
    help
    exit

    EXAMPLES:
    long,1,50         (Opens a long using 1% equity at 50x leverage)
    longtp,2.5,75,5   (Opens a long with 2.5% equity, 75x lev, 5% TP)
    close,50          (Closes 50% of the default pair's position)
    close,100         (Closes 100% of the default pair's position)
    """
    print(help_text)

async def main():
    """Main function to run the terminal command handler."""
    print("MEXC Terminal Trader is running.")
    print(f"Default pair is set to {DEFAULT_PAIR}.")
    print("Type 'help' for a list of commands or 'exit' to quit.")

    while True:
        try:
            # Get user input from the terminal
            user_input = await asyncio.to_thread(input, "> ")
            if not user_input:
                continue

            parts = user_input.strip().split(',')
            command = parts[0].lower()
            args = parts[1:]

            # --- Command Handling ---
            if command == "exit":
                print("Exiting...")
                break
            elif command == "help":
                print_help()
            elif command in ["long", "short"]:
                equity_perc = float(args[0]) if len(args) > 0 else DEFAULT_EQUITY_PERC
                leverage = int(args[1]) if len(args) > 1 else DEFAULT_LEVERAGE
                side = OrderSide.OpenLong if command == "long" else OrderSide.OpenShort
                print(f"Processing {command} for {DEFAULT_PAIR}...")
                result = await open_trade_position(DEFAULT_PAIR, side, equity_perc, leverage)
                print(result['message'] if result['success'] else f"Error: {result['error']}")
            elif command in ["longtp", "shorttp"]:
                equity_perc = float(args[0]) if len(args) > 0 else DEFAULT_EQUITY_PERC
                leverage = int(args[1]) if len(args) > 1 else DEFAULT_LEVERAGE
                tp_perc = float(args[2]) if len(args) > 2 else DEFAULT_TP_PERC
                side = OrderSide.OpenLong if command == "longtp" else OrderSide.OpenShort
                print(f"Processing {command} for {DEFAULT_PAIR}...")
                result = await open_trade_with_tp(DEFAULT_PAIR, side, equity_perc, leverage, tp_perc)
                print(result['message'] if result['success'] else f"Error: {result['error']}")
            elif command == "close":
                percentage = float(args[0]) if args else 100.0
                print(f"Processing request to close {percentage}% of {DEFAULT_PAIR}...")
                result = await partially_close_trade(DEFAULT_PAIR, percentage)
                print(result['message'] if result['success'] else f"Error: {result['error']}")
            elif command == "stats":
                print("Fetching stats...")
                account_msg = await get_account_stats()
                market_msg = await get_market_stats(DEFAULT_PAIR)
                print(account_msg)
                print(market_msg)
            elif command == "positions":
                print("Fetching open positions...")
                positions_msg = await get_position_stats()
                print(positions_msg)
            else:
                print(f"Unknown command: '{command}'. Type p for options.")

        except (ValueError, IndexError) as e:
            print(f"Invalid arguments. Please check your command format. Error: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProgram interrupted by user. Exiting.")

