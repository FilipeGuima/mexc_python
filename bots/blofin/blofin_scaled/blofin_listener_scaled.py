"""
Blofin Scaled Exit Strategy Bot

Strategy:
- TP1: Close 50% of position
- TP2: Close 50% of remaining (25% of original) + Move SL to entry
- TP3: Close all remaining (25% of original)

Position lifecycle:
100% -> TP1 hit -> 50% remaining
50% -> TP2 hit -> 25% remaining, SL moved to entry
25% -> TP3 hit or SL hit -> 0% (closed)
"""

import asyncio
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

current_path = Path(__file__).resolve()
project_root = current_path.parent.parent.parent
sys.path.append(str(project_root))

# --- LIBRARIES ---
from telethon import TelegramClient, events

# --- PROJECT IMPORTS ---
from blofincpy.api import BlofinFuturesAPI
from bots.blofin.blofin_scaled.state_manager import save_state, load_state
from common.parser import SignalParser, parse_signal
from common.utils import adjust_price_to_step, validate_signal_tp_sl

from mexcpy.config import (
    API_ID,
    API_HASH,
    TARGET_CHATS,
    SESSION_SCALED,
    BLOFIN_API_KEY,
    BLOFIN_SECRET_KEY,
    BLOFIN_PASSPHRASE,
    BLOFIN_TESTNET
)

# --- CONFIGURATION CHECK ---
if not BLOFIN_API_KEY or not BLOFIN_SECRET_KEY or not BLOFIN_PASSPHRASE:
    print("CRITICAL ERROR: Blofin credentials (API_KEY, SECRET, PASSPHRASE) are missing from config.py or .env")
    exit(1)

START_TIME = datetime.now(timezone.utc)

# --- LOGGING SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("BlofinScaledListener")

# --- INITIALIZE API & CLIENT ---
BlofinAPI = BlofinFuturesAPI(
    api_key=BLOFIN_API_KEY,
    secret_key=BLOFIN_SECRET_KEY,
    passphrase=BLOFIN_PASSPHRASE,
    testnet=BLOFIN_TESTNET
)

# Telegram Client
client = TelegramClient(str(SESSION_SCALED), API_ID, API_HASH)

# Cache for instrument lot sizes
_lot_size_cache = {}


async def get_lot_size(symbol: str) -> float:
    """Get the lot size for a symbol from API (cached)."""
    if symbol in _lot_size_cache:
        return _lot_size_cache[symbol]

    try:
        info = await BlofinAPI.get_instrument_info(symbol)
        if info:
            lot_size = float(info.get('lotSize', 1))
            _lot_size_cache[symbol] = lot_size
            logger.info(f"Lot size for {symbol}: {lot_size}")
            return lot_size
    except Exception as e:
        logger.warning(f"Failed to get lot size for {symbol}: {e}")

    # Default to 1 (whole contracts) if API fails
    _lot_size_cache[symbol] = 1.0
    return 1.0


@dataclass
class ScaledPosition:
    """Tracks a position with scaled exit strategy."""
    symbol: str
    side: str  # 'buy' or 'sell'
    original_size: float
    remaining_size: float
    entry_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    sl_price: float
    leverage: int

    # State tracking
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    sl_hit: bool = False

    # TPSL order IDs for tracking
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    tp3_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None

    # Live tracking data
    unrealized_pnl: float = 0.0
    mark_price: float = 0.0

    @property
    def position_side(self) -> str:
        return "long" if self.side == "buy" else "short"

    @property
    def close_side(self) -> str:
        return "sell" if self.side == "buy" else "buy"

    @property
    def is_closed(self) -> bool:
        return self.remaining_size <= 0 or self.sl_hit or self.tp3_hit


# Track pending limit orders
pending_orders = {}  # {order_id: order_info}

# Track active scaled positions
scaled_positions = {}  # {symbol: ScaledPosition}


def restore_positions_from_state():
    """Restore scaled positions from saved state file."""
    global scaled_positions

    saved = load_state()
    if not saved:
        return 0

    restored = 0
    for symbol, data in saved.items():
        try:
            pos = ScaledPosition(
                symbol=data['symbol'],
                side=data['side'],
                original_size=data['original_size'],
                remaining_size=data['remaining_size'],
                entry_price=data['entry_price'],
                tp1_price=data['tp1_price'],
                tp2_price=data['tp2_price'],
                tp3_price=data['tp3_price'],
                sl_price=data['sl_price'],
                leverage=data['leverage'],
                tp1_hit=data.get('tp1_hit', False),
                tp2_hit=data.get('tp2_hit', False),
                tp3_hit=data.get('tp3_hit', False),
                sl_hit=data.get('sl_hit', False),
                tp1_order_id=data.get('tp1_order_id'),
                tp2_order_id=data.get('tp2_order_id'),
                tp3_order_id=data.get('tp3_order_id'),
                sl_order_id=data.get('sl_order_id'),
            )

            # Only restore if not closed
            if not pos.is_closed:
                scaled_positions[symbol] = pos
                restored += 1
                logger.info(f"Restored position: {symbol} (TP1: {'hit' if pos.tp1_hit else 'pending'}, TP2: {'hit' if pos.tp2_hit else 'pending'})")
        except Exception as e:
            logger.error(f"Failed to restore {symbol}: {e}")

    return restored


async def check_api_positions():
    """Check API for any positions not in our state (e.g., opened externally)."""
    try:
        positions = await BlofinAPI.get_open_positions()
        if not positions:
            return

        for pos in positions:
            if pos.symbol not in scaled_positions:
                pnl_str = f"+{pos.unrealized:.2f}" if pos.unrealized >= 0 else f"{pos.unrealized:.2f}"
                print(f"  ⚠ Found untracked position: {pos.symbol} | PnL: {pnl_str}")
                print(f"    (Position was opened outside this bot - monitoring only)")
    except Exception as e:
        logger.warning(f"Could not check API positions: {e}")


def save_positions():
    """Save current positions to state file."""
    save_state(scaled_positions)


# --- HELPER FUNCTIONS ---
def round_size_to_lot(size, lot_size):
    """Rounds size to valid lot size."""
    if not lot_size or lot_size == 0:
        return size
    return round(round(size / lot_size) * lot_size, 8)


# --- TPSL ORDER MANAGEMENT ---

async def create_tpsl_order(symbol: str, position_side: str, close_side: str,
                            size: float, tp_price: float = None, sl_price: float = None) -> dict:
    """Create a TPSL order for partial or full position."""
    body = {
        "instId": symbol,
        "marginMode": "isolated",
        "posSide": position_side,
        "side": close_side,
        "size": str(size),
        "reduceOnly": "true"
    }

    if tp_price:
        body["tpTriggerPrice"] = str(tp_price)
        body["tpOrderPrice"] = "-1"

    if sl_price:
        body["slTriggerPrice"] = str(sl_price)
        body["slOrderPrice"] = "-1"

    logger.info(f" Creating TPSL: {body}")
    res = await BlofinAPI._make_request("POST", "/api/v1/trade/order-tpsl", body=body)
    logger.info(f" TPSL Response: {res}")

    return res


async def cancel_tpsl_by_id(symbol: str, tpsl_id: str) -> bool:
    """Cancel a specific TPSL order."""
    if not tpsl_id:
        return False

    res = await BlofinAPI.cancel_tpsl_order(symbol, tpsl_id)
    return res and res.get('code') == "0"


async def setup_scaled_tpsl(pos: ScaledPosition) -> str:
    """
    Set up the initial TPSL orders for scaled exit strategy.
    - TP1: 50% of position
    - TP2: 25% of position (will be set after TP1 hits to avoid complexity)
    - TP3: 25% of position (will be set after TP2 hits)
    - SL: 100% of position (will be adjusted as TPs hit)
    """
    results = []

    # Get actual lot size from API
    lot_size = await get_lot_size(pos.symbol)

    # Calculate sizes - use actual lot size
    tp1_size = round_size_to_lot(pos.original_size * 0.50, lot_size)  # 50%

    # Validate: TP1 size must be at least 1 lot
    if tp1_size < lot_size:
        # Position too small to split - close 100% at TP1
        logger.warning(f"Position too small to split ({pos.original_size}). Using 100% at each TP.")
        tp1_size = pos.original_size

    # Set TP1 (50%)
    if pos.tp1_price:
        tp1_res = await create_tpsl_order(
            pos.symbol, pos.position_side, pos.close_side,
            tp1_size, tp_price=pos.tp1_price
        )
        if tp1_res and tp1_res.get('code') == "0":
            data = tp1_res.get('data', {})
            # Handle both dict and list responses
            if isinstance(data, list) and data:
                pos.tp1_order_id = data[0].get('tpslId')
            elif isinstance(data, dict):
                pos.tp1_order_id = data.get('tpslId')
            results.append(f"TP1: {pos.tp1_price} (50%)")
        else:
            results.append(f"TP1 FAILED: {tp1_res.get('msg', 'Unknown') if tp1_res else 'No response'}")

    # Set SL (100% initially)
    if pos.sl_price:
        sl_res = await create_tpsl_order(
            pos.symbol, pos.position_side, pos.close_side,
            pos.original_size, sl_price=pos.sl_price
        )
        if sl_res and sl_res.get('code') == "0":
            data = sl_res.get('data', {})
            if isinstance(data, list) and data:
                pos.sl_order_id = data[0].get('tpslId')
            elif isinstance(data, dict):
                pos.sl_order_id = data.get('tpslId')
            results.append(f"SL: {pos.sl_price} (100%)")
        else:
            results.append(f"SL FAILED: {sl_res.get('msg', 'Unknown') if sl_res else 'No response'}")

    return ", ".join(results)


async def handle_tp1_hit(pos: ScaledPosition):
    """
    Called when TP1 is hit (50% closed).
    - Update remaining size to 50%
    - Cancel old SL, create new SL with 50% size
    - Create TP2 order for 50% of remaining (25% of original)
    """
    pos.tp1_hit = True
    lot_size = await get_lot_size(pos.symbol)
    pos.remaining_size = round_size_to_lot(pos.original_size * 0.50, lot_size)

    print(f"\n{'='*40}")
    print(f"🎯 **TP1 HIT** - {pos.symbol}")
    print(f"   Closed: 50% @ {pos.tp1_price}")
    print(f"   Remaining: {pos.remaining_size}")

    # Cancel old SL and create new one with 50% size
    if pos.sl_order_id:
        await cancel_tpsl_by_id(pos.symbol, pos.sl_order_id)

    sl_res = await create_tpsl_order(
        pos.symbol, pos.position_side, pos.close_side,
        pos.remaining_size, sl_price=pos.sl_price
    )
    if sl_res and sl_res.get('code') == "0":
        data = sl_res.get('data', {})
        if isinstance(data, list) and data:
            pos.sl_order_id = data[0].get('tpslId')
        elif isinstance(data, dict):
            pos.sl_order_id = data.get('tpslId')
        print(f"   ✓ SL updated to 50% size")

    # Create TP2 order (50% of remaining = 25% of original)
    tp2_size = round_size_to_lot(pos.remaining_size * 0.50, lot_size)
    if pos.tp2_price:
        tp2_res = await create_tpsl_order(
            pos.symbol, pos.position_side, pos.close_side,
            tp2_size, tp_price=pos.tp2_price
        )
        if tp2_res and tp2_res.get('code') == "0":
            data = tp2_res.get('data', {})
            if isinstance(data, list) and data:
                pos.tp2_order_id = data[0].get('tpslId')
            elif isinstance(data, dict):
                pos.tp2_order_id = data.get('tpslId')
            print(f"   ✓ TP2 set: {pos.tp2_price} (25% of original)")
        else:
            print(f"   ⚠️ TP2 failed: {tp2_res.get('msg', 'Unknown') if tp2_res else 'No response'}")

    print(f"{'='*40}")
    save_positions()  # Persist state


async def handle_tp2_hit(pos: ScaledPosition):
    """
    Called when TP2 is hit (25% more closed, 25% remaining).
    - Update remaining size to 25%
    - Move SL to entry price
    - Create TP3 order for remaining 25%
    """
    pos.tp2_hit = True
    lot_size = await get_lot_size(pos.symbol)
    pos.remaining_size = round_size_to_lot(pos.original_size * 0.25, lot_size)

    print(f"\n{'='*40}")
    print(f"🎯 **TP2 HIT** - {pos.symbol}")
    print(f"   Closed: 25% @ {pos.tp2_price}")
    print(f"   Remaining: {pos.remaining_size}")

    # Cancel old SL and move to ENTRY PRICE (breakeven)
    if pos.sl_order_id:
        await cancel_tpsl_by_id(pos.symbol, pos.sl_order_id)

    sl_res = await create_tpsl_order(
        pos.symbol, pos.position_side, pos.close_side,
        pos.remaining_size, sl_price=pos.entry_price  # SL at entry!
    )
    if sl_res and sl_res.get('code') == "0":
        data = sl_res.get('data', {})
        if isinstance(data, list) and data:
            pos.sl_order_id = data[0].get('tpslId')
        elif isinstance(data, dict):
            pos.sl_order_id = data.get('tpslId')
        print(f"   ✓ SL moved to ENTRY: {pos.entry_price} (breakeven)")

    # Create TP3 order for remaining 25%
    if pos.tp3_price:
        tp3_res = await create_tpsl_order(
            pos.symbol, pos.position_side, pos.close_side,
            pos.remaining_size, tp_price=pos.tp3_price
        )
        if tp3_res and tp3_res.get('code') == "0":
            data = tp3_res.get('data', {})
            if isinstance(data, list) and data:
                pos.tp3_order_id = data[0].get('tpslId')
            elif isinstance(data, dict):
                pos.tp3_order_id = data.get('tpslId')
            print(f"   ✓ TP3 set: {pos.tp3_price} (remaining 25%)")
        else:
            print(f"   ⚠️ TP3 failed: {tp3_res.get('msg', 'Unknown') if tp3_res else 'No response'}")

    print(f"{'='*40}")
    save_positions()  # Persist state


async def handle_tp3_hit(pos: ScaledPosition):
    """Called when TP3 is hit - position fully closed."""
    pos.tp3_hit = True
    pos.remaining_size = 0

    print(f"\n{'='*40}")
    print(f"🏆 **TP3 HIT - POSITION CLOSED** - {pos.symbol}")
    print(f"   Final close @ {pos.tp3_price}")
    print(f"   Entry: {pos.entry_price}")
    print(f"   Strategy completed successfully!")
    print(f"{'='*40}")
    save_positions()  # Persist state


async def handle_sl_hit(pos: ScaledPosition):
    """Called when SL is hit - position closed at stop loss."""
    pos.sl_hit = True

    sl_level = "entry (breakeven)" if pos.tp2_hit else f"{pos.sl_price}"

    print(f"\n{'='*40}")
    print(f"🛑 **STOP LOSS HIT** - {pos.symbol}")
    print(f"   SL triggered @ {sl_level}")
    print(f"   Entry was: {pos.entry_price}")
    if pos.tp1_hit:
        print(f"   TP1 was hit (50% profit taken)")
    if pos.tp2_hit:
        print(f"   TP2 was hit (75% profit taken, SL at breakeven)")
    print(f"{'='*40}")

    pos.remaining_size = 0
    save_positions()  # Persist state


# --- MONITORING ---

async def monitor_scaled_positions():
    """
    Background task that monitors scaled positions for TP/SL hits.
    """
    global scaled_positions, pending_orders

    while True:
        try:
            # === Monitor Pending Limit Orders ===
            if pending_orders:
                orders_to_remove = []
                all_pending = await BlofinAPI.get_pending_orders()

                for order_id, order_info in list(pending_orders.items()):
                    symbol = order_info['symbol']

                    our_order = None
                    for o in all_pending:
                        if str(o.get('orderId')) == str(order_id):
                            our_order = o
                            break

                    if our_order:
                        state = our_order.get('state', '')
                        if state == 'filled':
                            filled_size = float(our_order.get('filledSize', 0))
                            avg_price = float(our_order.get('averagePrice', 0)) or order_info.get('entry_price')
                            await _handle_order_filled(order_id, order_info, filled_size, avg_price)
                            orders_to_remove.append(order_id)
                    else:
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
                                print(f"❌ Order {order_id} cancelled for {symbol}")
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
                                await _handle_order_filled(
                                    order_id, order_info,
                                    order_info.get('size'),
                                    order_info.get('entry_price')
                                )
                                orders_to_remove.append(order_id)

                for oid in orders_to_remove:
                    if oid in pending_orders:
                        del pending_orders[oid]

            # === Monitor Scaled Positions for TP/SL hits ===
            if scaled_positions:
                positions_to_remove = []

                for symbol, pos in list(scaled_positions.items()):
                    if pos.is_closed:
                        positions_to_remove.append(symbol)
                        continue

                    # Check TPSL order history for triggered orders
                    tpsl_history = await BlofinAPI._make_request(
                        "GET",
                        "/api/v1/trade/orders-tpsl-history",
                        params={"instType": "SWAP", "instId": symbol}
                    )

                    if tpsl_history and tpsl_history.get('code') == "0":
                        history_data = tpsl_history.get('data', [])

                        for order in history_data:
                            tpsl_id = order.get('tpslId')
                            state = order.get('state', '')

                            if state not in ['filled', 'triggered']:
                                continue

                            # Check which TP/SL was hit
                            if tpsl_id == pos.tp1_order_id and not pos.tp1_hit:
                                await handle_tp1_hit(pos)
                            elif tpsl_id == pos.tp2_order_id and not pos.tp2_hit:
                                await handle_tp2_hit(pos)
                            elif tpsl_id == pos.tp3_order_id and not pos.tp3_hit:
                                await handle_tp3_hit(pos)
                                positions_to_remove.append(symbol)
                            elif tpsl_id == pos.sl_order_id and not pos.sl_hit:
                                await handle_sl_hit(pos)
                                positions_to_remove.append(symbol)

                    # Check if position still exists
                    positions = await BlofinAPI.get_open_positions(symbol)
                    if positions and len(positions) > 0:
                        # Update live PnL data
                        live_pos = positions[0]
                        pos.unrealized_pnl = live_pos.unrealized
                        pos.mark_price = live_pos.markPrice
                        continue

                    # Fallback: Check TPSL orders
                    tpsl_orders = await BlofinAPI.get_tpsl_orders(symbol)
                    if tpsl_orders and len(tpsl_orders) > 0:
                        continue  # Position still open

                    # Position appears closed
                    check_count = getattr(pos, '_close_check', 0) + 1
                    pos._close_check = check_count

                    if check_count >= 2:
                        # Position closed - determine final reason
                        from blofincpy.blofinTypes import CloseReason
                        reason = await BlofinAPI.get_position_close_reason(symbol)

                        if reason == CloseReason.SL:
                            await handle_sl_hit(pos)
                        elif reason == CloseReason.TP:
                            await handle_tp3_hit(pos)
                        else:
                            print(f"\n📊 Position closed for {symbol} (manual or unknown)")

                        positions_to_remove.append(symbol)

                for sym in positions_to_remove:
                    if sym in scaled_positions:
                        del scaled_positions[sym]

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Monitor error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(10)


async def _handle_order_filled(order_id: str, order_info: dict, filled_size: float, fill_price: float):
    """Handle when a limit order is filled - set up scaled TPSL."""
    symbol = order_info['symbol']
    side = order_info['side']

    print(f"\n{'='*40}")
    print(f"🚀 **ORDER FILLED** - {symbol}")
    print(f"   Side: {side.upper()}")
    print(f"   Entry: {fill_price}")
    print(f"   Size: {filled_size}")
    print(f"   Leverage: x{order_info.get('leverage', 'N/A')}")

    # Create scaled position tracker
    pos = ScaledPosition(
        symbol=symbol,
        side=side,
        original_size=filled_size,
        remaining_size=filled_size,
        entry_price=fill_price,
        tp1_price=order_info.get('tp1'),
        tp2_price=order_info.get('tp2'),
        tp3_price=order_info.get('tp3'),
        sl_price=order_info.get('sl'),
        leverage=order_info.get('leverage', 20)
    )

    # Set up initial TPSL orders
    tpsl_result = await setup_scaled_tpsl(pos)
    print(f"   TPSL: {tpsl_result}")
    print(f"{'='*40}")

    # Add to tracking
    scaled_positions[symbol] = pos
    logger.info(f"Added {symbol} to scaled positions monitoring")
    save_positions()  # Persist state


# --- TRADE EXECUTION ---

async def execute_signal_trade(data):
    """
    Scaled Exit Strategy:
    - TP1: 50% close
    - TP2: 50% of remaining close + SL to entry
    - TP3: Close all remaining
    """
    symbol_raw = data['symbol']
    formatted_symbol = symbol_raw.replace('_', '-')

    side = data['side']
    leverage = data['leverage']
    equity_perc = data['equity_perc']
    entry_price = data['entry']

    # Get TP levels
    sl_price = data.get('sl')
    tps = data.get('tps', [])

    tp1_price = tps[0] if len(tps) >= 1 else None
    tp2_price = tps[1] if len(tps) >= 2 else None
    tp3_price = tps[2] if len(tps) >= 3 else None

    # ===========================================
    # VALIDATION: Check for required TP/SL
    # ===========================================
    validation_error = validate_signal_tp_sl(data)
    if validation_error:
        return validation_error

    if not tp1_price:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {formatted_symbol}\n"
            f"   Reason: NO TP1 - Required for scaled strategy\n"
            f"{'='*50}"
        )

    if not tp2_price:
        logger.warning("No TP2 found - will use TP1 * 1.5 as fallback")
        # Could calculate a default TP2, but for now just warn

    if not tp3_price:
        logger.warning("No TP3 found - scaled exit may not complete fully")

    # ===========================================
    # VALIDATION: Entry price must be a number
    # ===========================================
    if not isinstance(entry_price, (int, float)):
        try:
            entry_price = float(str(entry_price).replace(',', ''))
        except (ValueError, TypeError):
            error_msg = (
                f"\n{'='*50}\n"
                f"❌ **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: INVALID ENTRY PRICE\n"
                f"   \n"
                f"   Entry value: '{entry_price}'\n"
                f"   Expected: A numeric value (e.g., 95000, 0.55)\n"
                f"   \n"
                f"   Cannot place limit order without valid entry price.\n"
                f"{'='*50}"
            )
            return error_msg

    blofin_side = "buy" if side == "LONG" else "sell"
    pos_side = "net"

    # Fetch Balance
    assets = await BlofinAPI.get_user_assets()
    usdt_asset = next((a for a in assets if a.currency == "USDT"), None)

    if not usdt_asset:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {formatted_symbol}\n"
            f"   Reason: WALLET ERROR\n"
            f"   \n"
            f"   USDT balance not found in account.\n"
            f"   Please ensure you have USDT available for trading.\n"
            f"{'='*50}"
        )

    balance = usdt_asset.availableBalance

    # Get instrument info
    inst_info = await BlofinAPI.get_instrument_info(formatted_symbol)
    if not inst_info:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {formatted_symbol}\n"
            f"   Reason: INSTRUMENT ERROR\n"
            f"   \n"
            f"   Could not get contract details for {formatted_symbol}.\n"
            f"   This symbol may not exist or trading may be unavailable.\n"
            f"{'='*50}"
        )

    contract_value = float(inst_info.get('contractValue', 1))
    lot_size = float(inst_info.get('lotSize', 1))
    min_size = float(inst_info.get('minSize', lot_size))
    tick_size = float(inst_info.get('tickSize', 0.00001))

    # Round prices
    entry_price = adjust_price_to_step(entry_price, tick_size)
    tp1_price = adjust_price_to_step(tp1_price, tick_size) if tp1_price else None
    tp2_price = adjust_price_to_step(tp2_price, tick_size) if tp2_price else None
    tp3_price = adjust_price_to_step(tp3_price, tick_size) if tp3_price else None
    sl_price = adjust_price_to_step(sl_price, tick_size) if sl_price else None

    # Calculate volume
    margin_amount = balance * (equity_perc / 100.0)
    notional_value = margin_amount * leverage
    contract_usdt_value = contract_value * entry_price
    calculated_vol = notional_value / contract_usdt_value

    final_vol = round_size_to_lot(calculated_vol, lot_size)
    if final_vol < min_size:
        final_vol = min_size

    logger.info(f" Balance: {balance:.2f} USDT | Size: {equity_perc}% | Vol: {final_vol}")

    # Get current price
    ticker_res = await BlofinAPI._make_request("GET", "/api/v1/market/tickers", params={"instId": formatted_symbol})
    current_price = 0
    if ticker_res and ticker_res.get('data'):
        current_price = float(ticker_res['data'][0]['last'])

    if current_price == 0:
        return (
            f"\n{'='*50}\n"
            f"❌ **ORDER REJECTED** - {formatted_symbol}\n"
            f"   Reason: PRICE FETCH ERROR\n"
            f"   \n"
            f"   Could not fetch current market price for {formatted_symbol}.\n"
            f"   Market may be closed or API unavailable.\n"
            f"{'='*50}"
        )

    # Smart entry logic
    use_market_order = False

    if blofin_side == "buy":
        if current_price <= entry_price:
            use_market_order = True
    else:
        if current_price >= entry_price:
            use_market_order = True

    # Validate TP/SL
    actual_entry = current_price if use_market_order else entry_price

    if blofin_side == "buy":
        if tp1_price and tp1_price <= actual_entry:
            tp1_price = None
        if tp2_price and tp2_price <= actual_entry:
            tp2_price = None
        if tp3_price and tp3_price <= actual_entry:
            tp3_price = None
        if sl_price and sl_price >= actual_entry:
            sl_price = None
    else:
        if tp1_price and tp1_price >= actual_entry:
            tp1_price = None
        if tp2_price and tp2_price >= actual_entry:
            tp2_price = None
        if tp3_price and tp3_price >= actual_entry:
            tp3_price = None
        if sl_price and sl_price <= actual_entry:
            sl_price = None

    order_info = {
        'symbol': formatted_symbol,
        'side': blofin_side,
        'size': final_vol,
        'entry_price': entry_price,
        'tp1': tp1_price,
        'tp2': tp2_price,
        'tp3': tp3_price,
        'sl': sl_price,
        'leverage': leverage
    }

    if use_market_order:
        logger.info(f" Placing MARKET {blofin_side.upper()} {formatted_symbol} x{leverage} | Vol: {final_vol}")

        res = await BlofinAPI.create_market_order(
            symbol=formatted_symbol,
            side=blofin_side,
            vol=final_vol,
            leverage=leverage,
            position_side=pos_side
        )

        if res and res.get('code') == "0":
            await asyncio.sleep(1.5)

            # Create scaled position and set up TPSL
            await _handle_order_filled("market", order_info, final_vol, current_price)

            order_msg = (
                f"🚀 **SCALED EXIT ORDER (Blofin)**\n"
                f"   Symbol: {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   Entry: ~{current_price}\n"
                f"   Size: {final_vol}\n"
                f"   Leverage: x{leverage}\n"
                f"   ---\n"
                f"   Strategy:\n"
                f"   TP1: {tp1_price} (50% close)\n"
                f"   TP2: {tp2_price} (25% close + SL→entry)\n"
                f"   TP3: {tp3_price} (close remaining)\n"
                f"   SL: {sl_price}\n"
            )
            return order_msg
        else:
            error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
            error_data = res.get('data', [])
            if error_data and isinstance(error_data, list) and error_data[0].get('msg'):
                error_msg = error_data[0].get('msg')
            return (
                f"\n{'='*50}\n"
                f"❌ **MARKET ORDER FAILED** - {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   \n"
                f"   API Error: {error_msg}\n"
                f"   \n"
                f"   Order Details:\n"
                f"   • Entry: Market (~{current_price})\n"
                f"   • Size: {final_vol}\n"
                f"   • Leverage: x{leverage}\n"
                f"   • TP1: {tp1_price or 'None'}\n"
                f"   • TP2: {tp2_price or 'None'}\n"
                f"   • TP3: {tp3_price or 'None'}\n"
                f"   • SL: {sl_price or 'None'}\n"
                f"{'='*50}"
            )

    else:
        logger.info(f" Placing LIMIT {blofin_side.upper()} {formatted_symbol} @ {entry_price} x{leverage}")

        res = await BlofinAPI.create_limit_order(
            symbol=formatted_symbol,
            side=blofin_side,
            vol=final_vol,
            price=entry_price,
            leverage=leverage,
            position_side=pos_side
        )

        if res and res.get('code') == "0":
            order_data = res.get('data', {})
            # Handle both dict and list responses
            if isinstance(order_data, list) and order_data:
                order_id = order_data[0].get('orderId', 'N/A')
            elif isinstance(order_data, dict):
                order_id = order_data.get('orderId', 'N/A')
            else:
                order_id = 'N/A'

            # Add to pending for monitoring
            if order_id != 'N/A':
                pending_orders[order_id] = order_info

            order_msg = (
                f"📋 **SCALED EXIT LIMIT ORDER (Blofin)**\n"
                f"   Symbol: {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   Entry: {entry_price}\n"
                f"   Size: {final_vol}\n"
                f"   Leverage: x{leverage}\n"
                f"   Order ID: {order_id}\n"
                f"   ---\n"
                f"   Strategy (on fill):\n"
                f"   TP1: {tp1_price} (50% close)\n"
                f"   TP2: {tp2_price} (25% close + SL→entry)\n"
                f"   TP3: {tp3_price} (close remaining)\n"
                f"   SL: {sl_price}\n"
                f"   ⏳ Waiting for entry..."
            )
            return order_msg
        else:
            error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
            error_data = res.get('data', [])
            if error_data and isinstance(error_data, list) and error_data[0].get('msg'):
                error_msg = error_data[0].get('msg')
            return (
                f"\n{'='*50}\n"
                f"❌ **LIMIT ORDER FAILED** - {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   \n"
                f"   API Error: {error_msg}\n"
                f"   \n"
                f"   Order Details:\n"
                f"   • Entry: {entry_price}\n"
                f"   • Current Price: {current_price}\n"
                f"   • Size: {final_vol}\n"
                f"   • Leverage: x{leverage}\n"
                f"   • TP1: {tp1_price or 'None'}\n"
                f"   • TP2: {tp2_price or 'None'}\n"
                f"   • TP3: {tp3_price or 'None'}\n"
                f"   • SL: {sl_price or 'None'}\n"
                f"{'='*50}"
            )


# --- TELEGRAM HANDLER ---
@client.on(events.NewMessage(chats=TARGET_CHATS, incoming=True))
async def handler(event):
    if event.date < START_TIME:
        return

    text = event.text
    if not text:
        return

    text_upper = text.upper()

    if "PAIR" in text_upper and "SIDE" in text_upper:
        print(f"\n--- New Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

        signal_data = parse_signal(text)
        if not signal_data:
            print(" Failed to parse signal.")
            return

        symbol = signal_data['symbol']

        if signal_data['type'] == 'TRADE':
            print(f"  Processing SCALED EXIT trade for {symbol}...")
            res = await execute_signal_trade(signal_data)
            print(res)

        return


# --- MAIN EXECUTION ---
async def startup():
    """Initialize bot, restore state, and check for positions."""
    # Check API for any positions we might have missed
    await check_api_positions()
    # Start the monitor
    asyncio.create_task(monitor_scaled_positions())
    logger.info("Scaled position monitor started")


if __name__ == "__main__":
    print("=" * 50)
    print("   BLOFIN SCALED EXIT BOT")
    print("=" * 50)
    print("   Strategy:")
    print("   - TP1: Close 50%")
    print("   - TP2: Close 50% of remaining + SL to entry")
    print("   - TP3: Close all remaining")
    print("=" * 50)
    print(f" Start Time (UTC): {START_TIME}")
    print(f" Listening to Chats: {TARGET_CHATS}")
    print("-" * 50)

    # Restore any saved positions from previous session
    restored = restore_positions_from_state()
    if restored > 0:
        print(f" Restored {restored} position(s) from previous session")
        for sym, pos in scaled_positions.items():
            status = []
            if pos.tp1_hit:
                status.append("TP1 ✓")
            if pos.tp2_hit:
                status.append("TP2 ✓")
            status_str = ", ".join(status) if status else "Pending"
            print(f"   {sym}: {status_str}")
    else:
        print(" No previous positions to restore")

    print("-" * 50)

    try:
        client.start()

        # Run startup tasks
        loop = asyncio.get_event_loop()
        loop.run_until_complete(startup())

        print("Waiting for signals... (Ctrl+C to stop)\n")

        client.run_until_disconnected()
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
        save_positions()  # Save on graceful exit
    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        save_positions()  # Save on error too
