import asyncio
import logging
import re
import math
import time
from datetime import datetime, timezone
from telethon import TelegramClient, events

# --- IMPORTS ---
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import (
    OrderSide, CreateOrderRequest, OpenType, OrderType,
    TriggerOrderRequest, TriggerType, TriggerPriceType, ExecuteCycle
)
from mexcpy.config import API_ID, API_HASH, TARGET_CHATS, TP1_TOKEN, SESSION_TP1, MEXC_TESTNET
from common.parser import SignalParser, UpdateParser, parse_signal
from common.utils import adjust_price_to_step, validate_signal_tp_sl

START_TIME = datetime.now(timezone.utc)

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

MexcAPI = MexcFuturesAPI(token=TP1_TOKEN, testnet=MEXC_TESTNET)
client = TelegramClient(str(SESSION_TP1), API_ID, API_HASH)


async def monitor_trade(symbol: str, start_vol: int, targets: list, is_limit_order: bool = False):
    """
    Monitors an open position for TP/SL hits.
    If is_limit_order=True, first waits for the limit order to fill before monitoring.
    """
    if is_limit_order:
        print(f" Waiting for limit order to fill for {symbol}...")
        fill_wait_count = 0
        max_wait_cycles = 720  # ~1 hour at 5s intervals

        while fill_wait_count < max_wait_cycles:
            await asyncio.sleep(5)
            fill_wait_count += 1

            try:
                pos_res = await MexcAPI.get_open_positions(symbol)
                if pos_res.success and pos_res.data:
                    print(f" Limit order FILLED for {symbol}! Position now open.")
                    start_vol = pos_res.data[0].holdVol
                    break

                # Check if order was cancelled
                orders_res = await MexcAPI.get_current_pending_orders(symbol=symbol)
                if orders_res.success:
                    has_pending = len(orders_res.data or []) > 0
                    if not has_pending:
                        # No position and no pending order = order was cancelled
                        print(f" Limit order for {symbol} was CANCELLED or EXPIRED. Stopping monitor.")
                        return

            except Exception as e:
                print(f" Error checking limit order status: {e}")

        else:
            print(f" Limit order for {symbol} did not fill within timeout. Stopping monitor.")
            return

    print(f" Auto-monitoring started for {symbol}... \n---------------------------------------")

    last_vol = start_vol
    first_run = True
    error_count = 0

    while True:
        await asyncio.sleep(5)

        try:
            pos_res = await MexcAPI.get_open_positions(symbol)

            if not pos_res.success:
                error_count += 1
                await asyncio.sleep(5)
                continue

            error_count = 0

            if not pos_res.data:
                await asyncio.sleep(2)
                stop_res = await MexcAPI.get_stop_limit_orders(
                    symbol=symbol,
                    is_finished=1,
                    page_size=5
                )

                reason = "Manual Close / Unknown"

                if stop_res.success and stop_res.data:
                    def get_time(item):
                        return item.get('updateTime') if isinstance(item, dict) else item.updateTime

                    sorted_stops = sorted(stop_res.data, key=get_time, reverse=True)

                    if sorted_stops:
                        last_stop = sorted_stops[0]
                        if isinstance(last_stop, dict):
                            up_time = last_stop.get('updateTime', 0)
                            state = last_stop.get('state')
                            trig_side = last_stop.get('triggerSide')
                            state_val = state if isinstance(state, int) else state.value
                            side_val = trig_side if isinstance(trig_side, int) else trig_side.value
                        else:
                            up_time = last_stop.updateTime
                            state_val = last_stop.state.value
                            side_val = last_stop.triggerSide.value

                        time_diff = (time.time() * 1000) - up_time

                        if state_val == 3 and time_diff < 120000:
                            if side_val == 2:
                                reason = "**TAKE PROFIT HIT**"
                            elif side_val == 1:
                                reason = " **STOP LOSS HIT**"

                msg = f" **{symbol} Closed!**\nReason: {reason}\n"
                print(f"\n{msg}\n---------------------------------------")
                # await client.send_message('me', msg)
                break

            position = pos_res.data[0]
            current_vol = position.holdVol

            if first_run:
                last_vol = current_vol
                first_run = False
                continue

            if current_vol != last_vol:
                last_vol = current_vol

        except Exception as e:
            import traceback
            print(f" Monitor Exception for {symbol}: {e}")
            traceback.print_exc()
            await asyncio.sleep(5)


# --- EXECUTION FUNCTIONS ---

async def execute_signal_trade(data):
    """
    Executes a NEW trade with smart entry logic:
    - LONG: If current price <= entry price -> market order (getting better price)
            If current price > entry price -> limit order at entry (wait for pullback)
    - SHORT: If current price >= entry price -> market order (getting better price)
             If current price < entry price -> limit order at entry (wait for bounce)
    - Market entry: Always execute immediately
    """
    symbol = data['symbol']
    leverage = data['leverage']
    side = data['side']
    equity_perc = data['equity_perc']
    entry_value = data['entry']

    sl_price_raw = data.get('sl')
    tp1_price_raw = data['tps'][0] if data['tps'] else None

    quote_currency = symbol.split('_')[1]

    assets_response = await MexcAPI.get_user_assets()
    if not assets_response.success:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {symbol}\n"
            f"   Reason: API ERROR\n"
            f"   \n"
            f"   Could not fetch account assets.\n"
            f"   Error: {assets_response.message}\n"
            f"{'='*50}"
        )

    target_asset = next((a for a in assets_response.data if a.currency == quote_currency), None)
    if not target_asset:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {symbol}\n"
            f"   Reason: WALLET ERROR\n"
            f"   \n"
            f"   {quote_currency} balance not found in account.\n"
            f"   Please ensure you have {quote_currency} available for trading.\n"
            f"{'='*50}"
        )

    balance = target_asset.availableBalance

    ticker_res = await MexcAPI.get_ticker(symbol)
    if not ticker_res.success or not ticker_res.data:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {symbol}\n"
            f"   Reason: PRICE FETCH ERROR\n"
            f"   \n"
            f"   Could not fetch ticker data for {symbol}.\n"
            f"   Error: {ticker_res.message or 'No data returned'}\n"
            f"   \n"
            f"   This symbol may not exist or market may be closed.\n"
            f"{'='*50}"
        )
    current_price = ticker_res.data.get('lastPrice')

    contract_res = await MexcAPI.get_contract_details(symbol)
    if not contract_res.success:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {symbol}\n"
            f"   Reason: CONTRACT ERROR\n"
            f"   \n"
            f"   Could not fetch contract details for {symbol}.\n"
            f"   Error: {contract_res.message}\n"
            f"{'='*50}"
        )

    contract_size = contract_res.data.get('contractSize')
    real_tick_size = contract_res.data.get('priceUnit')
    price_step = real_tick_size

    print(f" DEBUG: {symbol} Tick Size: {price_step} | Current Price: {current_price}")

    margin_amount = balance * (equity_perc / 100.0)
    position_value = margin_amount * leverage
    vol = int(position_value / (contract_size * current_price))

    if vol == 0:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {symbol}\n"
            f"   Reason: INSUFFICIENT BALANCE\n"
            f"   \n"
            f"   Calculated volume is 0 contracts.\n"
            f"   \n"
            f"   Account Details:\n"
            f"   • Balance: {balance:.2f} {quote_currency}\n"
            f"   • Size: {equity_perc}%\n"
            f"   • Leverage: x{leverage}\n"
            f"   \n"
            f"   Increase balance or position size percentage.\n"
            f"{'='*50}"
        )

    final_sl_price = adjust_price_to_step(sl_price_raw, price_step)
    final_tp_price = adjust_price_to_step(tp1_price_raw, price_step)

    # Determine order type based on entry logic
    use_market_order = True
    entry_price = None
    order_reason = "Market Entry"

    if entry_value != "Market" and isinstance(entry_value, (int, float)):
        entry_price = adjust_price_to_step(entry_value, price_step)
        is_long = side == OrderSide.OpenLong

        if is_long:
            if current_price <= entry_price:
                # Current price is at or below entry - buy now for better entry
                use_market_order = True
                order_reason = f"MARKET (price {current_price} <= entry {entry_price})"
            else:
                # Current price is above entry - use limit order to wait for pullback
                use_market_order = False
                order_reason = f"LIMIT @ {entry_price} (waiting for pullback from {current_price})"
        else:  # SHORT
            if current_price >= entry_price:
                # Current price is at or above entry - sell now for better entry
                use_market_order = True
                order_reason = f"MARKET (price {current_price} >= entry {entry_price})"
            else:
                # Current price is below entry - use limit order to wait for bounce
                use_market_order = False
                order_reason = f"LIMIT @ {entry_price} (waiting for bounce from {current_price})"

    logger.info(f" Executing {side.name} {symbol} x{leverage} | Vol: {vol} | {order_reason}")

    if use_market_order:
        open_req = CreateOrderRequest(
            symbol=symbol,
            side=side,
            vol=vol,
            leverage=leverage,
            openType=OpenType.Cross,
            type=OrderType.MarketOrder,
            stopLossPrice=final_sl_price,
            takeProfitPrice=final_tp_price
        )
    else:
        open_req = CreateOrderRequest(
            symbol=symbol,
            side=side,
            vol=vol,
            leverage=leverage,
            openType=OpenType.Cross,
            type=OrderType.PriceLimited,
            price=entry_price,
            stopLossPrice=final_sl_price,
            takeProfitPrice=final_tp_price
        )

    order_res = await MexcAPI.create_order(open_req)
    if not order_res.success:
        order_type_str = "MARKET" if use_market_order else f"LIMIT @ {entry_price}"
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER FAILED** - {symbol}\n"
            f"   Side: {side.name}\n"
            f"   \n"
            f"   API Error: {order_res.message}\n"
            f"   \n"
            f"   Order Details:\n"
            f"   • Type: {order_type_str}\n"
            f"   • Volume: {vol}\n"
            f"   • Leverage: x{leverage}\n"
            f"   • TP1: {final_tp_price or 'None'}\n"
            f"   • SL: {final_sl_price or 'None'}\n"
            f"{'='*50}"
        )

    asyncio.create_task(monitor_trade(symbol, vol, data['tps'], is_limit_order=not use_market_order))

    order_type_str = "MARKET" if use_market_order else f"LIMIT @ {entry_price}"
    return (
        f" SUCCESS: {symbol} {side.name} x{leverage}\n"
        f"---------------------------------------\n"
        f" Vol: {vol} | Order: {order_type_str}\n"
        f" SL: {final_sl_price} | TP1: {final_tp_price}\n"
        f" Reason: {order_reason}\n"
    )


async def execute_update_signal(data):
    """
    Executes an UPDATE by MODIFYING the existing Position TP/SL order.
    Handles both regular stop orders and position plan orders (Side=0).
    """
    symbol = data['symbol']
    update_type = data['type']
    new_price_raw = data['price']

    print(f"  PROCESSING UPDATE: {symbol} {update_type} -> {new_price_raw}")

    pos_res = await MexcAPI.get_open_positions(symbol)
    if not pos_res.success or not pos_res.data:
        return f"  Update Ignored: No open position found for {symbol}"

    contract_res = await MexcAPI.get_contract_details(symbol)
    price_step = contract_res.data.get('priceUnit') if contract_res.success else 0.01
    final_price = adjust_price_to_step(new_price_raw, price_step)

    orders_res = await MexcAPI.get_stop_limit_orders(symbol=symbol, is_finished=0)

    target_order = None

    if orders_res.success and orders_res.data:
        print(f" DEBUG: Found {len(orders_res.data)} active stop-limit orders.")

        for order in orders_res.data:
            is_dict = isinstance(order, dict)

            order_id = order.get('orderId') if is_dict else getattr(order, 'orderId', None)
            plan_id = order.get('id') if is_dict else getattr(order, 'id', None)
            t_side = order.get('triggerSide') if is_dict else getattr(order, 'triggerSide', None)

            side_val = t_side.value if hasattr(t_side, 'value') else (t_side or 0)

            curr_sl = order.get('stopLossPrice') if is_dict else getattr(order, 'stopLossPrice', None)
            curr_tp = order.get('takeProfitPrice') if is_dict else getattr(order, 'takeProfitPrice', None)

            print(f"    - ID={plan_id or order_id} | Side={side_val} | SL={curr_sl} | TP={curr_tp}")

            # SELECTION LOGIC:
            # Side 0 = Position Plan Order (contains both TP and SL) - MOST COMMON
            # Side 1 = Take Profit Order only
            # Side 2 = Stop Loss Order only

            match_found = False

            if side_val == 0:
                match_found = True
                print("      -> Selected (Position TP/SL Plan)")
            elif update_type == 'SL' and side_val == 2:
                match_found = True
            elif 'TP' in update_type and side_val == 1:
                match_found = True

            if match_found:
                target_order = {
                    'order_id': order_id,
                    'plan_id': plan_id,
                    'side': side_val,
                    'curr_sl': curr_sl,
                    'curr_tp': curr_tp,
                }
                break
    else:
        print(f" 🔎 DEBUG: No active stop-limit orders found.")

    if not target_order:
        return f"  Update Skipped: No existing {update_type} order found to modify."

    use_plan_api = target_order['side'] == 0 or target_order['plan_id']
    order_id_to_use = target_order['plan_id'] or target_order['order_id']

    if update_type == 'SL':
        new_sl = final_price
        new_tp = target_order['curr_tp']
    else:
        new_sl = target_order['curr_sl']
        new_tp = final_price

    print(f" Modifying Order {order_id_to_use}: SL={new_sl}, TP={new_tp}")

    try:
        if use_plan_api:
            print(f"    Using: update_stop_limit_trigger_plan_price (Plan Order)")
            res = await MexcAPI.update_stop_limit_trigger_plan_price(
                stop_plan_order_id=order_id_to_use,
                stop_loss_price=new_sl,
                take_profit_price=new_tp
            )
        else:
            print(f"    Using: change_stop_limit_trigger_price (Regular Order)")
            res = await MexcAPI.change_stop_limit_trigger_price(
                order_id=order_id_to_use,
                stop_loss_price=new_sl,
                take_profit_price=new_tp
            )

        if res.success:
            return f" SUCCESS: {symbol} {update_type} updated to {final_price}"
        else:
            if use_plan_api:
                print(f"    Plan API failed ({res.message}), trying regular API...")
                res = await MexcAPI.change_stop_limit_trigger_price(
                    order_id=order_id_to_use,
                    stop_loss_price=new_sl,
                    take_profit_price=new_tp
                )
                if res.success:
                    return f"  SUCCESS: {symbol} {update_type} updated to {final_price}"

            return f"  MODIFICATION FAILED: {res.message}"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"  Error calling modification API: {e}"



# --- TELEGRAM HANDLER ---

@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    if event.date < START_TIME: return
    text = event.text
    if not text: return

    text_upper = text.upper()

    if "PAIR" in text_upper and "SIDE" in text_upper:
        print("\n  New Signal Detected!")
        signal_data = parse_signal(text)
        if signal_data:
            # Convert string side to MEXC OrderSide enum
            if signal_data.get('side') == "LONG":
                signal_data['side'] = OrderSide.OpenLong
            elif signal_data.get('side') == "SHORT":
                signal_data['side'] = OrderSide.OpenShort

            # Validate TP/SL
            validation_error = validate_signal_tp_sl(signal_data)
            if validation_error:
                print(validation_error)
                return
            result = await execute_signal_trade(signal_data)
            print(result)
        return

    if any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET"]) and "/" in text:
        print("\n  Update Signal Detected!")
        update_data = UpdateParser.parse(text)

        if update_data:
            result = await execute_update_signal(update_data)
            print(result)
        else:
            print(" Update detected but failed to parse details.")


if __name__ == "__main__":
    print("--------------------------------------")
    print(" USER LISTENER STARTED (TP1 MODE) ")
    print(f" Start Time: {START_TIME}")
    print(f" Watching Chats: {TARGET_CHATS}")
    print("---------------------------------------")
    async def startup():
        await client.start()

        print(" Checking MEXC API connection...")
        res = await MexcAPI.get_user_assets()
        if res.success:
            usdt = next((a for a in res.data if a.currency == "USDT"), None)
            bal = f"{usdt.availableBalance:.2f} USDT" if usdt else "No USDT found"
            print(f" API OK | Balance: {bal}")
        else:
            print(f" API FAILED: {res.message}")
            print(" WARNING: Bot will start but trades may fail!")

        TESTNET_STATUS = "TRUE" if MEXC_TESTNET else "FALSE"
        print(f"\nTESTNET STATUS: {TESTNET_STATUS}")

        await client.run_until_disconnected()

    client.loop.run_until_complete(startup())