"""
Blofin TP1-Trail Strategy (Ride to TP3)

Strategy:
- On entry: Place TPSL with TP=TP3, SL=original SL
- Monitor mark_price against TP1 level
- When TP1 is crossed: Cancel old TPSL, place new TPSL with SL=TP2, TP=TP3
- Position closes at TP3 (full win) or TP2 (trailing SL hit)

Requires at least 3 TPs in signal.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Dict

from bots.blofin.strategies.interface.strategy_interface import BlofinStrategy
from common.utils import adjust_price_to_step

logger = logging.getLogger("Tp1TrailStrategy")

STATE_FILE = Path(__file__).parent.parent.parent / "blofin_tp1_trail" / "tp1_trail_state.json"


class Tp1TrailStrategy(BlofinStrategy):
    """
    TP1-Trail Strategy: Enter with TP3 target, trail SL to TP2 once TP1 is crossed.

    On entry fill: TPSL with TP=TP3, SL=original SL
    On TP1 crossed (mark_price): Cancel old TPSL, new TPSL with SL=TP2, TP=TP3
    Exit: TP3 hit (full win) or TP2 hit (smaller win after trail)
    """

    name = "BLOFIN TP1-TRAIL"

    def __init__(self):
        self.tracked_positions: Dict[str, dict] = {}
        # Temporary cache: store all TP levels per symbol between get_tp_config and on_order_fill
        self._pending_tp_levels: Dict[str, dict] = {}

    def validate_signal(self, signal_data: dict) -> Optional[str]:
        """Require at least 3 TPs and an SL."""
        tps = signal_data.get('tps', [])
        sl = signal_data.get('sl')

        if len(tps) < 3 or not all(tps[:3]):
            formatted_symbol = signal_data['symbol'].replace('_', '-')
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: Requires at least 3 TPs for TP1-Trail strategy\n"
                f"{'='*50}"
            )

        if not sl:
            formatted_symbol = signal_data['symbol'].replace('_', '-')
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: Requires SL for TP1-Trail strategy\n"
                f"{'='*50}"
            )

        return None

    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        """Return TP3 as target, original SL as stop. Cache all TP levels for on_order_fill."""
        tps = signal_data.get('tps', [])
        sl_price = signal_data.get('sl')
        symbol = signal_data['symbol'].replace('_', '-')

        tp1 = adjust_price_to_step(tps[0], tick_size) if len(tps) >= 1 and tps[0] else None
        tp2 = adjust_price_to_step(tps[1], tick_size) if len(tps) >= 2 and tps[1] else None
        tp3 = adjust_price_to_step(tps[2], tick_size) if len(tps) >= 3 and tps[2] else None
        sl = adjust_price_to_step(sl_price, tick_size) if sl_price else None

        # Cache TP levels so on_order_fill can access them
        self._pending_tp_levels[symbol] = {
            'tp1': tp1, 'tp2': tp2, 'tp3': tp3
        }

        return {'tp': tp3, 'sl': sl}

    async def on_order_fill(self, order_id, order_info, filled_size, fill_price, engine):
        """Set initial TPSL (TP=TP3, SL=original) and start tracking for TP1 cross."""
        symbol = order_info['symbol']
        side = order_info['side']
        tp_price = order_info.get('tp')
        sl_price = order_info.get('sl')

        # Retrieve cached TP levels from get_tp_config
        cached = self._pending_tp_levels.pop(symbol, {})
        tp1 = cached.get('tp1')
        tp2 = cached.get('tp2')
        tp3 = cached.get('tp3')

        tpsl_id = None

        if order_info.get('tpsl_attached'):
            # TP/SL was already attached to the market order
            parts = []
            if tp_price:
                parts.append(f"TP3: {tp_price}")
            if sl_price:
                parts.append(f"SL: {sl_price}")
            logger.info(f"   TP/SL attached to order: {', '.join(parts)}")
            # We don't have a tpsl_id — trail logic will need to query TPSL orders
        elif tp_price or sl_price:
            close_side = "sell" if side == "buy" else "buy"
            position_side = "long" if side == "buy" else "short"

            res = await engine.set_tpsl_order(
                symbol, position_side, close_side, filled_size,
                tp_price=tp_price, sl_price=sl_price
            )

            if res and res.get('code') == "0":
                # Extract tpsl_id from response
                data = res.get('data', {})
                if isinstance(data, list) and data:
                    tpsl_id = data[0].get('tpslId')
                elif isinstance(data, dict):
                    tpsl_id = data.get('tpslId')

                parts = []
                if tp_price:
                    parts.append(f"TP3: {tp_price}")
                if sl_price:
                    parts.append(f"SL: {sl_price}")
                logger.info(f"   Set initial TPSL: {', '.join(parts)}")
            else:
                error = res.get('msg', 'Failed') if res else 'No response'
                logger.info(f"   TPSL Failed: {error}")

        logger.info(f"{'='*40}")

        # Add to active positions (engine tracking)
        engine.active_positions[symbol] = {
            'side': side,
            'size': filled_size,
            'entry_price': fill_price,
            'tp': tp_price,
            'sl': sl_price,
            'leverage': order_info.get('leverage')
        }
        engine.logger.info(f"Added {symbol} to active positions monitoring")

        # Track for TP1 trail logic
        self.tracked_positions[symbol] = {
            'tp1': tp1,
            'tp2': tp2,
            'tp3': tp3,
            'sl': sl_price,
            'side': side,
            'entry_price': fill_price,
            'size': filled_size,
            'tp1_hit': False,
            'tpsl_id': tpsl_id,
        }
        logger.info(f"Tracking {symbol} for TP1 trail (TP1={tp1}, TP2={tp2}, TP3={tp3})")
        self._save_state()

    async def on_tick(self, engine):
        """Check mark_price against TP1 for each tracked position."""
        if not self.tracked_positions:
            return

        symbols_to_remove = []

        for symbol, info in list(self.tracked_positions.items()):
            if info['tp1_hit']:
                # Already trailed, just check if position still exists
                positions = await engine.api.get_open_positions(symbol)
                if not positions or len(positions) == 0:
                    # Double-check with TPSL orders
                    tpsl_orders = await engine.api.get_tpsl_orders(symbol)
                    if not tpsl_orders or len(tpsl_orders) == 0:
                        logger.info(f"Position closed for {symbol} (after TP1 trail)")
                        symbols_to_remove.append(symbol)
                continue

            # Check if position still exists
            positions = await engine.api.get_open_positions(symbol)
            if not positions or len(positions) == 0:
                # Position gone before TP1 was hit (SL or manual close)
                tpsl_orders = await engine.api.get_tpsl_orders(symbol)
                if not tpsl_orders or len(tpsl_orders) == 0:
                    logger.info(f"Position closed for {symbol} (before TP1)")
                    symbols_to_remove.append(symbol)
                continue

            # Get mark price from position
            position = positions[0]
            mark_price = position.markPrice

            tp1 = info['tp1']
            side = info['side']

            if not tp1 or not mark_price:
                continue

            # Check if TP1 is crossed
            tp1_crossed = False
            if side == "buy" and mark_price >= tp1:
                tp1_crossed = True
            elif side == "sell" and mark_price <= tp1:
                tp1_crossed = True

            if tp1_crossed:
                logger.info(f"\n{'='*40}")
                logger.info(f" **TP1 CROSSED** - {symbol}")
                logger.info(f"   Mark Price: {mark_price}")
                logger.info(f"   TP1 Level: {tp1}")
                logger.info(f"   Trailing SL to TP2: {info['tp2']}")

                position_side = "long" if side == "buy" else "short"
                close_side = "sell" if side == "buy" else "buy"

                # Cancel old TPSL order(s)
                old_tpsl_id = info.get('tpsl_id')
                if old_tpsl_id:
                    cancelled = await engine.cancel_tpsl_order(symbol, old_tpsl_id)
                    if cancelled:
                        logger.info(f"   Cancelled old TPSL: {old_tpsl_id}")
                    else:
                        logger.warning(f"   Failed to cancel old TPSL: {old_tpsl_id}")
                else:
                    # TP/SL was attached to order — cancel all active TPSL orders for this symbol
                    tpsl_orders = await engine.api.get_tpsl_orders(symbol)
                    if tpsl_orders:
                        for tpsl in tpsl_orders:
                            tid = tpsl.get('tpslId')
                            if tid:
                                await engine.cancel_tpsl_order(symbol, tid)
                                logger.info(f"   Cancelled attached TPSL: {tid}")

                # Place new TPSL with SL=TP2, TP=TP3
                new_tpsl_id = None
                res = await engine.set_tpsl_order(
                    symbol, position_side, close_side, info['size'],
                    tp_price=info['tp3'], sl_price=info['tp2']
                )

                if res and res.get('code') == "0":
                    data = res.get('data', {})
                    if isinstance(data, list) and data:
                        new_tpsl_id = data[0].get('tpslId')
                    elif isinstance(data, dict):
                        new_tpsl_id = data.get('tpslId')
                    logger.info(f"   New TPSL set: TP={info['tp3']}, SL={info['tp2']}")
                else:
                    error = res.get('msg', 'Failed') if res else 'No response'
                    logger.warning(f"   New TPSL failed: {error}")

                info['tp1_hit'] = True
                info['tpsl_id'] = new_tpsl_id
                logger.info(f"{'='*40}")
                self._save_state()

        for sym in symbols_to_remove:
            if sym in self.tracked_positions:
                del self.tracked_positions[sym]
        if symbols_to_remove:
            self._save_state()

    async def on_position_closed(self, symbol: str, pos_info: dict, close_reason, engine):
        """Remove symbol from tracking when position closes."""
        if symbol in self.tracked_positions:
            was_trailed = self.tracked_positions[symbol].get('tp1_hit', False)
            del self.tracked_positions[symbol]
            self._save_state()

            status = "after TP1 trail" if was_trailed else "before TP1"
            logger.info(f"Removed {symbol} from TP1-trail tracking ({status})")

    async def on_breakeven_signal(self, symbol: str, engine) -> Optional[str]:
        """TP1-Trail strategy does not support breakeven signals."""
        return None

    @property
    def supports_updates(self) -> bool:
        return False

    def get_state(self) -> dict:
        """Return serializable state for persistence."""
        return self.tracked_positions.copy()

    def load_state(self, data: dict):
        """Restore tracked positions from saved state."""
        if not data:
            return

        for symbol, pos_data in data.items():
            self.tracked_positions[symbol] = pos_data
            status = "TP1 trailed" if pos_data.get('tp1_hit') else "Watching for TP1"
            logger.info(f"Restored: {symbol} ({status})")

        if self.tracked_positions:
            logger.info(f"Restored {len(self.tracked_positions)} TP1-trail position(s)")

    def _save_state(self):
        """Persist tracked positions to disk."""
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE, 'w') as f:
                json.dump(self.tracked_positions, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_state_from_file(self) -> dict:
        """Load state from file."""
        if not STATE_FILE.exists():
            return {}
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return {}
