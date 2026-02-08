"""
Blofin Scaled Exit Strategy

Strategy:
- TP1: Close 50% of position
- TP2: Close 50% of remaining (25% of original) + Move SL to entry
- TP3: Close all remaining (25% of original)

Position lifecycle:
100% -> TP1 hit -> 50% remaining
50% -> TP2 hit -> 25% remaining, SL moved to entry
25% -> TP3 hit or SL hit -> 0% (closed)
"""

import logging
from dataclasses import dataclass
from typing import Optional, Dict

from bots.blofin.strategies.interface.strategy_interface import BlofinStrategy
from bots.blofin.blofin_scaled.state_manager import save_state, load_state
from common.utils import adjust_price_to_step

logger = logging.getLogger("ScaledStrategy")


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


class ScaledStrategy(BlofinStrategy):
    """
    Scaled Exit Strategy Implementation.

    On entry: Sets TP1 (50%) and SL (100%)
    On TP1 hit: Adjusts SL to 50%, sets TP2 (25% of remaining)
    On TP2 hit: Moves SL to entry, sets TP3 (remaining 25%)
    On TP3/SL hit: Position closed
    """

    name = "BLOFIN SCALED EXIT BOT"

    def __init__(self):
        self.scaled_positions: Dict[str, ScaledPosition] = {}
        self._lot_size_cache: Dict[str, float] = {}

    def validate_signal(self, signal_data: dict) -> Optional[str]:
        """Require at least TP1 for scaled strategy."""
        tps = signal_data.get('tps', [])
        if not tps or not tps[0]:
            formatted_symbol = signal_data['symbol'].replace('_', '-')
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: NO TP1 - Required for scaled strategy\n"
                f"{'='*50}"
            )
        return None

    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        """Return all TP levels for scaled exit."""
        tps = signal_data.get('tps', [])
        sl_price = signal_data.get('sl')

        tp1 = adjust_price_to_step(tps[0], tick_size) if len(tps) >= 1 and tps[0] else None
        tp2 = adjust_price_to_step(tps[1], tick_size) if len(tps) >= 2 and tps[1] else None
        tp3 = adjust_price_to_step(tps[2], tick_size) if len(tps) >= 3 and tps[2] else None
        sl = adjust_price_to_step(sl_price, tick_size) if sl_price else None

        return {
            'tp1': tp1,
            'tp2': tp2,
            'tp3': tp3,
            'sl': sl,
            'mode': 'scaled'
        }

    async def on_order_fill(self, order_id, order_info, filled_size, fill_price, engine):
        """Create scaled position and set up initial TPSL orders."""
        symbol = order_info['symbol']
        side = order_info['side']

        print(f"\n{'='*40}")
        print(f"  **ORDER FILLED** - {symbol}")
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
        tpsl_result = await self._setup_scaled_tpsl(pos, engine)
        print(f"   TPSL: {tpsl_result}")
        print(f"{'='*40}")

        # Add to tracking
        self.scaled_positions[symbol] = pos
        logger.info(f"Added {symbol} to scaled positions monitoring")
        self._save_positions()

    async def on_tick(self, engine):
        """Monitor scaled positions for TP/SL hits."""
        if not self.scaled_positions:
            return

        positions_to_remove = []

        for symbol, pos in list(self.scaled_positions.items()):
            if pos.is_closed:
                positions_to_remove.append(symbol)
                continue

            # Check TPSL order history for triggered orders
            tpsl_history = await engine.api._make_request(
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
                        await self._handle_tp1_hit(pos, engine)
                    elif tpsl_id == pos.tp2_order_id and not pos.tp2_hit:
                        await self._handle_tp2_hit(pos, engine)
                    elif tpsl_id == pos.tp3_order_id and not pos.tp3_hit:
                        await self._handle_tp3_hit(pos)
                        positions_to_remove.append(symbol)
                    elif tpsl_id == pos.sl_order_id and not pos.sl_hit:
                        await self._handle_sl_hit(pos)
                        positions_to_remove.append(symbol)

            # Check if position still exists
            positions = await engine.api.get_open_positions(symbol)
            if positions and len(positions) > 0:
                # Update live PnL data
                live_pos = positions[0]
                pos.unrealized_pnl = live_pos.unrealized
                pos.mark_price = live_pos.markPrice
                continue

            # Fallback: Check TPSL orders
            tpsl_orders = await engine.api.get_tpsl_orders(symbol)
            if tpsl_orders and len(tpsl_orders) > 0:
                continue  # Position still open

            # Position appears closed
            check_count = getattr(pos, '_close_check', 0) + 1
            pos._close_check = check_count

            if check_count >= 2:
                # Position closed - determine final reason
                from blofincpy.blofinTypes import CloseReason
                reason = await engine.api.get_position_close_reason(symbol)

                if reason == CloseReason.SL:
                    await self._handle_sl_hit(pos)
                elif reason == CloseReason.TP:
                    await self._handle_tp3_hit(pos)
                else:
                    print(f"\n Position closed for {symbol} (manual or unknown)")

                positions_to_remove.append(symbol)

        for sym in positions_to_remove:
            if sym in self.scaled_positions:
                del self.scaled_positions[sym]

    async def on_breakeven_signal(self, symbol: str, engine) -> Optional[str]:
        """Scaled strategy doesn't use external breakeven signals."""
        return None

    @property
    def supports_updates(self) -> bool:
        """Scaled strategy doesn't support UPDATE signals."""
        return False

    def get_state(self) -> dict:
        """Return serializable state for persistence."""
        return load_state()

    def load_state(self, data: dict):
        """Restore scaled positions from saved state."""
        if not data:
            return

        for symbol, pos_data in data.items():
            try:
                pos = ScaledPosition(
                    symbol=pos_data['symbol'],
                    side=pos_data['side'],
                    original_size=pos_data['original_size'],
                    remaining_size=pos_data['remaining_size'],
                    entry_price=pos_data['entry_price'],
                    tp1_price=pos_data['tp1_price'],
                    tp2_price=pos_data['tp2_price'],
                    tp3_price=pos_data['tp3_price'],
                    sl_price=pos_data['sl_price'],
                    leverage=pos_data['leverage'],
                    tp1_hit=pos_data.get('tp1_hit', False),
                    tp2_hit=pos_data.get('tp2_hit', False),
                    tp3_hit=pos_data.get('tp3_hit', False),
                    sl_hit=pos_data.get('sl_hit', False),
                    tp1_order_id=pos_data.get('tp1_order_id'),
                    tp2_order_id=pos_data.get('tp2_order_id'),
                    tp3_order_id=pos_data.get('tp3_order_id'),
                    sl_order_id=pos_data.get('sl_order_id'),
                )

                if not pos.is_closed:
                    self.scaled_positions[symbol] = pos
                    logger.info(f"Restored position: {symbol}")
            except Exception as e:
                logger.error(f"Failed to restore {symbol}: {e}")

        if self.scaled_positions:
            print(f" Restored {len(self.scaled_positions)} scaled position(s)")
            for sym, pos in self.scaled_positions.items():
                status = []
                if pos.tp1_hit:
                    status.append("TP1 hit")
                if pos.tp2_hit:
                    status.append("TP2 hit")
                status_str = ", ".join(status) if status else "Pending"
                print(f"   {sym}: {status_str}")

    # ===================================================================
    # INTERNAL HELPERS
    # ===================================================================

    def _save_positions(self):
        """Persist positions to disk."""
        save_state(self.scaled_positions)

    async def _get_lot_size(self, symbol: str, engine) -> float:
        """Get the lot size for a symbol from API (cached)."""
        if symbol in self._lot_size_cache:
            return self._lot_size_cache[symbol]

        try:
            info = await engine.api.get_instrument_info(symbol)
            if info:
                lot_size = float(info.get('lotSize', 1))
                self._lot_size_cache[symbol] = lot_size
                logger.info(f"Lot size for {symbol}: {lot_size}")
                return lot_size
        except Exception as e:
            logger.warning(f"Failed to get lot size for {symbol}: {e}")

        self._lot_size_cache[symbol] = 1.0
        return 1.0

    def _round_size_to_lot(self, size: float, lot_size: float) -> float:
        """Round size to valid lot size."""
        if not lot_size or lot_size == 0:
            return size
        return round(round(size / lot_size) * lot_size, 8)

    async def _setup_scaled_tpsl(self, pos: ScaledPosition, engine) -> str:
        """
        Set up the initial TPSL orders for scaled exit strategy.
        - TP1: 50% of position
        - SL: 100% of position
        """
        results = []

        lot_size = await self._get_lot_size(pos.symbol, engine)
        tp1_size = self._round_size_to_lot(pos.original_size * 0.50, lot_size)

        # Validate: TP1 size must be at least 1 lot
        if tp1_size < lot_size:
            logger.warning(f"Position too small to split ({pos.original_size}). Using 100% at each TP.")
            tp1_size = pos.original_size

        # Set TP1 (50%)
        if pos.tp1_price:
            tp1_res = await engine.set_tpsl_order(
                pos.symbol, pos.position_side, pos.close_side,
                tp1_size, tp_price=pos.tp1_price
            )
            if tp1_res and tp1_res.get('code') == "0":
                data = tp1_res.get('data', {})
                if isinstance(data, list) and data:
                    pos.tp1_order_id = data[0].get('tpslId')
                elif isinstance(data, dict):
                    pos.tp1_order_id = data.get('tpslId')
                results.append(f"TP1: {pos.tp1_price} (50%)")
            else:
                results.append(f"TP1 FAILED: {tp1_res.get('msg', 'Unknown') if tp1_res else 'No response'}")

        # Set SL (100% initially)
        if pos.sl_price:
            sl_res = await engine.set_tpsl_order(
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

    async def _handle_tp1_hit(self, pos: ScaledPosition, engine):
        """
        Called when TP1 is hit (50% closed).
        - Update remaining size to 50%
        - Cancel old SL, create new SL with 50% size
        - Create TP2 order for 50% of remaining (25% of original)
        """
        pos.tp1_hit = True
        lot_size = await self._get_lot_size(pos.symbol, engine)
        pos.remaining_size = self._round_size_to_lot(pos.original_size * 0.50, lot_size)

        print(f"\n{'='*40}")
        print(f" **TP1 HIT** - {pos.symbol}")
        print(f"   Closed: 50% @ {pos.tp1_price}")
        print(f"   Remaining: {pos.remaining_size}")

        # Cancel old SL and create new one with 50% size
        if pos.sl_order_id:
            await engine.cancel_tpsl_order(pos.symbol, pos.sl_order_id)

        sl_res = await engine.set_tpsl_order(
            pos.symbol, pos.position_side, pos.close_side,
            pos.remaining_size, sl_price=pos.sl_price
        )
        if sl_res and sl_res.get('code') == "0":
            data = sl_res.get('data', {})
            if isinstance(data, list) and data:
                pos.sl_order_id = data[0].get('tpslId')
            elif isinstance(data, dict):
                pos.sl_order_id = data.get('tpslId')
            print(f"   SL updated to 50% size")

        # Create TP2 order (50% of remaining = 25% of original)
        tp2_size = self._round_size_to_lot(pos.remaining_size * 0.50, lot_size)
        if pos.tp2_price:
            tp2_res = await engine.set_tpsl_order(
                pos.symbol, pos.position_side, pos.close_side,
                tp2_size, tp_price=pos.tp2_price
            )
            if tp2_res and tp2_res.get('code') == "0":
                data = tp2_res.get('data', {})
                if isinstance(data, list) and data:
                    pos.tp2_order_id = data[0].get('tpslId')
                elif isinstance(data, dict):
                    pos.tp2_order_id = data.get('tpslId')
                print(f"   TP2 set: {pos.tp2_price} (25% of original)")
            else:
                print(f"   TP2 failed: {tp2_res.get('msg', 'Unknown') if tp2_res else 'No response'}")

        print(f"{'='*40}")
        self._save_positions()

    async def _handle_tp2_hit(self, pos: ScaledPosition, engine):
        """
        Called when TP2 is hit (25% more closed, 25% remaining).
        - Update remaining size to 25%
        - Move SL to entry price
        - Create TP3 order for remaining 25%
        """
        pos.tp2_hit = True
        lot_size = await self._get_lot_size(pos.symbol, engine)
        pos.remaining_size = self._round_size_to_lot(pos.original_size * 0.25, lot_size)

        print(f"\n{'='*40}")
        print(f" **TP2 HIT** - {pos.symbol}")
        print(f"   Closed: 25% @ {pos.tp2_price}")
        print(f"   Remaining: {pos.remaining_size}")

        # Cancel old SL and move to ENTRY PRICE (breakeven)
        if pos.sl_order_id:
            await engine.cancel_tpsl_order(pos.symbol, pos.sl_order_id)

        sl_res = await engine.set_tpsl_order(
            pos.symbol, pos.position_side, pos.close_side,
            pos.remaining_size, sl_price=pos.entry_price  # SL at entry!
        )
        if sl_res and sl_res.get('code') == "0":
            data = sl_res.get('data', {})
            if isinstance(data, list) and data:
                pos.sl_order_id = data[0].get('tpslId')
            elif isinstance(data, dict):
                pos.sl_order_id = data.get('tpslId')
            print(f"   SL moved to ENTRY: {pos.entry_price} (breakeven)")

        # Create TP3 order for remaining 25%
        if pos.tp3_price:
            tp3_res = await engine.set_tpsl_order(
                pos.symbol, pos.position_side, pos.close_side,
                pos.remaining_size, tp_price=pos.tp3_price
            )
            if tp3_res and tp3_res.get('code') == "0":
                data = tp3_res.get('data', {})
                if isinstance(data, list) and data:
                    pos.tp3_order_id = data[0].get('tpslId')
                elif isinstance(data, dict):
                    pos.tp3_order_id = data.get('tpslId')
                print(f"   TP3 set: {pos.tp3_price} (remaining 25%)")
            else:
                print(f"   TP3 failed: {tp3_res.get('msg', 'Unknown') if tp3_res else 'No response'}")

        print(f"{'='*40}")
        self._save_positions()

    async def _handle_tp3_hit(self, pos: ScaledPosition):
        """Called when TP3 is hit - position fully closed."""
        pos.tp3_hit = True
        pos.remaining_size = 0

        print(f"\n{'='*40}")
        print(f" **TP3 HIT - POSITION CLOSED** - {pos.symbol}")
        print(f"   Final close @ {pos.tp3_price}")
        print(f"   Entry: {pos.entry_price}")
        print(f"   Strategy completed successfully!")
        print(f"{'='*40}")
        self._save_positions()

    async def _handle_sl_hit(self, pos: ScaledPosition):
        """Called when SL is hit - position closed at stop loss."""
        pos.sl_hit = True

        sl_level = "entry (breakeven)" if pos.tp2_hit else f"{pos.sl_price}"

        print(f"\n{'='*40}")
        print(f" **STOP LOSS HIT** - {pos.symbol}")
        print(f"   SL triggered @ {sl_level}")
        print(f"   Entry was: {pos.entry_price}")
        if pos.tp1_hit:
            print(f"   TP1 was hit (50% profit taken)")
        if pos.tp2_hit:
            print(f"   TP2 was hit (75% profit taken, SL at breakeven)")
        print(f"{'='*40}")

        pos.remaining_size = 0
        self._save_positions()
