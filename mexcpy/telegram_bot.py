import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, Defaults
from telegram.constants import ParseMode

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, PositionInfo

# --- CONFIGURATION ---
TELEGRAM_TOKEN = "8255841968:AAGpzntmicAST9IqfBTBxExnSkHH7WktJRw"
# MEXC_TOKEN = "WEB4fe4b377534c557ed822621b521dbcced6d03f469ae0610af920e5a726f77ec0"
MEXC_API_KEY = "mx0vglbHa8gXEKQ4VH"
MEXC_SECRET_KEY = "af55541eea2a49ed86a7bc8bb6ee89d0"


DEFAULT_PAIR = "BTC_USDT"
DEFAULT_MARGIN_PERC = 1.0
DEFAULT_LEVERAGE = 20

# --- SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

API = MexcFuturesAPI(api_key=MEXC_API_KEY, secret_key=MEXC_SECRET_KEY, testnet=True)


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
            return {"success": True, "message": f"Opened {side.name}: {vol} contracts of {pair}."}
        else:
            return {"success": False, "error": order_response.message}

    except Exception as e:
        return {"success": False, "error": str(e)}


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

        history_response = await API._make_request(
            "GET",
            "/private/position/list/history_positions",
            {"page_size": 50},
            response_type=PositionInfo
        )

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


# --- TELEGRAM COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("MEXC Trading Bot online. /help for commands.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """<b>Commands:</b>
<code>/long [pair] [margin%] [leverage]</code>
<code>/short [pair] [margin%] [leverage]</code>
<code>/close</code>
<code>/stats [pair]</code>

<b>Examples:</b>
<code>/long</code>
<code>/long BTC_USDT 2.5 50</code>
<code>/stats</code>
<code>/stats BTC_USDT</code>
"""
    await update.message.reply_text(help_text)


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text.split()[0].upper()
    side = OrderSide.OpenLong if command == "/LONG" else OrderSide.OpenShort

    args = context.args
    pair = args[0] if len(args) > 0 else DEFAULT_PAIR
    margin_perc = float(args[1]) if len(args) > 1 else DEFAULT_MARGIN_PERC
    leverage = int(args[2]) if len(args) > 2 else DEFAULT_LEVERAGE

    await update.message.reply_text(
        f"Processing {command.replace('/', '')} for <code>{pair}</code>..."
    )

    result = await open_trade_position(pair, side, margin_perc, leverage)

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


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """<b>Available Commands:</b>
<code>/long [pair] [margin%] [leverage]</code>
<code>/short [pair] [margin%] [leverage]</code>
<code>/close</code>
<code>/stats [pair]</code>
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
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    print("MEXC Futures API Initialized.")
    print("Telegram bot is running with default HTML parse mode.")
    application.run_polling()


if __name__ == "__main__":
    main()