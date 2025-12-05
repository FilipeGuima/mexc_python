import asyncio
import logging
import re
from datetime import datetime, timezone
from telethon import TelegramClient, events

# --- IMPORTS ---
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import (
    OrderSide, CreateOrderRequest, OpenType, OrderType,
    TriggerOrderRequest, TriggerType, TriggerPriceType, ExecuteCycle
)
from mexcpy.config import API_ID, API_HASH, TARGET_CHATS, BREAKEVEN_TOKEN, SESSION_BREAKEVEN

# --- CONFIGURATION ---
if not BREAKEVEN_TOKEN or not API_ID:
    print(" ERROR: Credentials missing. Please check your .env file.")
    exit(1)

START_TIME = datetime.now(timezone.utc)

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

IS_TESTNET = False

MexcAPI = MexcFuturesAPI(token=BREAKEVEN_TOKEN, testnet=IS_TESTNET)
client = TelegramClient(str(SESSION_BREAKEVEN), API_ID, API_HASH)

# --- HELPER FUNCTIONS ---
def adjust_price_to_step(price, step_size):
    if not price:
        return None
    if not step_size or step_size == 0:
        return price

    step_str = f"{float(step_size):.16f}".rstrip('0')
    precision = 0
    if '.' in step_str:
        precision = len(step_str.split('.')[1])

    return round(price, precision)

# --- TRADE LOGIC ---

async def move_sl_to_entry(symbol: str):
    logger.info(f"Processing 'Move SL to Entry' for {symbol}...")

    pos_res = await MexcAPI.get_open_positions(symbol)
    if not pos_res.success:
        return f" API Error checking positions: {pos_res.message}"

    if not pos_res.data:
        return f"  Cannot Move SL: No open position found for {symbol}"

    position = pos_res.data[0]
    entry_price = float(position.openAvgPrice)
    vol = position.holdVol
    leverage = position.leverage
    pos_type = position.positionType  # 1=Long, 2=Short

    contract_res = await MexcAPI.get_contract_details(symbol)
    price_step = contract_res.data.get('priceUnit') if contract_res.success else 0.01
    final_sl_price = adjust_price_to_step(entry_price, price_step)

    existing_sl_order_id = None
    existing_tp_price = None

    print(f"    Scanning for SL on {symbol} (Entry: {entry_price})...")

    for attempt in range(3):
        stops_res = await MexcAPI.get_stop_limit_orders(symbol=symbol, is_finished=0)

        if stops_res.success and stops_res.data:
            for order in stops_res.data:
                is_dict = isinstance(order, dict)
                o_id = order.get('id') if is_dict else order.id

                t_price = (
                              order.get('triggerPrice') if is_dict else getattr(order, 'triggerPrice', None)
                          ) or (
                              order.get('stopLossPrice') if is_dict else getattr(order, 'stopLossPrice', None)
                          ) or (
                              order.get('price') if is_dict else getattr(order, 'price', None)
                          )

                tp_val = order.get('takeProfitPrice') if is_dict else getattr(order, 'takeProfitPrice', None)

                if t_price is None: continue
                t_price = float(t_price)

                is_sl = False
                if pos_type == 1:
                    if t_price < entry_price:
                        is_sl = True
                elif pos_type == 2:
                    if t_price > entry_price:
                        is_sl = True

                if is_sl:
                    existing_sl_order_id = o_id
                    existing_tp_price = tp_val
                    break

        if existing_sl_order_id:
            break
        await asyncio.sleep(1.5)

    if existing_sl_order_id:
        print(f"   Identified SL (ID: {existing_sl_order_id}). Editing...")

        res = await MexcAPI.update_stop_limit_trigger_plan_price(
            stop_plan_order_id=existing_sl_order_id,
            stop_loss_price=final_sl_price,
            take_profit_price=existing_tp_price
        )

        if res.success:
            tp_msg = f" (TP kept at {existing_tp_price})" if existing_tp_price else " (No TP found)"
            return f"  **SL Updated to Entry!**\n   Symbol: {symbol}\n   New SL: {final_sl_price}\n  {tp_msg}"

        print(f"    Edit Failed ({res.message}). executing CANCEL & REPLACE strategy...")
        await MexcAPI.cancel_stop_limit_order(stop_plan_order_id=existing_sl_order_id)

    else:
        print("    No existing SL identified. Creating NEW SL...")

    if pos_type == 1:  # Long
        sl_side = OrderSide.CloseLong
        trigger_type = TriggerType.LessThanOrEqual
    else:
        sl_side = OrderSide.CloseShort
        trigger_type = TriggerType.GreaterThanOrEqual

    sl_req = TriggerOrderRequest(
        symbol=symbol,
        side=sl_side,
        vol=vol,
        openType=OpenType.Cross,
        triggerType=trigger_type,
        triggerPrice=final_sl_price,
        leverage=leverage,
        orderType=OrderType.MarketOrder,
        executeCycle=ExecuteCycle.UntilCanceled,
        trend=TriggerPriceType.LatestPrice
    )
    res = await MexcAPI.create_trigger_order(sl_req)

    if res.success:
        return f"  **SL Created at Entry!** (New Order)\n   Symbol: {symbol}\n   New SL: {final_sl_price}"
    else:
        return f"  **Failed to Create SL**\n   Error: {res.message}"


async def monitor_trade(symbol: str, start_vol: int, targets: list):
    import time
    print(f" Auto-monitoring started for {symbol}...")

    last_vol = start_vol
    first_run = True
    tp1_target = targets[0] if targets else None

    while True:
        await asyncio.sleep(5)

        try:
            pos_res = await MexcAPI.get_open_positions(symbol)

            if not pos_res.success:
                await asyncio.sleep(5)
                continue

            if not pos_res.data:
                await asyncio.sleep(2)

                stop_res = await MexcAPI.get_stop_limit_orders(symbol=symbol, is_finished=1, page_size=5)
                reason = "Manual Close / Unknown"

                if stop_res.success and stop_res.data:
                    def get_time(item):
                        return item.get('updateTime') if isinstance(item, dict) else item.updateTime

                    sorted_stops = sorted(stop_res.data, key=get_time, reverse=True)

                    if sorted_stops:
                        last_stop = sorted_stops[0]
                        if isinstance(last_stop, dict):
                            up_time = last_stop.get('updateTime', 0)
                            state_val = last_stop.get('state')
                            trig_price = float(last_stop.get('triggerPrice', 0))
                        else:
                            up_time = last_stop.updateTime
                            state_val = last_stop.state.value
                            trig_price = float(last_stop.triggerPrice)

                        time_diff = (time.time() * 1000) - up_time

                        if state_val == 3 and time_diff < 120000:
                            if tp1_target and abs(trig_price - tp1_target) / tp1_target < 0.005:
                                reason = f" **TAKE PROFIT HIT** (Target: {tp1_target})"
                            else:
                                reason = f" **STOP LOSS HIT** (Trigger: {trig_price})"

                msg = f" **{symbol} Closed!**\nReason: {reason}\n Cleanup done."
                print(f"\n{msg}\n---------------------------------------")
                await client.send_message('me', msg)
                await MexcAPI.cancel_all_orders(symbol=symbol)
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
            print(f" Monitor Exception for {symbol}: {e}")
            await asyncio.sleep(5)

# --- PARSER ---
def parse_signal(text: str):
    try:
        text = text.encode('ascii', 'ignore').decode('ascii')

        text_clean = re.sub(r'[^a-zA-Z0-9\s.:,/%-]', '', text)

        text_clean = re.sub(r'\s+', ' ', text_clean).strip()

        print(f" DEBUG CLEANED: {text_clean}")

        if "TARGET HIT" in text_clean.upper() or "PROFIT:" in text_clean.upper():
            return {'valid': False, 'error': 'Ignored: Status/Profit update message'}

        pair_match = re.search(r"PAIR:\s*([A-Z0-9]+)[/_]([A-Z0-9]+)", text_clean, re.IGNORECASE)
        if not pair_match:
            return {'valid': False, 'error': 'Parsing Failed: No PAIR found in message'}

        symbol = f"{pair_match.group(1)}_{pair_match.group(2)}".upper()

        if "MOVE SL TO ENTRY" in text_clean.upper():
            return {
                'valid': True,
                'type': 'BREAKEVEN',
                'symbol': symbol
            }

        side_match = re.search(r"SIDE:\s*(LONG|SHORT)", text_clean, re.IGNORECASE)

        if not side_match:
            return {'valid': False, 'error': f"Parsing Failed: Pair {symbol} found, but missing SIDE (LONG/SHORT)"}

        # --- 4. Parse Trade Details ---
        direction = side_match.group(1).upper()
        side = OrderSide.OpenLong if direction == "LONG" else OrderSide.OpenShort

        # Size
        size_match = re.search(r"SIZE:\s*(\d+)(?:-\s*(\d+))?%", text_clean, re.IGNORECASE)
        equity_perc = 1.0
        if size_match:
            val1 = float(size_match.group(1))
            val2 = float(size_match.group(2)) if size_match.group(2) else val1
            equity_perc = (val1 + val2) / 2

        # Entry
        entry_match = re.search(r"ENTRY:\s*([\d.]+)", text_clean, re.IGNORECASE)
        entry_label = float(entry_match.group(1)) if entry_match else "Market"

        # SL
        sl_match = re.search(r"SL:\s*([\d.]+)", text_clean, re.IGNORECASE)
        sl_price = float(sl_match.group(1)) if sl_match else None

        # Leverage
        lev_match = re.search(r"LEVERAGE:\s*(\d+)", text_clean, re.IGNORECASE)
        leverage = int(lev_match.group(1)) if lev_match else 20

        # TPs
        all_tps = re.findall(r"TP\d:\s*([\d.]+)", text_clean, re.IGNORECASE)
        real_tps = []
        if all_tps:
            limit = 3 if len(all_tps) >= 3 else len(all_tps)
            real_tps = [float(tp) for tp in all_tps[:limit]]

        return {
            'valid': True,
            'type': 'TRADE',
            'symbol': symbol,
            'side': side,
            'equity_perc': equity_perc,
            'leverage': leverage,
            'entry': entry_label,
            'sl': sl_price,
            'tps': real_tps
        }

    except Exception as e:
        logger.error(f"Parse Exception: {e}")
        return {'valid': False, 'error': f"Exception during parsing: {str(e)}"}


async def execute_signal_trade(data):
    symbol = data['symbol']
    leverage = data['leverage']
    side = data['side']
    equity_perc = data['equity_perc']
    entry_label = data['entry']

    sl_price_raw = data.get('sl')
    tp1_price_raw = data['tps'][0] if data['tps'] else None

    quote_currency = symbol.split('_')[1]

    assets_response = await MexcAPI.get_user_assets()
    if not assets_response.success:
        return f"  API Token Failure: Could not fetch assets ({assets_response.message})"

    target_asset = next((a for a in assets_response.data if a.currency == quote_currency), None)
    if not target_asset:
        return f"  Wallet Error: {quote_currency} not found in wallet."

    balance = target_asset.availableBalance

    ticker_res = await MexcAPI.get_ticker(symbol)
    if not ticker_res.success or not ticker_res.data:
        return f"  Pair Error: Ticker data for {symbol} unavailable. Pair may not exist."

    current_price = ticker_res.data.get('lastPrice')

    contract_res = await MexcAPI.get_contract_details(symbol)
    if not contract_res.success:
        return f"  Contract Error: Could not fetch details for {symbol}. ({contract_res.message})"

    contract_size = contract_res.data.get('contractSize')
    price_step = contract_res.data.get('priceUnit')

    margin_amount = balance * (equity_perc / 100.0)
    position_value = margin_amount * leverage
    vol = int(position_value / (contract_size * current_price))

    if vol == 0:
        return f"  Volume Error: Calculated volume is 0. (Bal: {balance:.2f} {quote_currency}, Lev: {leverage})"

    logger.info(f" Executing {side.name} {symbol} x{leverage} | Vol: {vol}")

    final_sl_price = adjust_price_to_step(sl_price_raw, price_step)
    final_tp_price = adjust_price_to_step(tp1_price_raw, price_step)

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

    order_res = await MexcAPI.create_order(open_req)
    if not order_res.success:
        return f"  Execution Failed: {order_res.message}"

    asyncio.create_task(monitor_trade(symbol, vol, data['tps']))

    return (
        f"  **SUCCESS: {symbol} {side.name} x{leverage}**\n"
        f"---------------------------------------\n"
        f" Pos Size: {equity_perc}% (Midpoint) | Vol: {vol}\n"
        f" Entry: {entry_label}\n"
        f" SL: {final_sl_price} (Set on Position )\n"
        f" TP1: {final_tp_price} (Set on Position )\n"
        f" Note: TP2/TP3 ignored. Monitor running."
    )

@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    if event.date < START_TIME:
        return

    text = event.text

    if "PAIR:" in text.upper():
        print(f"\n--- Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

        # print(f" RAW HIDDEN CHECK: {repr(text)}")

        result = parse_signal(text)

        if not result['valid']:
            if "Ignored" in result['error']:
                print(f" -> {result['error']}")
            else:
                print(f"  {result['error']}")
            return

        symbol = result['symbol']

        if result['type'] == 'BREAKEVEN':
            print(f"  Processing BREAKEVEN for {symbol}...")
            res = await move_sl_to_entry(symbol)
            print(res)
            await client.send_message('me', res)

        elif result['type'] == 'TRADE':
            print(f"  Processing TRADE for {symbol}...")
            res = await execute_signal_trade(result)
            print(res)
            await client.send_message('me', res)


if __name__ == "__main__":
    print("--------------------------------------")
    print(" USER LISTENER STARTED ( BREAKEVEN MODE)")
    print(f" Start Time (UTC): {START_TIME}")
    print(f" Watching Chats: {TARGET_CHATS}")
    print("---------------------------------------")
    client.start()

    TESTNET_STATUS = "TRUE" if IS_TESTNET else "FALSE"
    print(f"\nTESTNET STATUS: {TESTNET_STATUS}")

    client.run_until_disconnected()