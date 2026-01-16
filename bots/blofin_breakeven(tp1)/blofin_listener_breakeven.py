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
    SESSION_BREAKEVEN,
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
logger = logging.getLogger("BlofinListener")

# --- INITIALIZE API & CLIENT ---
BlofinAPI = BlofinFuturesAPI(
    api_key=BLOFIN_API_KEY,
    secret_key=BLOFIN_SECRET_KEY,
    passphrase=BLOFIN_PASSPHRASE,
    testnet=True
)

# Telegram Client (using the 'breakeven' session)
client = TelegramClient(str(SESSION_BREAKEVEN), API_ID, API_HASH)

# Track pending orders for monitoring
pending_orders = {}  # {order_id: {symbol, side, size, tp, sl, entry_price}}


# --- HELPER FUNCTIONS ---
def adjust_price_to_step(price, step_size):
    """Rounds a price to the nearest valid step size allowed by the exchange."""
    if not price: return None
    if not step_size or step_size == 0: return price

    step_str = f"{float(step_size):.16f}".rstrip('0')
    precision = 0
    if '.' in step_str:
        precision = len(step_str.split('.')[1])

    return round(price, precision)


# --- TRADE LOGIC ---

async def monitor_order_fills():
    """
    Background task that monitors pending limit orders.
    When an order fills, it sends a notification and sets up TP/SL.
    """
    global pending_orders

    while True:
        try:
            if pending_orders:
                orders_to_remove = []

                for order_id, order_info in list(pending_orders.items()):
                    symbol = order_info['symbol']

                    # Check if position exists for this symbol
                    positions = await BlofinAPI.get_open_positions(symbol)

                    # Also check the actual pending orders to see if our order is still there
                    all_pending = await BlofinAPI.get_pending_orders()  # Get ALL pending orders
                    our_order_pending = any(str(o.get('orderId')) == str(order_id) for o in all_pending)

                    if positions and len(positions) > 0:
                        # Order filled! Position is now open
                        position = positions[0]

                        fill_msg = (
                            f"🚀 **LIMIT ORDER FILLED!**\n"
                            f"   Symbol: {symbol}\n"
                            f"   Side: {order_info['side'].upper()}\n"
                            f"   Entry: {position.openAvgPrice}\n"
                            f"   Size: {position.holdVol}\n"
                            f"   Lev: x{position.leverage}\n"
                        )

                        # Set up TP/SL via separate call (since Blofin ignores them on order)
                        tp_price = order_info.get('tp')
                        sl_price = order_info.get('sl')

                        if tp_price or sl_price:
                            tpsl_side = "sell" if order_info['side'] == "buy" else "buy"

                            tpsl_body = {
                                "instId": symbol,
                                "marginMode": "isolated",
                                "posSide": "net",
                                "side": tpsl_side,
                                "size": str(position.holdVol),
                                "reduceOnly": "true",
                            }

                            if tp_price:
                                tpsl_body["tpTriggerPrice"] = str(tp_price)
                                tpsl_body["tpOrderPrice"] = "-1"

                            if sl_price:
                                tpsl_body["slTriggerPrice"] = str(sl_price)
                                tpsl_body["slOrderPrice"] = "-1"

                            tpsl_res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)

                            if tpsl_res and tpsl_res.get('code') == "0":
                                fill_msg += f"   ✓ TP/SL Set Successfully\n"
                                if tp_price:
                                    fill_msg += f"   TP: {tp_price}\n"
                                if sl_price:
                                    fill_msg += f"   SL: {sl_price}\n"
                            else:
                                error = tpsl_res.get('msg', 'Unknown') if tpsl_res else 'No response'
                                fill_msg += f"   ⚠️ TP/SL Failed: {error}\n"

                        logger.info(f"Order {order_id} filled for {symbol}")
                        print(f"\n{fill_msg}")

                        # Send notification to Telegram
                        # try:
                        #     await client.send_message('me', fill_msg)
                        # except Exception as e:
                        #     logger.error(f"Failed to send fill notification: {e}")

                        orders_to_remove.append(order_id)

                    elif our_order_pending:
                        # Order still pending, keep monitoring
                        logger.debug(f"Order {order_id} still pending for {symbol}")

                    else:
                        # No position AND order not in pending list = cancelled
                        # But increment a counter to avoid false positives
                        check_count = order_info.get('_check_count', 0) + 1
                        order_info['_check_count'] = check_count

                        if check_count >= 3:  # Only mark as cancelled after 3 checks (15 seconds)
                            logger.info(f"Order {order_id} for {symbol} appears to be cancelled")
                            orders_to_remove.append(order_id)

                # Remove processed orders
                for order_id in orders_to_remove:
                    del pending_orders[order_id]

            await asyncio.sleep(5)  # Check every 5 seconds

        except Exception as e:
            logger.error(f"Monitor error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(10)


async def move_sl_to_entry(symbol: str):
    """
    Logic:
    1. Check if we have an open position for the symbol.
    2. Identify the entry price.
    3. Update the Stop Loss (SL) to the entry price to 'break even'.
    """
    logger.info(f"Attempting BREAKEVEN for {symbol}...")

    # Blofin symbols usually use dashes (BTC-USDT). Ensure format is correct.
    formatted_symbol = symbol.replace('_', '-')

    # 1. Get Open Positions
    positions = await BlofinAPI.get_open_positions(formatted_symbol)

    if not positions:
        return f"⚠️ Cannot Move SL: No open position found for {formatted_symbol}"

    # Assuming we only have one position per symbol (standard for most bots)
    position = positions[0]
    entry_price = position.openAvgPrice
    pos_side = position.positionType  # 'long', 'short', or 'net'

    # 2. Determine SL Trigger Price
    # For now, we set it exactly to entry. You could add a small buffer here if desired.
    final_sl_price = entry_price

    print(f"   > Found Position: {pos_side.upper()} @ {entry_price}. Moving SL...")

    # 3. Send Request to Blofin to Update TPSL
    # Blofin Endpoint: /api/v1/trade/order-tpsl (creates conditional order)
    # NOTE: Blofin manages TP/SL as separate conditional orders

    # Determine exit side and position side for TPSL
    # For net positions, we need to determine direction from holdVol or assume long if positive
    if pos_side == "net":
        # Net mode - need to determine actual direction
        # Positive holdVol typically means long
        position_side = "long" if position.holdVol > 0 else "short"
    else:
        position_side = pos_side

    exit_side = "sell" if position_side == "long" else "buy"

    req_body = {
        "instId": formatted_symbol,
        "marginMode": "isolated",
        "posSide": position_side,  # Use explicit long/short
        "side": exit_side,
        "size": str(position.holdVol),  # Use actual position size
        "reduceOnly": "true",  # Links SL to position - closes with position
        "slTriggerPrice": str(final_sl_price),
        "slOrderPrice": "-1"  # -1 indicates "Market Price" execution for the SL
    }

    logger.info(f" Breakeven TPSL request: {req_body}")

    # We use the generic _make_request method from your API wrapper
    res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=req_body)
    logger.info(f" Breakeven TPSL response: {res}")

    if res and res.get('code') == "0":
        return (f"✅ **SL Updated to Entry!**\n"
                f"   Symbol: {formatted_symbol}\n"
                f"   New SL: {final_sl_price}\n"
                f"   (Breakeven Successful)")
    else:
        error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
        return f"❌ **Failed to Move SL**\n   Error: {error_msg}"


async def execute_signal_trade(data):
    """
    Logic:
    1. Place LIMIT Order at the entry price specified in the signal.
    2. TP/SL will be attached when the limit order fills.
    """
    symbol_raw = data['symbol']
    formatted_symbol = symbol_raw.replace('_', '-')

    side = data['side']
    leverage = data['leverage']
    equity_perc = data['equity_perc']
    entry_price = data['entry']  # This is now the limit price

    # Get TP/SL
    sl_price = data.get('sl')
    tps = data.get('tps', [])
    tp1_price = tps[0] if tps else None

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

    # Log full instrument info for debugging
    logger.info(f" Instrument Info: {inst_info}")

    # Blofin size is in contracts. contractValue = value per contract in quote currency (USDT)
    # For DOGE-USDT with contractValue=1000, 1 contract = 1000 DOGE
    # But the API expects size as number of contracts (can be decimal like 0.1)
    contract_value = float(inst_info.get('contractValue', 1))  # Value per contract in base units
    lot_size = float(inst_info.get('lotSize', 1))  # Minimum order increment
    min_size = float(inst_info.get('minSize', lot_size))  # Minimum order size

    logger.info(f" Contract Value: {contract_value} | Lot Size: {lot_size} | Min Size: {min_size}")

    # Get tick size for price precision
    tick_size = float(inst_info.get('tickSize', 0.00001))

    # Round entry price to tick size
    entry_price = adjust_price_to_step(entry_price, tick_size)

    # Round TP/SL to tick size
    if tp1_price:
        tp1_price = adjust_price_to_step(tp1_price, tick_size)
    if sl_price:
        sl_price = adjust_price_to_step(sl_price, tick_size)

    logger.info(f" Tick Size: {tick_size} | Entry (rounded): {entry_price}")

    # Calc Volume:
    # margin_amount = how much USDT we want to use as margin
    # notional_value = margin_amount * leverage (total position value in USDT)
    # Each contract is worth: contractValue * price (in USDT)
    # Number of contracts = notional_value / (contractValue * price)
    margin_amount = balance * (equity_perc / 100.0)
    notional_value = margin_amount * leverage
    contract_usdt_value = contract_value * entry_price  # USDT value of 1 contract
    calculated_vol = notional_value / contract_usdt_value

    logger.info(f" Calc: {notional_value:.2f} / {contract_usdt_value:.2f} = {calculated_vol:.4f} contracts")

    # Round to valid lot size (lot_size can be decimal like 0.1)
    final_vol = round(calculated_vol / lot_size) * lot_size

    # Ensure minimum size
    if final_vol < min_size:
        final_vol = min_size

    # Format to avoid floating point issues (e.g., 1.0000000001)
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

    # Determine if we should use limit or market order
    # BUY limit: entry should be BELOW current price (buying on dip)
    # SELL limit: entry should be ABOVE current price (selling on rally)
    use_market_order = False

    if blofin_side == "buy":
        if entry_price >= current_price:
            # Entry is at or above market - would fill immediately, use market order
            logger.info(f" Entry >= Market for BUY, using MARKET order at {current_price}")
            use_market_order = True
    else:  # sell
        if entry_price <= current_price:
            # Entry is at or below market - would fill immediately, use market order
            logger.info(f" Entry <= Market for SELL, using MARKET order at {current_price}")
            use_market_order = True

    # Validate TP/SL relative to entry/current price
    actual_entry = current_price if use_market_order else entry_price

    if blofin_side == "buy":
        # For LONG: TP should be ABOVE entry, SL should be BELOW entry
        if tp1_price and tp1_price <= actual_entry:
            logger.warning(f"TP1 ({tp1_price}) should be above entry ({actual_entry}) for LONG - skipping TP")
            tp1_price = None
        if sl_price and sl_price >= actual_entry:
            logger.warning(f"SL ({sl_price}) should be below entry ({actual_entry}) for LONG - skipping SL")
            sl_price = None
    else:  # sell/short
        # For SHORT: TP should be BELOW entry, SL should be ABOVE entry
        if tp1_price and tp1_price >= actual_entry:
            logger.warning(f"TP1 ({tp1_price}) should be below entry ({actual_entry}) for SHORT - skipping TP")
            tp1_price = None
        if sl_price and sl_price <= actual_entry:
            logger.warning(f"SL ({sl_price}) should be above entry ({actual_entry}) for SHORT - skipping SL")
            sl_price = None

    if use_market_order:
        logger.info(f" Placing MARKET {blofin_side.upper()} {formatted_symbol} x{leverage} | Vol: {final_vol}")

        # Place market order WITHOUT TP/SL first (Blofin demo has issues with TP/SL on market orders)
        # We'll set TP/SL separately after the order fills
        res = await BlofinAPI.create_market_order(
            symbol=formatted_symbol,
            side=blofin_side,
            vol=final_vol,
            leverage=leverage,
            position_side=pos_side
            # NOT passing TP/SL here - will set separately
        )

        logger.info(f"Order Response: {res}")

        if res and res.get('code') == "0":
            order_data = res.get('data', [])
            order_id = order_data[0].get('orderId', 'N/A') if isinstance(order_data, list) and order_data else 'N/A'

            order_msg = (
                f"🚀 **MARKET ORDER EXECUTED (Blofin)**\n"
                f"   Symbol: {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   Entry: Market (~{current_price})\n"
                f"   Size: {final_vol}\n"
                f"   Lev: x{leverage}\n"
            )

            # Set TP/SL via separate TPSL order AFTER market order fills
            if tp1_price or sl_price:
                # Wait for order to fill and price to settle
                await asyncio.sleep(1.5)

                # Fetch current market price for validation
                ticker_res2 = await BlofinAPI._make_request("GET", "/api/v1/market/tickers", params={"instId": formatted_symbol})
                latest_price = current_price
                if ticker_res2 and ticker_res2.get('data'):
                    latest_price = float(ticker_res2['data'][0]['last'])

                logger.info(f" Current price after fill: {latest_price}")

                # Validate TP/SL against current market price
                valid_tp = tp1_price
                valid_sl = sl_price

                if blofin_side == "buy":
                    # For LONG: TP must be above current, SL must be below current
                    if valid_tp and valid_tp <= latest_price:
                        logger.warning(f"TP ({valid_tp}) not above current price ({latest_price}) - skipping TP")
                        valid_tp = None
                    if valid_sl and valid_sl >= latest_price:
                        logger.warning(f"SL ({valid_sl}) not below current price ({latest_price}) - skipping SL")
                        valid_sl = None
                else:
                    # For SHORT: TP must be below current, SL must be above current
                    if valid_tp and valid_tp >= latest_price:
                        logger.warning(f"TP ({valid_tp}) not below current price ({latest_price}) - skipping TP")
                        valid_tp = None
                    if valid_sl and valid_sl <= latest_price:
                        logger.warning(f"SL ({valid_sl}) not above current price ({latest_price}) - skipping SL")
                        valid_sl = None

                if valid_tp or valid_sl:
                    tpsl_success = []
                    tpsl_errors = []

                    # Set TP and SL in SEPARATE calls
                    # For closing a position: side is opposite of position direction
                    # posSide should indicate the position we're closing
                    close_side = "sell" if blofin_side == "buy" else "buy"
                    position_side = "long" if blofin_side == "buy" else "short"

                    if valid_tp:
                        tp_body = {
                            "instId": formatted_symbol,
                            "marginMode": "isolated",
                            "posSide": position_side,  # The position we're closing
                            "side": close_side,        # The action to close it
                            "size": str(final_vol),
                            "reduceOnly": "true",
                            "tpTriggerPrice": str(valid_tp),
                            "tpOrderPrice": "-1"
                        }
                        logger.info(f" Setting TP: {tp_body}")
                        tp_res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=tp_body)
                        logger.info(f" TP Response: {tp_res}")

                        if tp_res and tp_res.get('code') == "0":
                            tpsl_success.append(f"TP1: {valid_tp}")
                            order_msg += f"   TP1: {valid_tp}\n"
                        else:
                            error = tp_res.get('msg', 'Unknown') if tp_res else 'No response'
                            tpsl_errors.append(f"TP: {error}")

                    if valid_sl:
                        sl_body = {
                            "instId": formatted_symbol,
                            "marginMode": "isolated",
                            "posSide": position_side,  # The position we're closing
                            "side": close_side,        # The action to close it
                            "size": str(final_vol),
                            "reduceOnly": "true",
                            "slTriggerPrice": str(valid_sl),
                            "slOrderPrice": "-1"
                        }
                        logger.info(f" Setting SL: {sl_body}")
                        sl_res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=sl_body)
                        logger.info(f" SL Response: {sl_res}")

                        if sl_res and sl_res.get('code') == "0":
                            tpsl_success.append(f"SL: {valid_sl}")
                            order_msg += f"   SL: {valid_sl}\n"
                        else:
                            error = sl_res.get('msg', 'Unknown') if sl_res else 'No response'
                            tpsl_errors.append(f"SL: {error}")

                    if tpsl_success:
                        order_msg += f"   ✓ Set: {', '.join(tpsl_success)}\n"
                    if tpsl_errors:
                        order_msg += f"   ⚠️ Failed: {'; '.join(tpsl_errors)}"
                else:
                    order_msg += "   ⚠️ TP/SL skipped (invalid vs current price)"

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

    # 2. Place LIMIT Order at entry price (TP/SL will be set by monitor when order fills)
    res = await BlofinAPI.create_limit_order(
        symbol=formatted_symbol,
        side=blofin_side,
        vol=final_vol,
        price=entry_price,
        leverage=leverage,
        position_side=pos_side
        # Note: NOT passing TP/SL here - Blofin ignores them on limit orders
        # The monitor will set TP/SL when the order fills
    )

    logger.info(f"Order Response: {res}")

    if res and res.get('code') == "0":
        order_data = res.get('data', [])
        order_id = order_data[0].get('orderId', 'N/A') if isinstance(order_data, list) and order_data else 'N/A'

        # Store order for monitoring (with TP/SL info for when it fills)
        if order_id != 'N/A':
            pending_orders[order_id] = {
                'symbol': formatted_symbol,
                'side': blofin_side,
                'size': final_vol,
                'entry_price': entry_price,
                'tp': tp1_price,
                'sl': sl_price,
                'leverage': leverage
            }
            logger.info(f"Added order {order_id} to monitoring queue")

        order_msg = (
            f"📋 **LIMIT ORDER PLACED (Blofin)**\n"
            f"   Symbol: {formatted_symbol}\n"
            f"   Side: {blofin_side.upper()}\n"
            f"   Entry: {entry_price}\n"
            f"   Size: {final_vol}\n"
            f"   Lev: x{leverage}\n"
            f"   Order ID: {order_id}\n"
        )

        if tp1_price:
            order_msg += f"   TP1: {tp1_price} (set on fill)\n"
        if sl_price:
            order_msg += f"   SL: {sl_price} (set on fill)\n"

        order_msg += "   ⏳ Waiting for price to reach entry..."

        return order_msg

    else:
        error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
        # Extract detailed error from data array
        data = res.get('data', [])
        if data and isinstance(data, list) and len(data) > 0 and data[0].get('msg'):
            error_msg = data[0].get('msg')
        return f"❌ **Limit Order Failed**\n   Error: {error_msg}"

# --- SIGNAL PARSER ---
def parse_signal(text: str):
    """
    Parses Telegram messages to extract trade details.
    Compatible with standard formats: "PAIR: BTC/USDT SIDE: LONG..."
    """
    try:
        # Remove common hidden/invisible Unicode characters first
        # This includes: zero-width spaces, non-breaking spaces, soft hyphens, etc.
        hidden_chars = [
            '\u200b',  # Zero-width space
            '\u200c',  # Zero-width non-joiner
            '\u200d',  # Zero-width joiner
            '\u200e',  # Left-to-right mark
            '\u200f',  # Right-to-left mark
            '\u00a0',  # Non-breaking space
            '\u2060',  # Word joiner
            '\ufeff',  # Byte order mark
            '\u00ad',  # Soft hyphen
            '\u2007',  # Figure space
            '\u2008',  # Punctuation space
            '\u2009',  # Thin space
            '\u200a',  # Hair space
            '\u202f',  # Narrow no-break space
            '\u205f',  # Medium mathematical space
            '\u3000',  # Ideographic space
        ]
        for char in hidden_chars:
            text = text.replace(char, ' ')

        # Convert to ASCII to remove emojis/weird chars
        text = text.encode('ascii', 'ignore').decode('ascii')
        text_clean = re.sub(r'[^a-zA-Z0-9\s.:,/%-]', '', text)
        text_clean = re.sub(r'\s+', ' ', text_clean).strip()

        logger.debug(f"Cleaned text: {text_clean}")

        # Ignore Status Updates
        if "TARGET HIT" in text_clean.upper() or "PROFIT:" in text_clean.upper():
            return {'valid': False, 'error': 'Ignored: Status/Profit update'}

        # 1. Parse Pair
        pair_match = re.search(r"PAIR:\s*([A-Z0-9]+)[/_]([A-Z0-9]+)", text_clean, re.IGNORECASE)
        if not pair_match:
            return {'valid': False, 'error': 'Parsing Failed: No PAIR found'}

        symbol = f"{pair_match.group(1)}_{pair_match.group(2)}".upper()

        # 2. Check for Breakeven Command
        if "MOVE SL TO ENTRY" in text_clean.upper():
            return {
                'valid': True,
                'type': 'BREAKEVEN',
                'symbol': symbol
            }

        # 3. Parse Side (Long/Short)
        side_match = re.search(r"SIDE:\s*(LONG|SHORT)", text_clean, re.IGNORECASE)
        if not side_match:
            return {'valid': False, 'error': f"Parsing Failed: Pair {symbol} found, but missing SIDE"}

        direction = side_match.group(1).upper()
        # We use standard naming for internal logic
        from mexcpy.mexcTypes import OrderSide as MexcOrderSide
        side = MexcOrderSide.OpenLong if direction == "LONG" else MexcOrderSide.OpenShort

        # 4. Parse Size/Equity %
        size_match = re.search(r"SIZE:\s*(\d+)(?:\s*-\s*(\d+))?\s*%", text_clean, re.IGNORECASE)
        equity_perc = 1.0
        if size_match:
            val1 = float(size_match.group(1))
            val2 = float(size_match.group(2)) if size_match.group(2) else val1
            equity_perc = (val1 + val2) / 2
            logger.info(f"Parsed SIZE: {val1}-{val2}% -> Midpoint: {equity_perc}%")
        else:
            # Debug: show what's around SIZE in the cleaned text
            size_area = re.search(r"SIZE.{0,20}", text_clean, re.IGNORECASE)
            if size_area:
                logger.warning(f"SIZE not matched. Text around SIZE: '{size_area.group()}'")
            else:
                logger.warning(f"SIZE not found in text at all, using default 1%")

        # 5. Parse Entry Price (REQUIRED for limit orders)
        entry_match = re.search(r"ENTRY:\s*([\d,.]+)", text_clean, re.IGNORECASE)
        if not entry_match:
            return {'valid': False, 'error': f"Parsing Failed: Pair {symbol} found, but missing ENTRY price"}

        entry_price = float(entry_match.group(1).replace(',', ''))

        # 6. Parse SL
        sl_match = re.search(r"SL:\s*([\d,.]+)", text_clean, re.IGNORECASE)
        sl_price = float(sl_match.group(1).replace(',', '')) if sl_match else None

        # 7. Parse Leverage
        lev_match = re.search(r"LEVERAGE:\s*(\d+)", text_clean, re.IGNORECASE)
        leverage = int(lev_match.group(1)) if lev_match else 20

        # 8. Parse TPs
        all_tps = re.findall(r"TP\d:\s*([\d,.]+)", text_clean, re.IGNORECASE)
        real_tps = []
        if all_tps:
            limit = 3 if len(all_tps) >= 3 else len(all_tps)
            real_tps = [float(tp.replace(',', '')) for tp in all_tps[:limit]]

        return {
            'valid': True,
            'type': 'TRADE',
            'symbol': symbol,
            'side': side,
            'equity_perc': equity_perc,
            'leverage': leverage,
            'entry': entry_price,
            'sl': sl_price,
            'tps': real_tps
        }

    except Exception as e:
        logger.error(f"Parse Exception: {e}")
        return {'valid': False, 'error': f"Exception: {str(e)}"}


# --- TELEGRAM HANDLER ---
@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    # Ignore old messages sent before bot started
    if event.date < START_TIME:
        return

    text = event.text

    # Basic filter to ensure we only look at signals
    if "PAIR:" in text.upper():
        print(f"\n--- Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

        result = parse_signal(text)

        if not result['valid']:
            # Log errors nicely
            if "Ignored" in result['error']:
                print(f" -> {result['error']}")
            else:
                print(f"  ⚠️ {result['error']}")
            return

        symbol = result['symbol']

        # ROUTING
        if result['type'] == 'BREAKEVEN':
            print(f"  🔄 Processing BREAKEVEN for {symbol}...")
            res = await move_sl_to_entry(symbol)
            print(res)
            # Send feedback to "Saved Messages" (me)
            # await client.send_message('me', res)

        elif result['type'] == 'TRADE':
            print(f"  ⚡ Processing TRADE for {symbol}...")
            res = await execute_signal_trade(result)
            print(res)
            # Send feedback to "Saved Messages" (me)
            # await client.send_message('me', res)


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    print("=======================================")
    print("   BLOFIN TRADING LISTENER (DEMO)      ")
    print("=======================================")
    print(f" Start Time (UTC): {START_TIME}")
    print(f" Listening to Chats: {TARGET_CHATS}")
    print(f" Session File: {SESSION_BREAKEVEN}")
    print("---------------------------------------")
    print("Waiting for signals... (Press Ctrl+C to stop)")

    try:
        # Start the client
        client.start()

        # Get the event loop and create the monitor task
        loop = asyncio.get_event_loop()
        loop.create_task(monitor_order_fills())
        logger.info("Order fill monitor started")

        # Run until disconnected
        client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")