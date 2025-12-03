import asyncio
import logging
import re
import math
import time
import os
from datetime import datetime, timezone
from telethon import TelegramClient, events
from dotenv import load_dotenv

# --- IMPORTS ---
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import (
    OrderSide, CreateOrderRequest, OpenType, OrderType,
    TriggerOrderRequest, TriggerType, TriggerPriceType, ExecuteCycle
)

import asyncio
import logging
import re
import math
from datetime import datetime, timezone
from telethon import TelegramClient, events

# --- IMPORTS ---
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import (
    OrderSide, CreateOrderRequest, OpenType, OrderType,
    TriggerOrderRequest, TriggerType, TriggerPriceType, ExecuteCycle
)
from mexcpy.config import API_ID, API_HASH, TARGET_CHATS, USER_LISTENER_TOKEN, SESSION_USER

# --- CONFIGURATION ---
if not USER_LISTENER_TOKEN or not API_ID:
    print(" ERROR: Credentials missing. Please check your .env file.")
    exit(1)

START_TIME = datetime.now(timezone.utc)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

MexcAPI = MexcFuturesAPI(token=USER_LISTENER_TOKEN, testnet=True)
client = TelegramClient(str(SESSION_USER), API_ID, API_HASH)

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


async def monitor_trade(symbol: str, start_vol: int, targets: list):
    print(f" Auto-monitoring started for {symbol}...")
    last_vol = start_vol
    first_run = True

    def identify_tp(fill_price):
        for i, target in enumerate(targets):
            # 0.5% buffer for price matching
            if abs(fill_price - target) / target < 0.005:
                return f"TP{i + 1}"
        return "Manual/Partial"

    while True:
        await asyncio.sleep(5)

        try:
            pos_res = await MexcAPI.get_open_positions(symbol)

            if not pos_res.success or not pos_res.data:

                await MexcAPI.cancel_all_orders(symbol=symbol)
                await MexcAPI.cancel_all_trigger_orders(symbol=symbol)

                hist_res = await MexcAPI.get_historical_orders(symbol=symbol, states='3', page_size=5)

                hit_labels = set()
                is_sl = False

                if hist_res.success and hist_res.data:
                    for order in hist_res.data:
                        if isinstance(order, dict):
                            price = float(order.get('dealAvgPrice', 0))
                            o_type = int(order.get('orderType', 0))
                        else:
                            price = float(order.dealAvgPrice)
                            o_type = int(order.orderType)

                        if o_type == 1:
                            label = identify_tp(price)
                            hit_labels.add(label)
                        elif o_type == 5:
                            is_sl = True

                reason = "Unknown"
                if is_sl:
                    reason = " **Stop Loss Hit (or Market Close)**"
                elif hit_labels:
                    reason = f" **{', '.join(sorted(hit_labels))} Hit** (All Closed)"

                msg = f" **{symbol} Closed!**\nReason: {reason}\n Cleanup done."
                print(f"\n{msg}\n---------------------------------------")
                await client.send_message('me', msg)
                break

            position = pos_res.data[0]
            current_vol = position.holdVol

            if first_run:
                last_vol = current_vol
                first_run = False
                continue

            if current_vol < last_vol:
                diff = last_vol - current_vol
                msg = f" **{symbol} Partial TP Hit!**\n   Reduced by {diff} contracts.\n   Remaining: {current_vol}"
                print(f"\n{msg}\n---------------------------------------")
                await client.send_message('me', msg)
                last_vol = current_vol

            if current_vol > last_vol:
                last_vol = current_vol

        except Exception as e:
            print(f" Monitor Error: {e}")
            await asyncio.sleep(5)


# --- PARSER ---
def parse_signal(text: str):
    try:
        text = text.replace('*', '').replace(',', '').replace('\xa0', ' ')
        print(f" DEBUG RAW: {text}")

        data = {}
        pair_match = re.search(r"PAIR:\s*([A-Z0-9]+)[/_]([A-Z0-9]+)", text, re.IGNORECASE)
        if not pair_match: return None
        data['symbol'] = f"{pair_match.group(1)}_{pair_match.group(2)}".upper()

        size_match = re.search(r"POSITION SIZE:\s*(\d+)\s*-\s*(\d+)%", text, re.IGNORECASE)
        if size_match:
            data['equity_perc'] = (float(size_match.group(1)) + float(size_match.group(2))) / 2
        else:
            data['equity_perc'] = 1.0

        type_match = re.search(r"TYPE:\s*(LONG|SHORT)", text, re.IGNORECASE)
        if not type_match: return None
        data['side'] = OrderSide.OpenLong if type_match.group(1).upper() == "LONG" else OrderSide.OpenShort

        entry_match = re.search(r"ENTRY[^0-9]*([\d.]+)", text, re.IGNORECASE)
        data['entry'] = float(entry_match.group(1)) if entry_match else "Market"

        sl_match = re.search(r"SL[^0-9]*([\d.]+)", text, re.IGNORECASE)
        data['sl'] = float(sl_match.group(1)) if sl_match else None

        lev_match = re.search(r"LEVERAGE[^0-9]*(\d+)", text, re.IGNORECASE)
        data['leverage'] = int(lev_match.group(1)) if lev_match else 20

        tps = re.findall(r"TP\d[^0-9]*([\d.]+)", text, re.IGNORECASE)
        data['tps'] = [float(tp) for tp in tps]

        return data
    except Exception as e:
        logger.error(f"Parse Error: {e}")
        return None


async def execute_signal_trade(data):
    symbol = data['symbol']
    leverage = data['leverage']
    side = data['side']
    equity_perc = data['equity_perc']
    entry_label = data['entry']
    sl_price_raw = data.get('sl')

    quote_currency = symbol.split('_')[1]

    assets_response = await MexcAPI.get_user_assets()
    if not assets_response.success: return f" API Error: {assets_response.message}"

    target_asset = next((a for a in assets_response.data if a.currency == quote_currency), None)
    if not target_asset: return f" Error: {quote_currency} not found in wallet."

    balance = target_asset.availableBalance

    ticker_res = await MexcAPI.get_ticker(symbol)
    if not ticker_res.success or not ticker_res.data:
        return f" Ticker Error: {ticker_res.message or 'No data returned'}"
    current_price = ticker_res.data.get('lastPrice')

    contract_res = await MexcAPI.get_contract_details(symbol)
    if not contract_res.success: return f" Contract Error: {contract_res.message}"

    contract_size = contract_res.data.get('contractSize')
    real_tick_size = contract_res.data.get('priceUnit')
    price_step = real_tick_size

    print(f" DEBUG: {symbol} Tick Size: {price_step} | Current Price: {current_price}")

    margin_amount = balance * (equity_perc / 100.0)
    position_value = margin_amount * leverage
    vol = int(position_value / (contract_size * current_price))

    if vol == 0: return f" Vol is 0. (Bal: {balance:.2f} {quote_currency}, Lev: {leverage})"

    logger.info(f" Executing {side.name} {symbol} x{leverage} | Vol: {vol}")

    final_sl_price = adjust_price_to_step(sl_price_raw, price_step)

    open_req = CreateOrderRequest(
        symbol=symbol,
        side=side,
        vol=vol,
        leverage=leverage,
        openType=OpenType.Cross,
        type=OrderType.MarketOrder,
        stopLossPrice=final_sl_price
    )

    order_res = await MexcAPI.create_order(open_req)
    if not order_res.success: return f" Open Failed: {order_res.message}"

    tp_formatted_msg = ""
    if data['tps']:
        tp_side = OrderSide.CloseLong if side == OrderSide.OpenLong else OrderSide.CloseShort

        tp1_vol = int(vol * 0.50)
        tp2_vol = int((vol - tp1_vol) * 0.50)
        tp3_vol = vol - tp1_vol - tp2_vol
        tps_vols = [tp1_vol, tp2_vol, tp3_vol]
        tps_labels = ["50%", "50% (of rem)", "100% (of rem)"]

        for i, tp_price in enumerate(data['tps']):
            if i >= 3: break
            target_vol = tps_vols[i]
            if target_vol <= 0: continue

            final_tp_price = adjust_price_to_step(tp_price, price_step)

            tp_req = CreateOrderRequest(
                symbol=symbol,
                side=tp_side,
                vol=target_vol,
                leverage=leverage,
                price=final_tp_price,
                openType=OpenType.Cross,
                type=OrderType.PriceLimited,
                reduceOnly=True
            )

            tp_res = await MexcAPI.create_order(tp_req)

            if tp_res.success:
                tp_formatted_msg += f"\n   • TP{i + 1}: {final_tp_price} ({tps_labels[i]})"
            else:
                print(f"  TP{i + 1} FAILED: {tp_res.message}")
                tp_formatted_msg += f"\n   •  TP{i + 1} Failed ({tp_res.message})"

    asyncio.create_task(monitor_trade(symbol, vol, data['tps']))

    return (
        f" SUCCESS: {symbol} {side.name} x{leverage}\n"
        f"---------------------------------------\n"
        f" Pos Size: {equity_perc}% (Midpoint) | Vol: {vol}\n"
        f" Entry: {entry_label}\n"
        f" SL: {final_sl_price} (Set on Position )\n"
        f" TPs: {tp_formatted_msg if tp_formatted_msg else 'None'}\n"
        f" Auto-monitoring started..."
    )


@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    if event.date < START_TIME:
        return

    text = event.text
    if "PAIR:" in text and "TYPE:" in text:
        print("\n Signal Detected! Parsing...")
        signal_data = parse_signal(text)

        if signal_data:
            result = await execute_signal_trade(signal_data)
            print(result)
            await client.send_message('me', result)
        else:
            print(" Failed to parse signal data.")


if __name__ == "__main__":
    print("--------------------------------------")
    print(" USER LISTENER STARTED (OLD ALL TP MODE)")
    print(f" Start Time (UTC): {START_TIME}")
    print(f" Watching Chats: {TARGET_CHATS}")
    print("---------------------------------------")
    client.start()
    client.run_until_disconnected()