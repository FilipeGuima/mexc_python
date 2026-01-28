import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

current_path = Path(__file__).resolve()
project_root = current_path.parent.parent.parent
sys.path.append(str(project_root))

# --- LIBRARIES ---
from telethon import TelegramClient, events

# --- PROJECT IMPORTS ---
from blofincpy.api import BlofinFuturesAPI

from mexcpy.config import (
    API_ID,
    API_HASH,
    TARGET_CHATS,
    SESSION_TP3,
    BLOFIN_API_KEY,
    BLOFIN_SECRET_KEY,
    BLOFIN_PASSPHRASE
)

# --- CONFIGURATION CHECK ---
if not BLOFIN_API_KEY or not BLOFIN_SECRET_KEY or not BLOFIN_PASSPHRASE:
    print("CRITICAL ERROR: Blofin credentials (API_KEY, SECRET, PASSPHRASE) are missing from config.py or .env")
    exit(1)

START_TIME = datetime.now(timezone.utc)

# --- LOGGING SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("BlofinTP3Listener")

# --- INITIALIZE API & CLIENT ---
BlofinAPI = BlofinFuturesAPI(
    api_key=BLOFIN_API_KEY,
    secret_key=BLOFIN_SECRET_KEY,
    passphrase=BLOFIN_PASSPHRASE,
    testnet=True
)

# Telegram Client (using the 'tp3' session)
client = TelegramClient(str(SESSION_TP3), API_ID, API_HASH)

# Track pending orders for monitoring
pending_orders = {}  # {order_id: {symbol, side, size, tp, sl, entry_price}}

# Track active positions for monitoring closures
active_positions = {}  # {symbol: {side, size, entry_price, tp, sl, leverage}}


async def load_existing_positions():
    """Load existing open positions on startup so we can track them."""
    global active_positions

    print("\nChecking for existing positions...")

    try:
        positions = await BlofinAPI.get_open_positions()

        if not positions:
            print("  No existing positions found.\n")
            return

        for pos in positions:
            symbol = pos.symbol

            # Get associated TPSL orders for this position
            tpsl_orders = await BlofinAPI.get_tpsl_orders(symbol)
            tp_price = None
            sl_price = None

            for order in tpsl_orders:
                tp = order.get('tpTriggerPrice')
                sl = order.get('slTriggerPrice')
                if tp and float(tp) > 0:
                    tp_price = float(tp)
                if sl and float(sl) > 0:
                    sl_price = float(sl)

            side = "buy" if pos.positionType in ["long", "net"] and pos.holdVol > 0 else "sell"

            active_positions[symbol] = {
                'side': side,
                'size': pos.holdVol,
                'entry_price': pos.openAvgPrice,
                'tp': tp_price,
                'sl': sl_price,
                'leverage': pos.leverage,
                'unrealized_pnl': pos.unrealized,
                'mark_price': pos.markPrice
            }

            pnl_str = f"+{pos.unrealized:.2f}" if pos.unrealized >= 0 else f"{pos.unrealized:.2f}"
            print(f"  ✓ Loaded: {symbol} | {side.upper()} | Entry: {pos.openAvgPrice} | PnL: {pnl_str}")

        print(f"\n  Total: {len(active_positions)} position(s) loaded.\n")

    except Exception as e:
        logger.error(f"Error loading existing positions: {e}")


# --- HELPER FUNCTIONS ---
def adjust_price_to_step(price, step_size):
    """Rounds a price to the nearest valid step size allowed by the exchange."""
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

async def monitor_orders_and_positions():
    """
    Background task that monitors:
    1. Pending limit orders - for fills or cancellations
    2. Active positions - for closures (TP hit, SL hit, manual close)
    """
    global pending_orders, active_positions

    while True:
        try:
            # === PART 1: Monitor Pending Orders ===
            if pending_orders:
                orders_to_remove = []
                all_pending = await BlofinAPI.get_pending_orders()

                for order_id, order_info in list(pending_orders.items()):
                    symbol = order_info['symbol']

                    # Find our order in pending orders
                    our_order = None
                    for o in all_pending:
                        if str(o.get('orderId')) == str(order_id):
                            our_order = o
                            break

                    if our_order:
                        # Order still pending
                        state = our_order.get('state', '')
                        if state == 'live':
                            logger.debug(f"Order {order_id} still pending for {symbol}")
                        elif state == 'filled':
                            filled_size = float(our_order.get('filledSize', 0))
                            avg_price = float(our_order.get('averagePrice', 0)) or order_info.get('entry_price')
                            await _handle_order_filled(order_id, order_info, filled_size, avg_price)
                            orders_to_remove.append(order_id)
                    else:
                        # Order not in pending - check history
                        history = await BlofinAPI.get_order_history(symbol=symbol, order_id=order_id)

                        if history:
                            hist_order = history[0] if isinstance(history, list) else history
                            state = hist_order.get('state', '')
                            filled_size = float(hist_order.get('filledSize', 0))
                            avg_price = float(hist_order.get('averagePrice', 0)) or order_info.get('entry_price')

                            if state == 'filled' and filled_size > 0:
                                await _handle_order_filled(order_id, order_info, filled_size, avg_price)
                                orders_to_remove.append(order_id)
                            elif state in ['cancelled', 'canceled']:
                                await _handle_order_cancelled(order_id, order_info)
                                orders_to_remove.append(order_id)
                            else:
                                check_count = order_info.get('_check_count', 0) + 1
                                order_info['_check_count'] = check_count
                                if check_count >= 3:
                                    orders_to_remove.append(order_id)
                        else:
                            check_count = order_info.get('_check_count', 0) + 1
                            order_info['_check_count'] = check_count
                            if check_count >= 3:
                                # Assume filled if disappeared
                                await _handle_order_filled(
                                    order_id, order_info,
                                    order_info.get('size'),
                                    order_info.get('entry_price')
                                )
                                orders_to_remove.append(order_id)

                for oid in orders_to_remove:
                    if oid in pending_orders:
                        del pending_orders[oid]

            # === PART 2: Monitor Active Positions ===
            if active_positions:
                positions_to_remove = []

                for symbol, pos_info in list(active_positions.items()):
                    # Check if position still exists
                    positions = await BlofinAPI.get_open_positions(symbol)

                    if positions and len(positions) > 0:
                        # Position still open - update live data
                        live_pos = positions[0]
                        pos_info['unrealized_pnl'] = live_pos.unrealized
                        pos_info['mark_price'] = live_pos.markPrice
                        continue

                    # Position not found - check if TPSL orders exist as fallback
                    tpsl_orders = await BlofinAPI.get_tpsl_orders(symbol)
                    if tpsl_orders and len(tpsl_orders) > 0:
                        continue  # TPSL exists, position likely still open

                    # Position appears closed - determine reason
                    check_count = pos_info.get('_close_check_count', 0) + 1
                    pos_info['_close_check_count'] = check_count

                    if check_count >= 2:  # Confirm closure after 2 checks
                        await _handle_position_closed(symbol, pos_info)
                        positions_to_remove.append(symbol)

                for sym in positions_to_remove:
                    if sym in active_positions:
                        del active_positions[sym]

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Monitor error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(10)


async def _handle_order_filled(order_id: str, order_info: dict, filled_size: float, fill_price: float):
    """Handle when a limit order is filled."""
    symbol = order_info['symbol']
    side = order_info['side']

    fill_msg = (
        f"\n{'='*40}\n"
        f"🚀 **LIMIT ORDER FILLED!**\n"
        f"   Symbol: {symbol}\n"
        f"   Side: {side.upper()}\n"
        f"   Entry: {fill_price}\n"
        f"   Size: {filled_size}\n"
        f"   Lev: x{order_info.get('leverage', 'N/A')}\n"
    )

    tp_price = order_info.get('tp')
    sl_price = order_info.get('sl')

    # Set TP/SL if needed - use SINGLE combined order
    if tp_price or sl_price:
        tpsl_side = "sell" if side == "buy" else "buy"
        position_side = "long" if side == "buy" else "short"

        tpsl_body = {
            "instId": symbol,
            "marginMode": "isolated",
            "posSide": position_side,
            "side": tpsl_side,
            "size": str(filled_size),
            "reduceOnly": "true"
        }

        if tp_price:
            tpsl_body["tpTriggerPrice"] = str(tp_price)
            tpsl_body["tpOrderPrice"] = "-1"
        if sl_price:
            tpsl_body["slTriggerPrice"] = str(sl_price)
            tpsl_body["slOrderPrice"] = "-1"

        tpsl_res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)

        if tpsl_res and tpsl_res.get('code') == "0":
            parts = []
            if tp_price:
                parts.append(f"TP3: {tp_price}")
            if sl_price:
                parts.append(f"SL: {sl_price}")
            fill_msg += f"   ✓ Set: {', '.join(parts)} (combined)\n"
        else:
            error = tpsl_res.get('msg', 'Failed') if tpsl_res else 'No response'
            fill_msg += f"   ⚠️ TPSL Failed: {error}\n"

    fill_msg += f"{'='*40}"
    print(fill_msg)

    # Add to active positions for monitoring
    active_positions[symbol] = {
        'side': side,
        'size': filled_size,
        'entry_price': fill_price,
        'tp': tp_price,
        'sl': sl_price,
        'leverage': order_info.get('leverage')
    }
    logger.info(f"Added {symbol} to active positions monitoring")


async def _handle_order_cancelled(order_id: str, order_info: dict):
    """Handle when an order is cancelled."""
    symbol = order_info['symbol']
    side = order_info['side']
    entry = order_info.get('entry_price', 'N/A')

    cancel_msg = (
        f"\n{'='*40}\n"
        f"❌ **ORDER CANCELLED**\n"
        f"   Symbol: {symbol}\n"
        f"   Side: {side.upper()}\n"
        f"   Entry: {entry}\n"
        f"   Order ID: {order_id}\n"
        f"{'='*40}"
    )
    print(cancel_msg)


async def _handle_position_closed(symbol: str, pos_info: dict):
    """Handle when a position is closed. Determine the reason and show summary."""
    from blofincpy.blofinTypes import CloseReason

    side = pos_info.get('side', 'unknown')
    entry_price = pos_info.get('entry_price', 0)
    tp_price = pos_info.get('tp')
    sl_price = pos_info.get('sl')
    leverage = pos_info.get('leverage', 1)
    size = pos_info.get('size', 0)

    # Get close reason from API
    close_reason = await BlofinAPI.get_position_close_reason(symbol)

    # Format reason message
    if close_reason == CloseReason.TP:
        reason_str = f"TP3 HIT @ {tp_price}" if tp_price else "TAKE PROFIT"
        emoji = "🎯"
    elif close_reason == CloseReason.SL:
        reason_str = f"STOP LOSS @ {sl_price}" if sl_price else "STOP LOSS"
        emoji = "🛑"
    elif close_reason == CloseReason.LIQUIDATION:
        reason_str = "LIQUIDATED"
        emoji = "💀"
    elif close_reason == CloseReason.MANUAL:
        reason_str = "MANUAL CLOSE"
        emoji = "👤"
    else:
        reason_str = "UNKNOWN"
        emoji = "❓"

    close_msg = (
        f"\n{'='*40}\n"
        f"{emoji} **POSITION CLOSED** - {symbol}\n"
        f"   Side: {side.upper()}\n"
        f"   Entry: {entry_price}\n"
        f"   Size: {size} @ {leverage}x\n"
        f"   Reason: {reason_str}\n"
        f"{'='*40}"
    )
    print(close_msg)
    logger.info(f"Position closed: {symbol} - {reason_str}")


async def execute_signal_trade(data):
    """
    TP3 Strategy: Place order and close 100% at TP3.
    """
    symbol_raw = data['symbol']
    formatted_symbol = symbol_raw.replace('_', '-')

    side = data['side']
    leverage = data['leverage']
    equity_perc = data['equity_perc']
    entry_price = data['entry']

    # Get TP/SL - USE TP3 (index 2) for 100% close
    sl_price = data.get('sl')
    tps = data.get('tps', [])

    # TP3 Strategy: Use the third TP level (index 2)
    tp3_price = None
    if len(tps) >= 3:
        tp3_price = tps[2]  # TP3
    elif len(tps) >= 2:
        tp3_price = tps[1]  # Fallback to TP2 if no TP3
        logger.warning(f"No TP3 found, using TP2: {tp3_price}")
    elif len(tps) >= 1:
        tp3_price = tps[0]  # Fallback to TP1 if no TP2/TP3
        logger.warning(f"No TP3/TP2 found, using TP1: {tp3_price}")

    if not tp3_price:
        logger.warning("No TP levels found in signal!")

    # Validate entry price is a number
    if not isinstance(entry_price, (int, float)):
        try:
            entry_price = float(str(entry_price).replace(',', ''))
        except (ValueError, TypeError):
            return f"⚠️ Invalid Entry Price: {entry_price} - Cannot place limit order"

    # Determine Entry Side
    blofin_side = "buy" if "LONG" in side.name.upper() else "sell"
    pos_side = "net"

    # 1. Fetch Balance & Calc Volume
    assets = await BlofinAPI.get_user_assets()
    usdt_asset = next((a for a in assets if a.currency == "USDT"), None)

    if not usdt_asset:
        return "⚠️ Wallet Error: USDT balance not found."

    balance = usdt_asset.availableBalance

    # Get instrument info for contract size
    inst_info = await BlofinAPI.get_instrument_info(formatted_symbol)
    if not inst_info:
        return f"⚠️ Instrument Error: Could not get contract details for {formatted_symbol}"

    logger.info(f" Instrument Info: {inst_info}")

    contract_value = float(inst_info.get('contractValue', 1))
    lot_size = float(inst_info.get('lotSize', 1))
    min_size = float(inst_info.get('minSize', lot_size))

    logger.info(f" Contract Value: {contract_value} | Lot Size: {lot_size} | Min Size: {min_size}")

    # Get tick size for price precision
    tick_size = float(inst_info.get('tickSize', 0.00001))

    # Round entry price to tick size
    entry_price = adjust_price_to_step(entry_price, tick_size)

    # Round TP/SL to tick size
    if tp3_price:
        tp3_price = adjust_price_to_step(tp3_price, tick_size)
    if sl_price:
        sl_price = adjust_price_to_step(sl_price, tick_size)

    logger.info(f" Tick Size: {tick_size} | Entry (rounded): {entry_price}")

    # Calc Volume
    margin_amount = balance * (equity_perc / 100.0)
    notional_value = margin_amount * leverage
    contract_usdt_value = contract_value * entry_price
    calculated_vol = notional_value / contract_usdt_value

    logger.info(f" Calc: {notional_value:.2f} / {contract_usdt_value:.2f} = {calculated_vol:.4f} contracts")

    # Round to valid lot size
    final_vol = round(calculated_vol / lot_size) * lot_size

    # Ensure minimum size
    if final_vol < min_size:
        final_vol = min_size

    final_vol = round(final_vol, 8)

    logger.info(f" Balance: {balance:.2f} USDT | Size: {equity_perc}% | Margin: {margin_amount:.2f} | Notional: {notional_value:.2f}")

    # Fetch current market price
    ticker_res = await BlofinAPI._make_request("GET", "/api/v1/market/tickers", params={"instId": formatted_symbol})
    current_price = 0
    if ticker_res and ticker_res.get('data'):
        current_price = float(ticker_res['data'][0]['last'])

    if current_price == 0:
        return f"⚠️ Price Error: Could not fetch current price for {formatted_symbol}"

    logger.info(f" Current Market Price: {current_price} | Entry Price: {entry_price}")

    # Smart entry logic
    use_market_order = False
    order_reason = "LIMIT ORDER"

    if blofin_side == "buy":  # LONG
        if current_price <= entry_price:
            use_market_order = True
            order_reason = f"MARKET (price {current_price} <= entry {entry_price})"
        else:
            order_reason = f"LIMIT @ {entry_price} (waiting for pullback from {current_price})"
    else:  # sell / SHORT
        if current_price >= entry_price:
            use_market_order = True
            order_reason = f"MARKET (price {current_price} >= entry {entry_price})"
        else:
            order_reason = f"LIMIT @ {entry_price} (waiting for bounce from {current_price})"

    logger.info(f" Order Decision: {order_reason}")

    # Validate TP/SL relative to entry/current price
    actual_entry = current_price if use_market_order else entry_price

    if blofin_side == "buy":
        if tp3_price and tp3_price <= actual_entry:
            logger.warning(f"TP3 ({tp3_price}) should be above entry ({actual_entry}) for LONG - skipping TP")
            tp3_price = None
        if sl_price and sl_price >= actual_entry:
            logger.warning(f"SL ({sl_price}) should be below entry ({actual_entry}) for LONG - skipping SL")
            sl_price = None
    else:
        if tp3_price and tp3_price >= actual_entry:
            logger.warning(f"TP3 ({tp3_price}) should be below entry ({actual_entry}) for SHORT - skipping TP")
            tp3_price = None
        if sl_price and sl_price <= actual_entry:
            logger.warning(f"SL ({sl_price}) should be above entry ({actual_entry}) for SHORT - skipping SL")
            sl_price = None

    if use_market_order:
        logger.info(f" Placing MARKET {blofin_side.upper()} {formatted_symbol} x{leverage} | Vol: {final_vol}")

        res = await BlofinAPI.create_market_order(
            symbol=formatted_symbol,
            side=blofin_side,
            vol=final_vol,
            leverage=leverage,
            position_side=pos_side
        )

        logger.info(f"Order Response: {res}")

        if res and res.get('code') == "0":
            order_data = res.get('data', {})
            # Handle both dict and list responses
            if isinstance(order_data, list) and order_data:
                order_id = order_data[0].get('orderId', 'N/A')
            elif isinstance(order_data, dict):
                order_id = order_data.get('orderId', 'N/A')
            else:
                order_id = 'N/A'

            order_msg = (
                f"🚀 **MARKET ORDER EXECUTED (Blofin TP3)**\n"
                f"   Symbol: {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   Entry: Market (~{current_price})\n"
                f"   Size: {final_vol} (100% close at TP3)\n"
                f"   Lev: x{leverage}\n"
            )

            # Set TP/SL via separate TPSL order
            if tp3_price or sl_price:
                await asyncio.sleep(1.5)

                ticker_res2 = await BlofinAPI._make_request("GET", "/api/v1/market/tickers", params={"instId": formatted_symbol})
                latest_price = current_price
                if ticker_res2 and ticker_res2.get('data'):
                    latest_price = float(ticker_res2['data'][0]['last'])

                logger.info(f" Current price after fill: {latest_price}")

                valid_tp = tp3_price
                valid_sl = sl_price

                if blofin_side == "buy":
                    if valid_tp and valid_tp <= latest_price:
                        logger.warning(f"TP3 ({valid_tp}) not above current price ({latest_price}) - skipping TP")
                        valid_tp = None
                    if valid_sl and valid_sl >= latest_price:
                        logger.warning(f"SL ({valid_sl}) not below current price ({latest_price}) - skipping SL")
                        valid_sl = None
                else:
                    if valid_tp and valid_tp >= latest_price:
                        logger.warning(f"TP3 ({valid_tp}) not below current price ({latest_price}) - skipping TP")
                        valid_tp = None
                    if valid_sl and valid_sl <= latest_price:
                        logger.warning(f"SL ({valid_sl}) not above current price ({latest_price}) - skipping SL")
                        valid_sl = None

                if valid_tp or valid_sl:
                    # Use SINGLE combined order for both TP and SL
                    close_side = "sell" if blofin_side == "buy" else "buy"
                    position_side = "long" if blofin_side == "buy" else "short"

                    tpsl_body = {
                        "instId": formatted_symbol,
                        "marginMode": "isolated",
                        "posSide": position_side,
                        "side": close_side,
                        "size": str(final_vol),
                        "reduceOnly": "true"
                    }

                    if valid_tp:
                        tpsl_body["tpTriggerPrice"] = str(valid_tp)
                        tpsl_body["tpOrderPrice"] = "-1"
                    if valid_sl:
                        tpsl_body["slTriggerPrice"] = str(valid_sl)
                        tpsl_body["slOrderPrice"] = "-1"

                    logger.info(f" Setting combined TPSL: {tpsl_body}")
                    tpsl_res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)
                    logger.info(f" TPSL Response: {tpsl_res}")

                    if tpsl_res and tpsl_res.get('code') == "0":
                        if valid_tp:
                            order_msg += f"   TP3: {valid_tp} (100% close)\n"
                        if valid_sl:
                            order_msg += f"   SL: {valid_sl}\n"
                        order_msg += f"   ✓ TPSL set (combined order)\n"
                    else:
                        error = tpsl_res.get('msg', 'Unknown') if tpsl_res else 'No response'
                        order_msg += f"   ⚠️ TPSL Failed: {error}"
                else:
                    order_msg += "   ⚠️ TP/SL skipped (invalid vs current price)"

            # Add to active positions for monitoring
            active_positions[formatted_symbol] = {
                'side': blofin_side,
                'size': final_vol,
                'entry_price': current_price,
                'tp': tp3_price,
                'sl': sl_price,
                'leverage': leverage
            }
            logger.info(f"Added {formatted_symbol} to active positions monitoring")

            return order_msg
        else:
            error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
            data = res.get('data', [])
            if data and isinstance(data, list) and data[0].get('msg'):
                error_msg = data[0].get('msg')
            return f"❌ **Market Order Failed**\n   Error: {error_msg}"

    else:
        # Use limit order
        logger.info(f" Placing LIMIT {blofin_side.upper()} {formatted_symbol} @ {entry_price} x{leverage} | Vol: {final_vol}")

    # Place LIMIT Order at entry price with TP/SL attached
    res = await BlofinAPI.create_limit_order(
        symbol=formatted_symbol,
        side=blofin_side,
        vol=final_vol,
        price=entry_price,
        leverage=leverage,
        position_side=pos_side,
        take_profit=tp3_price,
        stop_loss=sl_price
    )

    logger.info(f"Order Response: {res}")

    if res and res.get('code') == "0":
        order_data = res.get('data', {})
        # Handle both dict and list responses
        if isinstance(order_data, list) and order_data:
            order_id = order_data[0].get('orderId', 'N/A')
        elif isinstance(order_data, dict):
            order_id = order_data.get('orderId', 'N/A')
        else:
            order_id = 'N/A'

        # Check if TP/SL were attached
        tpsl_attached = False
        if order_id != 'N/A':
            await asyncio.sleep(0.5)
            pending = await BlofinAPI.get_pending_orders(formatted_symbol)
            for p in pending:
                if str(p.get('orderId')) == str(order_id):
                    if p.get('tpTriggerPrice') or p.get('slTriggerPrice'):
                        tpsl_attached = True
                    break

        # If TP/SL not attached, add to monitor queue
        if not tpsl_attached and order_id != 'N/A':
            pending_orders[order_id] = {
                'symbol': formatted_symbol,
                'side': blofin_side,
                'size': final_vol,
                'entry_price': entry_price,
                'tp': tp3_price,
                'sl': sl_price,
                'leverage': leverage
            }
            logger.info(f"TP/SL not attached to order, added {order_id} to monitoring queue")

        order_msg = (
            f"📋 **LIMIT ORDER PLACED (Blofin TP3)**\n"
            f"   Symbol: {formatted_symbol}\n"
            f"   Side: {blofin_side.upper()}\n"
            f"   Entry: {entry_price}\n"
            f"   Size: {final_vol} (100% close at TP3)\n"
            f"   Lev: x{leverage}\n"
            f"   Order ID: {order_id}\n"
        )

        if tp3_price:
            status = "✓" if tpsl_attached else "on fill"
            order_msg += f"   TP3: {tp3_price} ({status})\n"
        if sl_price:
            status = "✓" if tpsl_attached else "on fill"
            order_msg += f"   SL: {sl_price} ({status})\n"

        order_msg += "   ⏳ Waiting for price to reach entry..."

        return order_msg

    else:
        error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
        data = res.get('data', [])
        if data and isinstance(data, list) and len(data) > 0 and data[0].get('msg'):
            error_msg = data[0].get('msg')
        return f"❌ **Limit Order Failed**\n   Error: {error_msg}"


# --- PARSERS ---

class SignalParser:
    """
    Robust signal parser for NEW TRADES.
    Handles: "PAIR: BTC/USDT", "SIDE: LONG", "TP1: 0.55", ignoring "R:R" ratios.
    """
    NUM_PATTERN = r'([\d,]+\.?\d*)'

    # Hidden characters to strip from Telegram messages
    HIDDEN_CHARS = [
        '\u200b', '\u200c', '\u200d', '\u200e', '\u200f',
        '\u00a0', '\u2060', '\ufeff', '\u00ad', '\u2007',
        '\u2008', '\u2009', '\u200a', '\u202f', '\u205f', '\u3000',
    ]

    @staticmethod
    def _extract_number(text: str) -> float | None:
        if not text:
            return None
        cleaned = text.replace(',', '')
        try:
            return float(cleaned)
        except ValueError:
            return None

    @classmethod
    def _clean_text(cls, text: str) -> str:
        """Remove hidden unicode characters and normalize text."""
        for char in cls.HIDDEN_CHARS:
            text = text.replace(char, ' ')
        return text.upper()

    @classmethod
    def parse(cls, text: str, debug: bool = True) -> dict | None:
        if debug:
            print(f" DEBUG RAW: {repr(text)}")

        text_upper = cls._clean_text(text)
        data = {}

        # Ignore Status Updates
        if "TARGET HIT" in text_upper or "PROFIT:" in text_upper:
            if debug:
                print(" Ignored: Status/Profit update")
            return None

        # --- PAIR ---
        pair_match = re.search(r'PAIR[\W_]*([A-Z0-9]+)[\W_]*[/_:-]?[\W_]*([A-Z0-9]+)', text_upper)
        if not pair_match:
            if debug:
                print(" Parsing failed: No PAIR found.")
            return None
        data['symbol'] = f"{pair_match.group(1)}_{pair_match.group(2)}"

        # --- SIDE ---
        side_match = re.search(r'SIDE[\W_]*(LONG|SHORT)', text_upper)
        if not side_match:
            if debug:
                print(" Parsing failed: No SIDE found.")
            return None
        direction = side_match.group(1)
        from mexcpy.mexcTypes import OrderSide as MexcOrderSide
        data['side'] = MexcOrderSide.OpenLong if direction == "LONG" else MexcOrderSide.OpenShort

        # --- SIZE ---
        size_match = re.search(r'SIZE[\W_]*(\d+)[\W_]*(?:-[\W_]*(\d+))?[\W_]*%', text_upper)
        if size_match:
            val1 = float(size_match.group(1))
            val2 = float(size_match.group(2)) if size_match.group(2) else val1
            data['equity_perc'] = (val1 + val2) / 2
        else:
            data['equity_perc'] = 1.0

        # --- ENTRY ---
        entry_match = re.search(r'ENTRY[\W_]*' + cls.NUM_PATTERN + r'(?:[\W_]*-[\W_]*' + cls.NUM_PATTERN + r')?',
                                text_upper)
        if entry_match:
            entry1 = cls._extract_number(entry_match.group(1))
            entry2 = cls._extract_number(entry_match.group(2))
            if entry1 and entry2:
                data['entry'] = (entry1 + entry2) / 2
            elif entry1:
                data['entry'] = entry1
            else:
                data['entry'] = "Market"
        else:
            data['entry'] = "Market"

        # --- STOP LOSS ---
        sl_match = re.search(r'SL[\W_]*' + cls.NUM_PATTERN, text_upper)
        data['sl'] = cls._extract_number(sl_match.group(1)) if sl_match else None

        # --- LEVERAGE ---
        lev_match = re.search(r'LEV(?:ERAGE)?[\W_]*(\d+)', text_upper)
        data['leverage'] = int(lev_match.group(1)) if lev_match else 20

        # --- TAKE PROFIT TARGETS ---
        tp_matches = re.findall(r'TP\d[\W_]*' + cls.NUM_PATTERN + r'(?!\s*R:R)', text_upper)

        real_tps = []
        for tp_str in tp_matches:
            tp_val = cls._extract_number(tp_str)
            if tp_val:
                real_tps.append(tp_val)

        data['tps'] = real_tps[:3]
        data['type'] = 'TRADE'

        if debug:
            print(f" PARSED SIGNAL: {data}")
        return data


class UpdateParser:
    """
    Parses 'Update' messages like:
    "ASTER/USDT #1175 change TP1 to 0.75222"
    "BTC/USDT change SL to 94000"
    """

    @staticmethod
    def parse(text: str, debug: bool = True) -> dict | None:
        text_upper = text.upper()

        if not any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET", "UPDATE"]):
            return None

        data = {}

        pair_match = re.search(r'([A-Z0-9]+)[\W_]*[/_:-][\W_]*([A-Z0-9]+)', text_upper)
        if not pair_match:
            if debug:
                print(" Update detected but NO PAIR found. Ignoring.")
            return None

        data['symbol'] = f"{pair_match.group(1)}_{pair_match.group(2)}"

        sl_match = re.search(r'(?:SL|STOP(?:\s*LOSS)?)\W+(?:IS\W+)?(?:NOW|TO|BE)\W+([\d,.]+)', text_upper)
        if sl_match:
            data['type'] = 'SL'
            data['price'] = float(sl_match.group(1).replace(',', ''))
            if debug:
                print(f" PARSED UPDATE: {data}")
            return data

        tp_match = re.search(r'(TP\d?)\W+(?:IS\W+)?(?:NOW|TO|BE)\W+([\d,.]+)', text_upper)
        if tp_match:
            data['type'] = tp_match.group(1)
            data['price'] = float(tp_match.group(2).replace(',', ''))
            if debug:
                print(f" PARSED UPDATE: {data}")
            return data

        return None


def parse_signal(text: str) -> dict | None:
    """Wrapper for backward compatibility"""
    try:
        return SignalParser.parse(text, debug=True)
    except Exception as e:
        logger.error(f"Parse Error: {e}")
        return None


# --- UPDATE EXECUTION ---

async def execute_update_signal(data):
    """
    Executes an UPDATE by MODIFYING the existing Position TP/SL order.
    """
    symbol_raw = data['symbol']
    formatted_symbol = symbol_raw.replace('_', '-')
    update_type = data['type']
    new_price_raw = data['price']

    print(f"  PROCESSING UPDATE: {formatted_symbol} {update_type} -> {new_price_raw}")

    # Get instrument info for price precision
    inst_info = await BlofinAPI.get_instrument_info(formatted_symbol)
    tick_size = float(inst_info.get('tickSize', 0.00001)) if inst_info else 0.00001
    final_price = adjust_price_to_step(new_price_raw, tick_size)

    # Get existing TPSL orders
    tpsl_orders = await BlofinAPI.get_tpsl_orders(formatted_symbol)

    print(f" DEBUG: Found {len(tpsl_orders)} active TPSL orders for {formatted_symbol}")

    # Try to get position info
    position = None
    position_side = None
    hold_vol = None
    margin_mode = "isolated"

    positions = await BlofinAPI.get_open_positions(formatted_symbol)
    if positions and len(positions) > 0:
        position = positions[0]
        pos_side = position.positionType
        hold_vol = abs(position.holdVol)
        margin_mode = position.marginMode or "isolated"

        if pos_side == "net":
            position_side = "long" if position.holdVol > 0 else "short"
        else:
            position_side = pos_side

    # Fallback: get position info from existing TPSL orders
    if not position_side and tpsl_orders:
        first_tpsl = tpsl_orders[0]
        hold_vol = float(first_tpsl.get('size', 0))
        position_side = first_tpsl.get('posSide', 'long')
        margin_mode = first_tpsl.get('marginMode', 'isolated')

    # Fallback: get from order history
    if not position_side:
        history = await BlofinAPI.get_order_history(symbol=formatted_symbol)
        if history:
            for h in history:
                if h.get('state') == 'filled':
                    hold_vol = float(h.get('filledSize', 0))
                    side = h.get('side', 'buy')
                    position_side = "long" if side == "buy" else "short"
                    break

    if not tpsl_orders and not position_side:
        return f"  Update Ignored: No position or TPSL orders found for {formatted_symbol}"

    target_order = None
    for order in tpsl_orders:
        tpsl_id = order.get('tpslId')
        order_type = order.get('tpslType', '')
        tp_trigger = order.get('tpTriggerPrice')
        sl_trigger = order.get('slTriggerPrice')

        print(f"    - ID={tpsl_id} | Type={order_type} | TP={tp_trigger} | SL={sl_trigger}")

        if update_type == 'SL' and (order_type in ['sl', 'tpsl'] or sl_trigger):
            target_order = {
                'tpsl_id': tpsl_id,
                'order_type': order_type,
                'curr_tp': tp_trigger,
                'curr_sl': sl_trigger,
                'size': order.get('size'),
                'posSide': order.get('posSide', position_side),
                'marginMode': order.get('marginMode', margin_mode)
            }
            break
        elif 'TP' in update_type and (order_type in ['tp', 'tpsl'] or tp_trigger):
            target_order = {
                'tpsl_id': tpsl_id,
                'order_type': order_type,
                'curr_tp': tp_trigger,
                'curr_sl': sl_trigger,
                'size': order.get('size'),
                'posSide': order.get('posSide', position_side),
                'marginMode': order.get('marginMode', margin_mode)
            }
            break

    if not target_order:
        if not position_side or not hold_vol:
            return f"  Update Failed: Cannot determine position info for {formatted_symbol}"

        print(f" No existing {update_type} order found. Creating new TPSL order...")

        close_side = "sell" if position_side == "long" else "buy"

        tpsl_body = {
            "instId": formatted_symbol,
            "marginMode": margin_mode,
            "posSide": position_side,
            "side": close_side,
            "size": str(hold_vol),
            "reduceOnly": "true"
        }

        if update_type == 'SL':
            tpsl_body["slTriggerPrice"] = str(final_price)
            tpsl_body["slOrderPrice"] = "-1"
        else:
            tpsl_body["tpTriggerPrice"] = str(final_price)
            tpsl_body["tpOrderPrice"] = "-1"

        logger.info(f" Creating new TPSL: {tpsl_body}")
        res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)
        logger.info(f" TPSL Response: {res}")

        if res and res.get('code') == "0":
            return f" SUCCESS: {formatted_symbol} {update_type} set to {final_price}"
        else:
            error_msg = res.get('msg', 'Unknown Error') if res else 'No Response'
            return f"  FAILED to create {update_type}: {error_msg}"

    # Try to amend the existing TPSL order
    print(f" Amending TPSL Order {target_order['tpsl_id']}...")

    if update_type == 'SL':
        res = await BlofinAPI.amend_tpsl_order(
            symbol=formatted_symbol,
            tpsl_id=target_order['tpsl_id'],
            new_sl_trigger_price=final_price
        )
    else:
        res = await BlofinAPI.amend_tpsl_order(
            symbol=formatted_symbol,
            tpsl_id=target_order['tpsl_id'],
            new_tp_trigger_price=final_price
        )

    logger.info(f" Amend TPSL Response: {res}")

    if res and res.get('code') == "0":
        return f" SUCCESS: {formatted_symbol} {update_type} updated to {final_price}"
    else:
        error_msg = res.get('msg', 'Unknown') if res else 'No Response'
        print(f"    Amend failed ({error_msg}), trying cancel & recreate...")

        cancel_res = await BlofinAPI.cancel_tpsl_order(formatted_symbol, target_order['tpsl_id'])
        logger.info(f" Cancel TPSL Response: {cancel_res}")

        if not (cancel_res and cancel_res.get('code') == "0"):
            return f"  FAILED to cancel existing {update_type}: {cancel_res.get('msg', 'Unknown') if cancel_res else 'No Response'}"

        pos_side = target_order.get('posSide', position_side or 'long')
        close_side = "sell" if pos_side == "long" else "buy"
        size = target_order.get('size') or str(hold_vol or 0)

        tpsl_body = {
            "instId": formatted_symbol,
            "marginMode": target_order.get('marginMode', margin_mode),
            "posSide": pos_side,
            "side": close_side,
            "size": size,
            "reduceOnly": "true"
        }

        if update_type == 'SL':
            tpsl_body["slTriggerPrice"] = str(final_price)
            tpsl_body["slOrderPrice"] = "-1"
            if target_order.get('curr_tp'):
                tpsl_body["tpTriggerPrice"] = str(target_order['curr_tp'])
                tpsl_body["tpOrderPrice"] = "-1"
        else:
            tpsl_body["tpTriggerPrice"] = str(final_price)
            tpsl_body["tpOrderPrice"] = "-1"
            if target_order.get('curr_sl'):
                tpsl_body["slTriggerPrice"] = str(target_order['curr_sl'])
                tpsl_body["slOrderPrice"] = "-1"

        logger.info(f" Creating replacement TPSL: {tpsl_body}")
        new_res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)
        logger.info(f" New TPSL Response: {new_res}")

        if new_res and new_res.get('code') == "0":
            return f" SUCCESS: {formatted_symbol} {update_type} updated to {final_price} (via recreate)"
        else:
            new_error = new_res.get('msg', 'Unknown') if new_res else 'No Response'
            return f"  FAILED to recreate {update_type}: {new_error}"


# --- TELEGRAM HANDLER ---
@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    # Ignore old messages sent before bot started
    if event.date < START_TIME:
        return

    text = event.text
    if not text:
        return

    text_upper = text.upper()

    # Route 1: New Trade Signals (PAIR + SIDE)
    if "PAIR" in text_upper and "SIDE" in text_upper:
        print(f"\n--- New Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

        signal_data = parse_signal(text)
        if not signal_data:
            print(" Failed to parse signal.")
            return

        symbol = signal_data['symbol']

        if signal_data['type'] == 'TRADE':
            print(f"  Processing TP3 TRADE for {symbol}...")
            res = await execute_signal_trade(signal_data)
            print(res)

        return

    # Route 2: Update Signals (change TP/SL)
    if any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET"]) and "/" in text:
        print(f"\n--- Update Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

        update_data = UpdateParser.parse(text)

        if update_data:
            result = await execute_update_signal(update_data)
            print(result)
        else:
            print(" Update detected but failed to parse details.")


# --- MAIN EXECUTION ---
async def startup():
    """Initialize bot and load existing positions."""
    await load_existing_positions()
    asyncio.create_task(monitor_orders_and_positions())
    logger.info("Position monitor started")


if __name__ == "__main__":
    print("="*45)
    print("   BLOFIN TP3 BOT (100% Close at TP3)")
    print("="*45)
    print(f" Start Time (UTC): {START_TIME}")
    print(f" Listening to Chats: {TARGET_CHATS}")
    print("-"*45)

    try:
        # Start the client
        client.start()

        # Run startup tasks (load positions, start monitor)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(startup())

        print("Waiting for signals... (Ctrl+C to stop)\n")

        # Run until disconnected
        client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
        import traceback
        traceback.print_exc()
