"""
Blofin TP3 Strategy (100% Close at TP3)
- Places order with TP at TP3 (or best available TP), SL at signal SL
- Supports UPDATE signals (change TP/SL)
- Does NOT support BREAKEVEN signals
"""

from typing import Optional
from bots.blofin.strategies.interface.strategy_interface import BlofinStrategy
from common.utils import adjust_price_to_step


class TP3Strategy(BlofinStrategy):
    name = "BLOFIN TP3 BOT (100% Close at TP3)"

    def validate_signal(self, signal_data: dict) -> Optional[str]:
        """No extra validation needed for TP3 strategy."""
        return None

    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        """
        Pick TP3 as the take profit target.
        Fallback: TP2 -> TP1 if TP3 not available.
        """
        tps = signal_data.get('tps', [])
        sl_price = signal_data.get('sl')

        # Try TP3 first, then TP2, then TP1
        tp_price = None
        if len(tps) >= 3 and tps[2]:
            tp_price = tps[2]
        elif len(tps) >= 2 and tps[1]:
            tp_price = tps[1]
        elif len(tps) >= 1 and tps[0]:
            tp_price = tps[0]

        if tp_price:
            tp_price = adjust_price_to_step(tp_price, tick_size)
        if sl_price:
            sl_price = adjust_price_to_step(sl_price, tick_size)

        return {'tp': tp_price, 'sl': sl_price}

    async def on_order_fill(self, order_id, order_info, filled_size, fill_price, engine):
        """Set combined TP/SL order and add to active positions."""
        symbol = order_info['symbol']
        side = order_info['side']
        tp_price = order_info.get('tp')
        sl_price = order_info.get('sl')

        if tp_price or sl_price:
            close_side = "sell" if side == "buy" else "buy"
            position_side = "long" if side == "buy" else "short"

            res = await engine.set_tpsl_order(
                symbol, position_side, close_side, filled_size,
                tp_price=tp_price, sl_price=sl_price
            )

            if res and res.get('code') == "0":
                parts = []
                if tp_price:
                    parts.append(f"TP3: {tp_price}")
                if sl_price:
                    parts.append(f"SL: {sl_price}")
                print(f"   Set: {', '.join(parts)}")
            else:
                error = res.get('msg', 'Failed') if res else 'No response'
                print(f"   TPSL Failed: {error}")

        print(f"{'='*40}")

        # Add to active positions
        engine.active_positions[symbol] = {
            'side': side,
            'size': filled_size,
            'entry_price': fill_price,
            'tp': tp_price,
            'sl': sl_price,
            'leverage': order_info.get('leverage')
        }
        engine.logger.info(f"Added {symbol} to active positions monitoring")

    async def on_tick(self, engine):
        """No custom tick logic needed for TP3 strategy."""
        pass

    async def on_breakeven_signal(self, symbol: str, engine) -> Optional[str]:
        """TP3 strategy does not support breakeven signals."""
        return None

    @property
    def supports_updates(self) -> bool:
        """TP3 strategy supports UPDATE signals."""
        return True
