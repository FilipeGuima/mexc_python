import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, Defaults
from telegram.constants import ParseMode
from datetime import datetime

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, PositionInfo, CreateOrderRequest, OpenType, OrderType

# --- CONFIGURATION ---
TELEGRAM_TOKEN = "REDACTED"
MEXC_TOKEN = "REDACTED"

DEFAULT_PAIR = "BTC_USDT"
DEFAULT_MARGIN_PERC = 1.0
DEFAULT_LEVERAGE = 20
DEFAULT_TP_PERC = 1

# --- SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

API = MexcFuturesAPI(token=MEXC_TOKEN, testnet=True)


# --- TRADING LOGIC ---
async def open_trade_position(pair: str, side: OrderSide, margin_perc: float, leverage: int):
    try:
        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}

        usdt_asset = next((asset for asset in assets_response.data if asset.currency == "USDT"), None)
        if not usdt_asset: return {"success": False, "error": "USDT asset not found."}

        equity = usdt_asset.equity
        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}

        current_price = ticker_response.data.get('lastPrice')
        contract_details_response = await API.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}

        contract_size = contract_details_response.data.get('contractSize')
        margin_in_usdt = equity * (margin_perc / 100.0)
        position_size_usdt = margin_in_usdt * leverage
        value_of_one_contract_usdt = contract_size * current_price
        vol = int(position_size_usdt / value_of_one_contract_usdt)

        if vol == 0:
            return {"success": False, "error": "Calculated volume is zero. Increase margin or leverage."}

        order_response = await API.create_market_order(pair, side, vol, leverage)

        if order_response.success:
            return {
                "success": True,
                "message": f"Opened {side.name}: {vol} contracts of {pair}.",
                "orderId": order_response.data.orderId,
                "vol": vol
            }
        else:
            return {"success": False, "error": order_response.message}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def open_trade_with_tp(pair: str, side: OrderSide, margin_perc: float, leverage: int, tp_perc: float):
    open_result = await open_trade_position(pair, side, margin_perc, leverage)

    if not open_result["success"]:
        return open_result

    try:
        order_id = open_result["orderId"]
        vol = open_result["vol"]

        # await asyncio.sleep(1)

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


async def close_all_trades():
    positions_response = await API.get_open_positions()
    if not positions_response.success or not positions_response.data:
        return {"success": True, "message": "No open positions."}

    closed_count = 0
    for position in positions_response.data:
        position_type_enum = PositionType(position.positionType)
        close_side = OrderSide.CloseLong if position_type_enum == PositionType.Long else OrderSide.CloseShort
        close_response = await API.create_market_order(
            position.symbol, close_side, position.holdVol, position.leverage
        )
        if close_response.success:
            closed_count += 1

    return {"success": True, "message": f"Closed {closed_count} position(s)."}


# --- STATISTICS LOGIC ---
async def get_account_stats():
    try:
        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return "Could not fetch account assets."

        usdt_asset = next((asset for asset in assets_response.data if asset.currency == "USDT"), None)
        if not usdt_asset: return "USDT asset not found."

        history_response = await API.get_historical_positions()

        total_pnl = 0
        if history_response.success and history_response.data:
            for position in history_response.data:
                total_pnl += position.realised

        stats_message = f"""
<b>Account Statistics</b>
<b>Equity:</b> <code>{usdt_asset.equity:.2f} USDT</code>
<b>Available Balance:</b> <code>{usdt_asset.availableBalance:.2f} USDT</code>
<b>Unrealized PNL:</b> <code>{usdt_asset.unrealized:.2f} USDT</code>
<b>Realized PNL (50 trades):</b> <code>{total_pnl:.2f} USDT</code>
"""
        return stats_message
    except Exception as e:
        return f"Error fetching account stats: {e}"


async def get_market_stats(pair: str):
    try:
        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return f"Could not fetch market data for {pair}."

        data = ticker_response.data
        price_change_perc = (data.get('riseFallRate', 0)) * 100

        stats_message = f"""
<b>Market Stats for <code>{pair}</code></b>
<b>Last Price:</b> <code>{data.get('lastPrice')}</code>
<b>24h Change:</b> <code>{price_change_perc:.2f}%</code>
<b>24h High:</b> <code>{data.get('highPrice')}</code>
<b>24h Low:</b> <code>{data.get('lowPrice')}</code>
<b>24h Volume:</b> <code>{data.get('volume24')}</code>
"""
        return stats_message
    except Exception as e:
        return f"Error fetching market stats: {e}"


# --- NEW FUNCTION TO GET POSITION STATS ---
async def get_position_stats():
    try:
        positions_response = await API.get_open_positions()
        if not positions_response.success or not positions_response.data:
            return "No open positions."

        orders_response = await API.get_current_pending_orders()
        pending_orders = orders_response.data or []

        stats_message = "<b>Open Positions:</b>\n"
        for pos in positions_response.data:
            pos_type = "LONG" if pos.positionType == PositionType.Long else "SHORT"

            # Find the take-profit order associated with this position
            tp_order = next((
                order for order in pending_orders
                if order.positionId == pos.positionId and order.orderType == OrderType.PriceLimited
            ), None)
            tp_price_str = f"<code>{tp_order.price}</code>" if tp_order else "N/A"

            # Convert timestamp to readable date
            open_time = datetime.fromtimestamp(pos.createTime / 1000).strftime('%Y-%m-%d %H:%M:%S')

            stats_message += f"""
--------------------
<b>Pair:</b> <code>{pos.symbol}</code>
<b>Direction:</b> <code>{pos_type}</code>
<b>Volume:</b> <code>{pos.holdVol}</code> contracts
<b>Entry Price:</b> <code>{pos.openAvgPrice}</code>
<b>Take-Profit:</b> {tp_price_str}
<b>Margin:</b> <code>{pos.im:.2f} USDT</code>
<b>Opened At:</b> <code>{open_time}</code>
"""
        return stats_message
    except Exception as e:
        return f"Error fetching position stats: {e}"


# --- TELEGRAM COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("MEXC Trading Bot online. /help for commands.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """<b>Commands:</b>
<code>/long [pair] [margin%] [leverage]</code>
<code>/short [pair] [margin%] [leverage]</code>
<code>/longtp [pair] [margin%] [leverage] [tp%]</code>
<code>/shorttp [pair] [margin%] [leverage] [tp%]</code>
<code>/close</code> - Closes all positions.
<code>/stats [pair]</code> - Gets account or market stats.
<code>/positions</code> - Shows details of all open positions.

<b>Examples:</b>
<code>/longtp ETH_USDT 2.5 50 5</code>
<code>/stats BTC_USDT</code>
<code>/positions</code>
"""
    await update.message.reply_text(help_text)


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text.split()[0].upper()
    side = OrderSide.OpenLong if command == "/LONG" else OrderSide.OpenShort

    args = context.args
    pair = args[0] if len(args) > 0 else DEFAULT_PAIR
    margin_perc = float(args[1]) if len(args) > 1 else DEFAULT_MARGIN_PERC
    leverage = int(args[2]) if len(args) > 2 else DEFAULT_LEVERAGE

    await update.message.reply_text(f"Processing {command.replace('/', '')} for <code>{pair}</code>...")
    result = await open_trade_position(pair, side, margin_perc, leverage)

    if result["success"]:
        await update.message.reply_text(result["message"])
    else:
        await update.message.reply_text(f"Error: <code>{result['error']}</code>")


async def trade_with_tp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text.split()[0].upper()
    side = OrderSide.OpenLong if command == "/LONGTP" else OrderSide.OpenShort

    args = context.args
    pair = args[0] if len(args) > 0 else DEFAULT_PAIR
    margin_perc = float(args[1]) if len(args) > 1 else DEFAULT_MARGIN_PERC
    leverage = int(args[2]) if len(args) > 2 else DEFAULT_LEVERAGE
    tp_perc = float(args[3]) if len(args) > 3 else DEFAULT_TP_PERC

    await update.message.reply_text(
        f"Processing {command.replace('/', '')} for <code>{pair}</code> with {tp_perc}% TP...")
    result = await open_trade_with_tp(pair, side, margin_perc, leverage, tp_perc)

    if result["success"]:
        await update.message.reply_text(result["message"])
    else:
        await update.message.reply_text(f"Error: <code>{result['error']}</code>")


async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Closing all positions...")
    result = await close_all_trades()
    await update.message.reply_text(result["message"])


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        pair = context.args[0].upper()
        await update.message.reply_text(f"Fetching market stats for <code>{pair}</code>...")
        message = await get_market_stats(pair)
    else:
        await update.message.reply_text("Fetching account stats...")
        message = await get_account_stats()
    await update.message.reply_text(message)


# --- NEW COMMAND HANDLER FOR /positions ---
async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Fetching open positions...")
    message = await get_position_stats()
    await update.message.reply_text(message)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """<b>Available Commands:</b>
<code>/long</code>, <code>/short</code>, <code>/longtp</code>, <code>/shorttp</code>
<code>/close</code>, <code>/stats</code>, <code>/positions</code>
"""
    await update.message.reply_text(f"Unknown command.\n\n{help_text}")


# --- BOT STARTUP ---
def main() -> None:
    defaults = Defaults(parse_mode=ParseMode.HTML)
    application = Application.builder().token(TELEGRAM_TOKEN).defaults(defaults).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("long", trade_command))
    application.add_handler(CommandHandler("short", trade_command))
    application.add_handler(CommandHandler("close", close_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("longtp", trade_with_tp_command))
    application.add_handler(CommandHandler("shorttp", trade_with_tp_command))
    # Add the new handler
    application.add_handler(CommandHandler("positions", positions_command))
    # Keep this last
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    print("MEXC Futures API Initialized.")
    print("Telegram bot is running with default HTML parse mode.")
    application.run_polling()


if __name__ == "__main__":
    main()