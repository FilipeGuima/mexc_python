import asyncio
import logging
import re
import math
import time
import os # Import os
from datetime import datetime, timezone
from telethon import TelegramClient, events
from dotenv import load_dotenv # Import dotenv


# --- IMPORTS ---
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import (
    OrderSide, CreateOrderRequest, OpenType, OrderType,
    TriggerOrderRequest, TriggerType, TriggerPriceType, ExecuteCycle
)
from mexcpy.config import API_ID, API_HASH, TARGET_CHATS, MEXC_TOKEN, SESSION_FILE

START_TIME = datetime.now(timezone.utc)

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

MexcAPI = MexcFuturesAPI(token=MEXC_TOKEN, testnet=True)
client = TelegramClient(str(SESSION_FILE), API_ID, API_HASH)

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
                await client.send_message('me', msg)
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


def parse_signal(text: str):
    try:
        print(f" DEBUG RAW: {text}")

        text_clean = text.encode('ascii', 'ignore').decode('ascii')
        text_clean = text_clean.replace('*', '').replace(',', '')

        data = {}

        pair_match = re.search(r"PAIR:\s*([A-Z0-9]+)[/_]([A-Z0-9]+)", text_clean, re.IGNORECASE)
        if not pair_match:
            print(" Parsing failed: No PAIR found.")
            return None
        data['symbol'] = f"{pair_match.group(1)}_{pair_match.group(2)}".upper()

        size_match = re.search(r"SIZE:\s*(\d+)(?:-\s*(\d+))?%", text_clean, re.IGNORECASE)

        if size_match:
            val1 = float(size_match.group(1))
            val2 = float(size_match.group(2)) if size_match.group(2) else val1
            data['equity_perc'] = (val1 + val2) / 2

        side_match = re.search(r"SIDE:\s*(LONG|SHORT)", text_clean, re.IGNORECASE)
        if not side_match:
            print(" Parsing failed: No SIDE (LONG/SHORT) found.")
            return None

        direction = side_match.group(1).upper()
        data['side'] = OrderSide.OpenLong if direction == "LONG" else OrderSide.OpenShort

        entry_match = re.search(r"ENTRY:\s*([\d.]+)", text_clean, re.IGNORECASE)
        data['entry'] = float(entry_match.group(1)) if entry_match else "Market"

        sl_match = re.search(r"SL:\s*([\d.]+)", text_clean, re.IGNORECASE)
        data['sl'] = float(sl_match.group(1)) if sl_match else None

        lev_match = re.search(r"LEVERAGE:\s*(\d+)", text_clean, re.IGNORECASE)
        data['leverage'] = int(lev_match.group(1)) if lev_match else 20


        all_tps = re.findall(r"TP\d:\s*([\d.]+)", text_clean, re.IGNORECASE)

        real_tps = []
        if all_tps:
            limit = 3 if len(all_tps) >= 6 else len(all_tps)
            real_tps = [float(tp) for tp in all_tps[:limit]]

        data['tps'] = real_tps

        return data
    except Exception as e:
        logger.error(f"Parse Error: {e}")
        import traceback
        traceback.print_exc()
        return None


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
    if not order_res.success: return f" Open Failed: {order_res.message}"

    asyncio.create_task(monitor_trade(symbol, vol, data['tps']))

    return (
        f" SUCCESS: {symbol} {side.name} x{leverage}\n"
        f"---------------------------------------\n"
        f" Pos Size: {equity_perc}% (Midpoint) | Vol: {vol}\n"
        f" Entry: {entry_label}\n"
        f" SL: {final_sl_price} (Set on Position )\n"
        f" TP1: {final_tp_price} (Set on Position )\n"
    )


@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    if event.date < START_TIME:
        return

    text = event.text
    if "PAIR:" in text and "SIDE:" in text:
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
    print(" USER LISTENER STARTED (TP1 MODE) ")
    print(f" Start Time: {START_TIME}")
    print(f" Watching Chats: {TARGET_CHATS}")
    print("---------------------------------------")
    client.start()
    client.run_until_disconnected()