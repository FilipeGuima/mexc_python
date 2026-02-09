import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, Defaults
from telegram.constants import ParseMode
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

# --- CLEAN IMPORTS ---
from bots.telegram.telegram_stats.exchange_adapter import (
    ExchangeAdapter, create_adapter,
    StatsAssetInfo, StatsPositionInfo, StatsOrderInfo, StatsTickerInfo, StatsContractInfo,
)

from mexcpy.config import TELEGRAM_BOT_TOKEN, STATS_ACCOUNTS

# --- CONFIGURATION CHECKS ---
if not TELEGRAM_BOT_TOKEN:
    print(" ERROR: TELEGRAM_BOT_TOKEN not found in .env via mexcpy.config")
    exit(1)

if not STATS_ACCOUNTS:
    print(" WARNING: No STATS_BOTx accounts defined in .env. Stats functionality will be limited.")

# --- SETUP ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize a dictionary of adapters (one for each configured bot)
API_CLIENTS: Dict[str, ExchangeAdapter] = {
    account['account_id']: create_adapter(account)
    for account in STATS_ACCOUNTS
}

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


# --- HELPER: Truncate to Telegram limit ---

def safe_truncate(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    truncated = text[:max_len - 30]
    # Try to cut at last newline for cleaner output
    last_nl = truncated.rfind('\n')
    if last_nl > max_len // 2:
        truncated = truncated[:last_nl]
    return truncated + "\n\n<i>... truncated</i>"


# --- KEYBOARD BUILDERS ---

def build_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Equity", callback_data="equity:all"),
            InlineKeyboardButton("Alerts", callback_data="alerts"),
        ],
        [
            InlineKeyboardButton("Trades", callback_data="trades:all"),
            InlineKeyboardButton("History", callback_data="history:all"),
        ],
    ])


def build_section_keyboard(prefix: str, include_all: bool = True) -> InlineKeyboardMarkup:
    """Build keyboard with per-account filter buttons + Refresh + Back."""
    account_buttons = []
    row = []
    for acc in STATS_ACCOUNTS:
        aid = acc['account_id']
        row.append(InlineKeyboardButton(aid, callback_data=f"{prefix}:{aid}"))
        if len(row) == 3:
            account_buttons.append(row)
            row = []
    if row:
        account_buttons.append(row)

    bottom_row = []
    if include_all:
        bottom_row.append(InlineKeyboardButton("All Accounts", callback_data=f"{prefix}:all"))
    bottom_row.append(InlineKeyboardButton("Refresh", callback_data=f"{prefix}:all" if include_all else prefix))
    bottom_row.append(InlineKeyboardButton("<< Dashboard", callback_data="dash"))

    account_buttons.append(bottom_row)
    return InlineKeyboardMarkup(account_buttons)


def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("<< Dashboard", callback_data="dash")]
    ])


# --- STATISTICS LOGIC (Global and Pair-Agnostic) ---

async def get_account_stats(adapter: ExchangeAdapter, account_id: str):
    """
    Fetches and formats general account statistics including PNL history,
    dynamically determining the margin currency.
    """
    try:
        assets_response_data = await adapter.get_assets()
        if not assets_response_data:
            return f"<b>--- {account_id} ({adapter.exchange_name}) ---</b>\nCould not fetch account assets."

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
            primary_asset = max(assets_response_data, key=lambda a: a.equity)

        if not primary_asset:
            return f"<b>--- {account_id} ({adapter.exchange_name}) ---</b>\nNo primary asset found in wallet."

        display_currency = primary_asset.currency

        all_positions = []
        page_num = 1
        page_size = 100
        thirty_days_ago = datetime.now() - timedelta(days=30)

        while True:
            history_data = await adapter.get_historical_positions(
                symbol=None, page_num=page_num, page_size=page_size
            )

            if not history_data:
                break

            all_positions.extend(history_data)

            last_trade_in_page = history_data[-1]
            last_trade_time = datetime.fromtimestamp(last_trade_in_page.updateTime)

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
                try:
                    realised_pnl = float(position.realised)
                    close_time = datetime.fromtimestamp(position.updateTime)
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
<b>--- {account_id} ({adapter.exchange_name}) ---</b>
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


async def get_last_position_stats(adapter: ExchangeAdapter, account_id: str):
    """
    Fetches and formats the *absolute* last open or closed position for the account
    across all pairs.
    """
    try:
        # Check for ANY open positions first (symbol=None)
        open_positions_data = await adapter.get_open_positions(symbol=None)

        if open_positions_data:
            # Find the most recently opened position
            pos = max(open_positions_data, key=lambda p: p.createTime)
            pair = pos.symbol
            open_time = datetime.fromtimestamp(pos.createTime).strftime('%Y-%m-%d %H:%M:%S')

            # Compute unrealized PNL
            pnl_value = pos.unrealized
            if pnl_value == 0.0:
                # MEXC adapter returns 0 for unrealized — compute from ticker
                exchange_symbol = adapter.to_exchange_symbol(pair)
                ticker_data, contract_details_data = await asyncio.gather(
                    adapter.get_ticker(exchange_symbol),
                    adapter.get_contract_details(exchange_symbol)
                )

                if ticker_data and contract_details_data:
                    current_price = ticker_data.lastPrice
                    contract_size = contract_details_data.contractSize
                    entry_price = float(pos.openAvgPrice)
                    position_volume = float(pos.holdVol)

                    if pos.positionType == "LONG":
                        pnl_value = (current_price - entry_price) * position_volume * contract_size
                    else:
                        pnl_value = (entry_price - current_price) * position_volume * contract_size

            return f"""
<b>--- {account_id} ({adapter.exchange_name}) Last Position Stats (OPEN) ---</b>
<b>Pair:</b> <code>{pos.symbol}</code>
<b>Direction:</b> <code>{pos.positionType}</code>
<b>Margin Size:</b> <code>{pos.margin:.2f} USDT</code>
<b>Open Time:</b> <code>{open_time}</code>
<b>Unrealized PNL:</b> <code>{pnl_value:.2f} USDT</code>
"""

        # If no open positions, check historical positions (symbol=None)
        history_data = await adapter.get_historical_positions(symbol=None, page_size=1)
        if history_data:
            last_pos = history_data[0]
            open_time = datetime.fromtimestamp(last_pos.createTime).strftime('%Y-%m-%d %H:%M:%S')
            close_time = datetime.fromtimestamp(last_pos.updateTime).strftime('%Y-%m-%d %H:%M:%S')

            return f"""
<b>--- {account_id} ({adapter.exchange_name}) Last Position Stats (CLOSED) ---</b>
<b>Pair:</b> <code>{last_pos.symbol}</code>
<b>Direction:</b> <code>{last_pos.positionType}</code>
<b>Margin Size:</b> <code>{last_pos.margin:.2f} USDT</code>
<b>Open Time:</b> <code>{open_time}</code>
<b>Close Time:</b> <code>{close_time}</code>
<b>Open Price:</b> <code>{last_pos.openAvgPrice}</code>
<b>Close Price:</b> <code>{last_pos.closeAvgPrice}</code>
<b>Realized PNL:</b> <code>{last_pos.realised:.2f} USDT</code>
"""

        return f"<b>--- {account_id} ({adapter.exchange_name}) Last Position Stats ---</b>\nNo open or historical position found."
    except Exception as e:
        logger.error(f"Error in get_last_position_stats for {account_id}: {e}")
        return f"Error fetching last position stats for {account_id}: {e}"


async def get_all_open_positions_stats(adapter: ExchangeAdapter, account_id: str):
    """Fetches and formats details for ALL currently open positions across all pairs."""
    try:
        positions_data = await adapter.get_open_positions(symbol=None)
        if not positions_data:
            return ""

        pending_orders = await adapter.get_pending_tp_orders()
        pending_orders = pending_orders or []

        stats_message = f"<b>--- {account_id} ({adapter.exchange_name}) Open Positions ---</b>\n"
        for pos in positions_data:
            tp_order = next((
                order for order in pending_orders
                if order.positionId == pos.positionId
            ), None)

            tp_price_str = f"<code>{tp_order.price}</code>" if tp_order else "N/A"

            open_time = datetime.fromtimestamp(pos.createTime).strftime('%Y-%m-%d %H:%M:%S')

            stats_message += f"""
--------------------
<b>Pair:</b> <code>{pos.symbol}</code>
<b>Direction:</b> <code>{pos.positionType}</code>
<b>Volume:</b> <code>{pos.holdVol}</code> contracts
<b>Entry Price:</b> <code>{pos.openAvgPrice}</code>
<b>Take-Profit:</b> {tp_price_str}
<b>Margin:</b> <code>{pos.margin:.2f} USDT</code>
<b>Opened At:</b> <code>{open_time}</code>
"""
        return stats_message
    except Exception as e:
        return f"Error fetching position stats for {account_id}: {e}"


# --- STATISTICS LOGIC (Pair-Specific) ---

async def get_pair_market_info(adapter: ExchangeAdapter, account_id: str, pair: str) -> str:
    """Fetches real-time market data for a specific pair."""
    exchange_symbol = adapter.to_exchange_symbol(pair)
    ticker_data, contract_details_data = await asyncio.gather(
        adapter.get_ticker(exchange_symbol),
        adapter.get_contract_details(exchange_symbol)
    )

    if not ticker_data or not contract_details_data:
        return f"<b>--- {account_id} ({adapter.exchange_name}) - Market Info ---</b>\nCould not fetch market data for <code>{pair}</code>."

    current_price = ticker_data.lastPrice
    price_unit = contract_details_data.priceUnit

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
<b>--- {account_id} ({adapter.exchange_name}) - Market Info ---</b>
<b>Pair:</b> <code>{pair}</code>
<b>Price:</b> <code>{formatted_price}</code>
<b>Volume (24h):</b> <code>{ticker_data.volume24}</code>
"""


async def get_last_position_for_pair(adapter: ExchangeAdapter, account_id: str, pair: str):
    """Fetches and formats the last open or closed position for a specific pair."""
    try:
        exchange_symbol = adapter.to_exchange_symbol(pair)

        # Check for open positions first
        open_positions_data = await adapter.get_open_positions(symbol=pair)

        if open_positions_data:
            # Filter for the most recently created open position for this pair
            pos = max(open_positions_data, key=lambda p: p.createTime)

            # Compute unrealized PNL
            pnl_value = pos.unrealized
            if pnl_value == 0.0:
                ticker_data, contract_details_data = await asyncio.gather(
                    adapter.get_ticker(exchange_symbol),
                    adapter.get_contract_details(exchange_symbol)
                )

                if ticker_data and contract_details_data:
                    current_price = ticker_data.lastPrice
                    contract_size = contract_details_data.contractSize
                    entry_price = float(pos.openAvgPrice)
                    position_volume = float(pos.holdVol)

                    if pos.positionType == "LONG":
                        pnl_value = (current_price - entry_price) * position_volume * contract_size
                    else:
                        pnl_value = (entry_price - current_price) * position_volume * contract_size

            return f"""
<b>--- {account_id} ({adapter.exchange_name}) - {pair} Position (OPEN) ---</b>
<b>Direction:</b> <code>{pos.positionType}</code>
<b>Margin Size:</b> <code>{pos.margin:.2f} USDT</code>
<b>Open Time:</b> <code>{datetime.fromtimestamp(pos.createTime).strftime('%Y-%m-%d %H:%M:%S')}</code>
<b>Unrealized PNL:</b> <code>{pnl_value:.2f} USDT</code>
"""

        # If no open positions, check historical positions
        history_data = await adapter.get_historical_positions(symbol=pair, page_size=1)
        if history_data:
            last_pos = history_data[0]

            return f"""
<b>--- {account_id} ({adapter.exchange_name}) - {pair} Position (CLOSED) ---</b>
<b>Direction:</b> <code>{last_pos.positionType}</code>
<b>Margin Size:</b> <code>{last_pos.margin:.2f} USDT</code>
<b>Open Time:</b> <code>{datetime.fromtimestamp(last_pos.createTime).strftime('%Y-%m-%d %H:%M:%S')}</code>
<b>Close Time:</b> <code>{datetime.fromtimestamp(last_pos.updateTime).strftime('%Y-%m-%d %H:%M:%S')}</code>
<b>Realized PNL:</b> <code>{last_pos.realised:.2f} USDT</code>
"""

        return f"<b>--- {account_id} ({adapter.exchange_name}) - {pair} Position ---</b>\nNo position history found for <code>{pair}</code>."
    except Exception as e:
        logger.error(f"Error in get_last_position_for_pair for {account_id} {pair}: {e}")
        return f"Error fetching stats for {account_id} on <code>{pair}</code>: {e}"


# --- RENDER FUNCTIONS (return text + keyboard) ---

def render_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    account_list = ", ".join(
        f"{acc['account_id']} ({acc.get('exchange', 'mexc').upper()})" for acc in STATS_ACCOUNTS
    )
    if not account_list:
        account_list = "None configured"

    text = (
        "<b>Dashboard</b>\n\n"
        f"<b>Accounts:</b> {account_list}\n\n"
        "Select a section below:"
    )
    return text, build_dashboard_keyboard()


async def render_equity(target: str) -> Tuple[str, InlineKeyboardMarkup]:
    if not API_CLIENTS:
        return "No API clients configured.", build_back_keyboard()

    if target == "all":
        accounts = STATS_ACCOUNTS
    else:
        accounts = [acc for acc in STATS_ACCOUNTS if acc['account_id'] == target]
        if not accounts:
            return f"Account <code>{target}</code> not found.", build_back_keyboard()

    tasks = []
    for acc in accounts:
        adapter = API_CLIENTS[acc['account_id']]
        tasks.append(get_account_stats(adapter, acc['account_id']))
        tasks.append(get_last_position_stats(adapter, acc['account_id']))

    results = await asyncio.gather(*tasks)

    combined = "\n".join(r.strip() for r in results if r.strip())
    text = f"<b>Equity Overview</b>\n\n{combined}" if combined else "<b>Equity Overview</b>\n\nNo data available."
    return text, build_section_keyboard("equity")


async def render_alerts() -> Tuple[str, InlineKeyboardMarkup]:
    if not API_CLIENTS:
        return "No API clients configured.", build_back_keyboard()

    lines = ["<b>Alerts</b>\n"]

    for acc in STATS_ACCOUNTS:
        aid = acc['account_id']
        adapter = API_CLIENTS[aid]
        exchange = adapter.exchange_name

        # API health check
        assets = await adapter.get_assets()
        if assets is None:
            lines.append(f"<b>{aid} ({exchange}):</b> API connection failed / token expired")
        else:
            lines.append(f"<b>{aid} ({exchange}):</b> API OK")

        # Unfilled limit orders
        pending = await adapter.get_pending_limit_orders()
        if pending:
            lines.append(f"  Unfilled limit orders: <code>{len(pending)}</code>")
            for order in pending[:5]:  # Show max 5
                lines.append(f"    - {order.symbol} @ <code>{order.price}</code>")
            if len(pending) > 5:
                lines.append(f"    <i>... and {len(pending) - 5} more</i>")
        else:
            lines.append("  No unfilled limit orders")

        lines.append("")

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Refresh", callback_data="alerts"),
            InlineKeyboardButton("<< Dashboard", callback_data="dash"),
        ]
    ])
    return text, kb


async def render_trades(target: str) -> Tuple[str, InlineKeyboardMarkup]:
    if not API_CLIENTS:
        return "No API clients configured.", build_back_keyboard()

    if target == "all":
        accounts = STATS_ACCOUNTS
    else:
        accounts = [acc for acc in STATS_ACCOUNTS if acc['account_id'] == target]
        if not accounts:
            return f"Account <code>{target}</code> not found.", build_back_keyboard()

    tasks = [
        get_all_open_positions_stats(API_CLIENTS[acc['account_id']], acc['account_id'])
        for acc in accounts
    ]
    results = await asyncio.gather(*tasks)

    open_msgs = [msg for msg in results if msg.strip()]
    if not open_msgs:
        text = "<b>Open Positions</b>\n\nNo open positions found."
    else:
        combined = "\n".join(m.strip() for m in open_msgs)
        text = f"<b>Open Positions</b>\n\n{combined}"

    return text, build_section_keyboard("trades")


async def render_history(target: str) -> Tuple[str, InlineKeyboardMarkup]:
    if not API_CLIENTS:
        return "No API clients configured.", build_back_keyboard()

    if target == "all":
        accounts = STATS_ACCOUNTS
    else:
        accounts = [acc for acc in STATS_ACCOUNTS if acc['account_id'] == target]
        if not accounts:
            return f"Account <code>{target}</code> not found.", build_back_keyboard()

    lines = ["<b>Recent Closed Trades</b>\n"]

    for acc in accounts:
        aid = acc['account_id']
        adapter = API_CLIENTS[aid]
        exchange = adapter.exchange_name

        history = await adapter.get_historical_positions(page_size=10)
        if not history:
            lines.append(f"<b>{aid} ({exchange}):</b> No recent trades\n")
            continue

        lines.append(f"<b>--- {aid} ({exchange}) ---</b>")
        for pos in history:
            pnl = pos.realised
            pnl_icon = "+" if pnl >= 0 else ""
            close_time = datetime.fromtimestamp(pos.updateTime).strftime('%m-%d %H:%M')
            lines.append(
                f"  <code>{close_time}</code> {pos.symbol} {pos.positionType} "
                f"<code>{pnl_icon}{pnl:.2f}</code>"
            )
        lines.append("")

    text = "\n".join(lines)
    return text, build_section_keyboard("history")


# --- TELEGRAM HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, keyboard = render_dashboard()
    await update.message.reply_text(text, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    account_list = "\n".join([f"- {acc['account_id']} ({acc.get('exchange', 'mexc').upper()})" for acc in STATS_ACCOUNTS])
    if not account_list:
        account_list = "- No accounts configured (Check STATS_BOTx in .env)"

    help_text = f"""<b>Stats Bot Help</b>

<code>/start</code> or <code>/dash</code> - Open the dashboard
<code>/help</code> - Show this help message

Use the inline buttons to navigate between sections:
<b>Equity</b> - Account balance + PNL breakdown
<b>Alerts</b> - API health + unfilled limit orders
<b>Trades</b> - All currently open positions
<b>History</b> - Recent closed trades

<b>Configured Accounts:</b>
{account_list}
"""
    await update.message.reply_text(help_text)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if not data:
        return

    # Show loading state
    try:
        await query.edit_message_text("Loading...")
    except Exception:
        pass

    # Route callback data to render functions
    try:
        if data == "dash":
            text, keyboard = render_dashboard()

        elif data.startswith("equity:"):
            target = data.split(":", 1)[1]
            text, keyboard = await render_equity(target)

        elif data == "alerts":
            text, keyboard = await render_alerts()

        elif data.startswith("trades:"):
            target = data.split(":", 1)[1]
            text, keyboard = await render_trades(target)

        elif data.startswith("history:"):
            target = data.split(":", 1)[1]
            text, keyboard = await render_history(target)

        else:
            text, keyboard = render_dashboard()

        text = safe_truncate(text)
        await query.edit_message_text(text, reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error handling callback '{data}': {e}")
        try:
            await query.edit_message_text(
                f"An error occurred: <code>{e}</code>",
                reply_markup=build_back_keyboard()
            )
        except Exception:
            pass


# --- BOT STARTUP ---
def main() -> None:
    defaults = Defaults(parse_mode=ParseMode.HTML)
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).defaults(defaults).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("dash", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    exchange_summary = ", ".join(
        f"{acc['account_id']}({acc.get('exchange', 'mexc').upper()})" for acc in STATS_ACCOUNTS
    )
    print(f"Loaded {len(API_CLIENTS)} exchange adapter(s): {exchange_summary}")
    print("Telegram Stats bot is running with inline dashboard UI.")
    application.run_polling()


if __name__ == "__main__":
    main()
