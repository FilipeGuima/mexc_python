import asyncio
import logging
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, Defaults
from telegram.constants import ParseMode
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

# --- CLEAN IMPORTS ---
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import PositionType, OrderType

from mexcpy.config import TELEGRAM_BOT_TOKEN, STATS_ACCOUNTS

# --- UI CONSTANTS ---
TOKEN_MENU_PAIRS = [
    ["BTC_USDT", "ETH_USDT", "SOL_USDT"],
    ["DOGE_USDT", "SUI_USDT"],
    ["/stats", "/positions", "/help"]
]

KEYBOARD_MARKUP = ReplyKeyboardMarkup(
    TOKEN_MENU_PAIRS,
    resize_keyboard=True,
    is_persistent=True
)

# --- CONFIGURATION CHECKS ---
if not TELEGRAM_BOT_TOKEN:
    print(" ERROR: TELEGRAM_BOT_TOKEN not found in .env via mexcpy.config")
    exit(1)

if not STATS_ACCOUNTS:
    print(" WARNING: No STATS_BOTx_TOKEN defined in .env. Stats functionality will be limited.")

# --- SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize a dictionary of API clients (one for each configured bot)
API_CLIENTS: Dict[str, MexcFuturesAPI] = {
    account['account_id']: MexcFuturesAPI(token=account['token'], testnet=False)
    for account in STATS_ACCOUNTS
}


# --- UTILITY FUNCTION ---
async def fetch_api_response(api: MexcFuturesAPI, method: str, *args, **kwargs) -> Any:
    """Utility to safely call API methods and return data or None on failure."""
    try:
        method_func = getattr(api, method, None)
        if not method_func:
            return None

        response = await method_func(*args, **kwargs)

        if response.success:
            if isinstance(response.data, (list, dict)) and not response.data:
                return None
            return response.data
        else:
            logger.warning(f"API call failed for {method}: {response.message}")
            return None
    except Exception as e:
        logger.error(f"API Exception in {method}: {e}")
        return None


# --- STATISTICS LOGIC (Global and Pair-Agnostic) ---

async def get_account_stats(api: MexcFuturesAPI, account_id: str):
    """
    Fetches and formats general account statistics including PNL history,
    dynamically determining the margin currency.
    """
    try:
        assets_response_data = await fetch_api_response(api, "get_user_assets")
        if not assets_response_data:
            return f"<b>--- {account_id} ---</b>\nCould not fetch account assets."

        # Dynamically determine the primary margin currency for display
        primary_asset = None
        common_quote_currencies = ["USDT", "USDC", "BTC", "ETH"]

        # 1. Prioritize common quote currencies with non-zero equity
        for currency in common_quote_currencies:
            asset = next((a for a in assets_response_data if a.currency == currency and a.equity > 0), None)
            if asset:
                primary_asset = asset
                break

        # 2. Fallback to the asset with the highest equity
        if not primary_asset and assets_response_data:
            primary_asset = max(assets_response_data, key=lambda a: getattr(a, 'equity', 0))

        if not primary_asset:
            return f"<b>--- {account_id} ---</b>\nNo primary asset found in wallet."

        display_currency = primary_asset.currency

        all_positions = []
        page_num = 1
        page_size = 100
        thirty_days_ago = datetime.now() - timedelta(days=30)

        while True:
            history_data = await fetch_api_response(
                api, "get_historical_positions", symbol=None, page_num=page_num, page_size=page_size
            )

            if not history_data:
                break

            all_positions.extend(history_data)

            last_trade_in_page = history_data[-1]
            last_trade_time = datetime.fromtimestamp(last_trade_in_page.updateTime / 1000)

            if last_trade_time < thirty_days_ago:
                break

            page_num += 1

            if page_num > 50:
                logger.warning(f"Stopped fetching history for {account_id} after 50 pages.")
                break

        pnl_last_day = 0.0
        pnl_last_7_days = 0.0
        pnl_last_30_days = 0.0

        if all_positions:
            now = datetime.now()

            for position in all_positions:
                if hasattr(position, 'realised') and hasattr(position, 'updateTime'):
                    try:
                        realised_pnl = float(position.realised)
                        close_time = datetime.fromtimestamp(position.updateTime / 1000)
                        time_difference = now - close_time

                        if close_time > thirty_days_ago:
                            pnl_last_30_days += realised_pnl
                            if time_difference.days < 7:
                                pnl_last_7_days += realised_pnl
                            if time_difference.days < 1:
                                pnl_last_day += realised_pnl
                    except Exception:
                        pass

        stats_message = f"""
<b>--- {account_id} ---</b>
<b>Equity:</b> <code>{primary_asset.equity:.2f} {display_currency}</code>
<b>Available Balance:</b> <code>{primary_asset.availableBalance:.2f} {display_currency}</code>
<b>Unrealized PNL:</b> <code>{primary_asset.unrealized:.2f} {display_currency}</code>
---
<b>Realized PNL (24h):</b> <code>{pnl_last_day:.2f} {display_currency}</code>
<b>Realized PNL (7d):</b> <code>{pnl_last_7_days:.2f} {display_currency}</code>
<b>Realized PNL (30d):</b> <code>{pnl_last_30_days:.2f} {display_currency}</code>
"""
        return stats_message
    except Exception as e:
        return f"Error fetching account stats for {account_id}: {e}"


async def get_last_position_stats(api: MexcFuturesAPI, account_id: str):
    """
    Fetches and formats the *absolute* last open or closed position for the account
    across all pairs.
    """
    try:
        # Check for ANY open positions first (symbol=None)
        open_positions_data = await fetch_api_response(api, "get_open_positions", symbol=None)

        if open_positions_data:
            # Find the most recently opened position
            pos = max(open_positions_data, key=lambda p: p.createTime)
            pair = pos.symbol
            pos_type = "LONG" if pos.positionType == PositionType.Long.value else "SHORT"
            open_time = datetime.fromtimestamp(pos.createTime / 1000).strftime('%Y-%m-%d %H:%M:%S')

            pnl_value = 0.0
            ticker_data, contract_details_data = await asyncio.gather(
                fetch_api_response(api, "get_ticker", pair),
                fetch_api_response(api, "get_contract_details", pair)
            )

            if ticker_data and contract_details_data:
                current_price = float(ticker_data.get('lastPrice', 0))
                contract_size = float(contract_details_data.get('contractSize', 1))
                entry_price = float(pos.openAvgPrice)
                position_volume = float(pos.holdVol)

                if pos.positionType == PositionType.Long.value:
                    pnl_value = (current_price - entry_price) * position_volume * contract_size
                else:
                    pnl_value = (entry_price - current_price) * position_volume * contract_size

            return f"""
<b>--- {account_id} Last Position Stats (OPEN) ---</b>
<b>Pair:</b> <code>{pos.symbol}</code>
<b>Direction:</b> <code>{pos_type}</code>
<b>Margin Size:</b> <code>{pos.im:.2f} USDT</code>
<b>Open Time:</b> <code>{open_time}</code>
<b>Unrealized PNL:</b> <code>{pnl_value:.2f} USDT</code>
"""

        # If no open positions, check historical positions (symbol=None)
        history_data = await fetch_api_response(api, "get_historical_positions", symbol=None, page_size=1)
        if history_data:
            last_pos = history_data[0]
            pos_type = "LONG" if last_pos.positionType == PositionType.Long.value else "SHORT"
            open_time = datetime.fromtimestamp(last_pos.createTime / 1000).strftime('%Y-%m-%d %H:%M:%S')
            close_time = datetime.fromtimestamp(last_pos.updateTime / 1000).strftime('%Y-%m-%d %H:%M:%S')

            return f"""
<b>--- {account_id} Last Position Stats (CLOSED) ---</b>
<b>Pair:</b> <code>{last_pos.symbol}</code>
<b>Direction:</b> <code>{pos_type}</code>
<b>Margin Size:</b> <code>{last_pos.im:.2f} USDT</code>
<b>Open Time:</b> <code>{open_time}</code>
<b>Close Time:</b> <code>{close_time}</code>
<b>Open Price:</b> <code>{last_pos.openAvgPrice}</code>
<b>Close Price:</b> <code>{last_pos.closeAvgPrice}</code>
<b>Realized PNL:</b> <code>{last_pos.realised:.2f} USDT</code>
"""

        return f"<b>--- {account_id} Last Position Stats ---</b>\nNo open or historical position found."
    except Exception as e:
        logger.error(f"Error in get_last_position_stats for {account_id}: {e}")
        return f"Error fetching last position stats for {account_id}: {e}"


async def get_all_open_positions_stats(api: MexcFuturesAPI, account_id: str):
    """Fetches and formats details for ALL currently open positions across all pairs."""
    try:
        positions_data = await fetch_api_response(api, "get_open_positions", symbol=None)
        if not positions_data:
            return ""

        orders_data = await fetch_api_response(api, "get_current_pending_orders")
        pending_orders = orders_data or []

        stats_message = f"<b>--- {account_id} Open Positions ---</b>\n"
        for pos in positions_data:
            pos_type = "LONG" if pos.positionType == PositionType.Long.value else "SHORT"

            tp_order = next((
                order for order in pending_orders
                if order.positionId == pos.positionId and order.orderType == OrderType.PriceLimited
            ), None)

            tp_price_str = f"<code>{tp_order.price}</code>" if tp_order else "N/A"

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
        return f"Error fetching position stats for {account_id}: {e}"


# --- STATISTICS LOGIC (Pair-Specific for UI Buttons) ---

async def get_pair_market_info(api: MexcFuturesAPI, account_id: str, pair: str) -> str:
    """Fetches real-time market data for a specific pair."""
    ticker_data, contract_details_data = await asyncio.gather(
        fetch_api_response(api, "get_ticker", pair),
        fetch_api_response(api, "get_contract_details", pair)
    )

    if not ticker_data or not contract_details_data:
        return f"<b>--- {account_id} - Market Info ---</b>\nCould not fetch market data for <code>{pair}</code>."

    current_price = ticker_data.get('lastPrice')
    price_unit = contract_details_data.get('priceUnit')

    # Basic attempt to format price based on ticker info
    try:
        if price_unit:
            precision = len(str(price_unit).split('.')[-1])
            formatted_price = f"{float(current_price):.{precision}f}"
        else:
            formatted_price = str(current_price)
    except Exception:
        formatted_price = str(current_price)

    return f"""
<b>--- {account_id} - Market Info ---</b>
<b>Pair:</b> <code>{pair}</code>
<b>Price:</b> <code>{formatted_price}</code>
<b>Volume (24h):</b> <code>{ticker_data.get('volume24', 'N/A')}</code>
"""


async def get_last_position_for_pair(api: MexcFuturesAPI, account_id: str, pair: str):
    """Fetches and formats the last open or closed position for a specific pair."""
    try:
        # Check for open positions first
        open_positions_data = await fetch_api_response(api, "get_open_positions", symbol=pair)

        if open_positions_data:
            # Filter for the most recently created open position for this pair
            pos = max(open_positions_data, key=lambda p: p.createTime)
            pos_type = "LONG" if pos.positionType == PositionType.Long.value else "SHORT"

            pnl_value = 0.0
            ticker_data, contract_details_data = await asyncio.gather(
                fetch_api_response(api, "get_ticker", pair),
                fetch_api_response(api, "get_contract_details", pair)
            )

            if ticker_data and contract_details_data:
                current_price = float(ticker_data.get('lastPrice', 0))
                contract_size = float(contract_details_data.get('contractSize', 1))
                entry_price = float(pos.openAvgPrice)
                position_volume = float(pos.holdVol)

                if pos.positionType == PositionType.Long.value:
                    pnl_value = (current_price - entry_price) * position_volume * contract_size
                else:
                    pnl_value = (entry_price - current_price) * position_volume * contract_size

            return f"""
<b>--- {account_id} - {pair} Position (OPEN) ---</b>
<b>Direction:</b> <code>{pos_type}</code>
<b>Margin Size:</b> <code>{pos.im:.2f} USDT</code>
<b>Open Time:</b> <code>{datetime.fromtimestamp(pos.createTime / 1000).strftime('%Y-%m-%d %H:%M:%S')}</code>
<b>Unrealized PNL:</b> <code>{pnl_value:.2f} USDT</code>
"""

        # If no open positions, check historical positions
        history_data = await fetch_api_response(api, "get_historical_positions", symbol=pair, page_size=1)
        if history_data:
            last_pos = history_data[0]
            pos_type = "LONG" if last_pos.positionType == PositionType.Long.value else "SHORT"

            return f"""
<b>--- {account_id} - {pair} Position (CLOSED) ---</b>
<b>Direction:</b> <code>{pos_type}</code>
<b>Margin Size:</b> <code>{last_pos.im:.2f} USDT</code>
<b>Open Time:</b> <code>{datetime.fromtimestamp(last_pos.createTime / 1000).strftime('%Y-%m-%d %H:%M:%S')}</code>
<b>Close Time:</b> <code>{datetime.fromtimestamp(last_pos.updateTime / 1000).strftime('%Y-%m-%d %H:%M:%S')}</code>
<b>Realized PNL:</b> <code>{last_pos.realised:.2f} USDT</code>
"""

        return f"<b>--- {account_id} - {pair} Position ---</b>\nNo position history found for <code>{pair}</code>."
    except Exception as e:
        logger.error(f"Error in get_last_position_for_pair for {account_id} {pair}: {e}")
        return f"Error fetching stats for {account_id} on <code>{pair}</code>: {e}"


# --- TELEGRAM COMMAND HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("MEXC Stats Bot online. Select a token or use a command below:",
                                    reply_markup=KEYBOARD_MARKUP)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    account_list = "\n".join([f"- {acc['account_id']}" for acc in STATS_ACCOUNTS])
    if not account_list:
        account_list = "- No accounts configured (Check STATS_BOTx in .env)"

    help_text = f"""<b>Commands:</b>
Click a **TOKEN BUTTON** to get its market price and your last position on that pair.
<code>/stats</code> - Gets account PNL and the very last open/closed position across all pairs for all configured accounts (Global Stats).
<code>/positions</code> - Shows details of ALL currently open positions across all configured accounts.
<code>/token [PAIR]</code> - Manually request stats for a pair (e.g., /token ETH_USDT).

<b>Configured Accounts:</b>
{account_list}
"""
    await update.message.reply_text(help_text, reply_markup=KEYBOARD_MARKUP)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not API_CLIENTS:
        await update.message.reply_text("Error: No API clients configured. Check STATS_BOTx_TOKEN in your .env file.",
                                        reply_markup=KEYBOARD_MARKUP)
        return

    await update.message.reply_text("Fetching GLOBAL stats for all configured accounts...",
                                    reply_markup=KEYBOARD_MARKUP)

    stats_tasks = []
    for account in STATS_ACCOUNTS:
        api = API_CLIENTS[account['account_id']]
        stats_tasks.append(get_account_stats(api, account['account_id']))
        stats_tasks.append(get_last_position_stats(api, account['account_id']))

    results = await asyncio.gather(*stats_tasks)

    combined_message = "\n\n".join(results)
    await update.message.reply_text(f"<b>Overall Account Statistics:</b>\n{combined_message}",
                                    reply_markup=KEYBOARD_MARKUP)


async def positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not API_CLIENTS:
        await update.message.reply_text("Error: No API clients configured. Check STATS_BOTx_TOKEN in your .env file.",
                                        reply_markup=KEYBOARD_MARKUP)
        return

    await update.message.reply_text("Fetching ALL OPEN positions for all configured accounts...",
                                    reply_markup=KEYBOARD_MARKUP)

    position_tasks = []
    for account in STATS_ACCOUNTS:
        api = API_CLIENTS[account['account_id']]
        position_tasks.append(get_all_open_positions_stats(api, account['account_id']))

    results = await asyncio.gather(*position_tasks)

    open_positions_messages = [msg for msg in results if msg.strip()]

    if not open_positions_messages:
        await update.message.reply_text("No open positions found across all configured accounts.",
                                        reply_markup=KEYBOARD_MARKUP)
    else:
        combined_message = "\n\n".join(open_positions_messages)
        await update.message.reply_text(f"<b>Open Positions Summary:</b>\n{combined_message}",
                                        reply_markup=KEYBOARD_MARKUP)


async def token_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles token button presses and manual /token commands."""

    # Check if the command was triggered by the /token command or the text handler
    if update.message.text.startswith('/'):
        # Triggered by /token
        if context.args:
            pair = context.args[0].upper()
        else:
            await update.message.reply_text("Please specify a token (e.g., BTC_USDT) or press a button.",
                                            reply_markup=KEYBOARD_MARKUP)
            return
    else:
        # Triggered by plain text (button press)
        pair = update.message.text.strip().upper()

    if not pair or '_' not in pair:
        await update.message.reply_text(
            f"Invalid pair format: <code>{pair}</code>. Please use the format TOKEN_QUOTE (e.g., BTC_USDT).",
            reply_markup=KEYBOARD_MARKUP)
        return

    await update.message.reply_text(f"Fetching focused stats for <code>{pair}</code> across all accounts...",
                                    reply_markup=KEYBOARD_MARKUP)

    tasks = []
    for account in STATS_ACCOUNTS:
        api = API_CLIENTS[account['account_id']]
        account_id = account['account_id']

        # Add tasks to fetch market info and last position for the specific pair
        tasks.append(get_pair_market_info(api, account_id, pair))
        tasks.append(get_last_position_for_pair(api, account_id, pair))

    results = await asyncio.gather(*tasks)

    # Filter out redundant info messages from separate fetches
    results = [r for r in results if r]

    combined_message = "\n\n".join(results)

    await update.message.reply_text(f"<b>Token Stats Summary:</b>\n{combined_message}", reply_markup=KEYBOARD_MARKUP)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles text input that isn't a command, primarily from Reply Keyboard buttons."""
    text = update.message.text.strip()

    # A simple check to see if the text is a pair (TOKEN_QUOTE)
    # Allows numbers and letters, but requires an underscore
    if '_' in text and text.replace('_', '').isalnum():
        # User pressed a token button, redirect to the token_stats_command
        # We don't use context.args here; we pass the text directly.
        await token_stats_command(update, context)
    else:
        # Ignore other non-command text
        await update.message.reply_text("Please use a command or select a token from the menu below.",
                                        reply_markup=KEYBOARD_MARKUP)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = """<b>Available Commands:</b>
<code>/stats</code>, <code>/positions</code>, <code>/help</code>, <code>/token</code>
"""
    await update.message.reply_text(f"Unknown command.\n\n{help_text}", reply_markup=KEYBOARD_MARKUP)


# --- BOT STARTUP ---
def main() -> None:
    defaults = Defaults(parse_mode=ParseMode.HTML)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).defaults(defaults).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("positions", positions_command))
    application.add_handler(CommandHandler("token", token_stats_command))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    print(f"Loaded {len(API_CLIENTS)} MEXC Futures API client(s).")
    print("Telegram Stats bot is running with default HTML parse mode.")
    application.run_polling()


if __name__ == "__main__":
    main()