"""
Blofin Breakeven Strategy (TP1)
- Places order with TP at TP1, SL at signal SL
- Supports BREAKEVEN signal (move SL to entry)
- Supports UPDATE signals (change TP/SL)
"""

import logging
from typing import Optional
from bots.blofin.strategies.interface.strategy_interface import BlofinStrategy
from common.utils import adjust_price_to_step

logger = logging.getLogger("BlofinBreakevenStrategy")


class BreakevenStrategy(BlofinStrategy):
    name = "BLOFIN BREAKEVEN BOT (TP1 Strategy)"

    def validate_signal(self, signal_data: dict) -> Optional[str]:
        return None

    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        tps = signal_data.get('tps', [])
        tp1_price = tps[0] if tps else None
        sl_price = signal_data.get('sl')

        if tp1_price:
            tp1_price = adjust_price_to_step(tp1_price, tick_size)
        if sl_price:
            sl_price = adjust_price_to_step(sl_price, tick_size)

        return {'tp': tp1_price, 'sl': sl_price}

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
                    parts.append(f"TP: {tp_price}")
                if sl_price:
                    parts.append(f"SL: {sl_price}")
                logger.info(f"   Set: {', '.join(parts)}")
            else:
                error = res.get('msg', 'Failed') if res else 'No response'
                logger.info(f"   TPSL Failed: {error}")

        logger.info(f"{'='*40}")

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
        pass

    async def on_breakeven_signal(self, symbol: str, engine) -> Optional[str]:
        """Move SL to entry price for breakeven."""
        engine.logger.info(f"Attempting BREAKEVEN for {symbol}...")

        formatted_symbol = symbol.replace('_', '-')

        # 1. Try to get position info
        entry_price = None
        position_side = None
        hold_vol = None

        positions = await engine.api.get_open_positions(formatted_symbol)
        if positions and len(positions) > 0:
            position = positions[0]
            entry_price = position.openAvgPrice
            pos_side = position.positionType
            hold_vol = abs(position.holdVol)

            if pos_side == "net":
                position_side = "long" if position.holdVol > 0 else "short"
            else:
                position_side = pos_side

        # Fallback: TPSL orders
        if not entry_price:
            tpsl_orders = await engine.api.get_tpsl_orders(formatted_symbol)
            if tpsl_orders:
                tpsl = tpsl_orders[0]
                hold_vol = float(tpsl.get('size', 0))
                position_side = tpsl.get('posSide', 'long')

                fills = await engine.api.get_fills(symbol=formatted_symbol)
                if fills:
                    entry_price = float(fills[0].get('fillPrice', 0))

        # Fallback: order history
        if not entry_price:
            history = await engine.api.get_order_history(symbol=formatted_symbol)
            if history:
                for h in history:
                    if h.get('state') == 'filled':
                        entry_price = float(h.get('averagePrice', 0))
                        hold_vol = float(h.get('filledSize', 0))
                        side = h.get('side', 'buy')
                        position_side = "long" if side == "buy" else "short"
                        break

        if not entry_price or not hold_vol:
            return f"Cannot Move SL: No position info found for {formatted_symbol}"

        final_sl_price = entry_price
        exit_side = "sell" if position_side == "long" else "buy"

        logger.info(f"   > Found Position: {position_side.upper()} @ {entry_price}. Moving SL...")

        req_body = {
            "instId": formatted_symbol,
            "marginMode": "isolated",
            "posSide": position_side,
            "side": exit_side,
            "size": str(hold_vol),
            "reduceOnly": "true",
            "slTriggerPrice": str(final_sl_price),
            "slOrderPrice": "-1"
        }

        engine.logger.info(f" Breakeven TPSL request: {req_body}")
        res = await engine.api._make_request("POST", "/api/v1/trade/order-tpsl", body=req_body)
        engine.logger.info(f" Breakeven TPSL response: {res}")

        if res and res.get('code') == "0":
            return (f"  **SL Updated to Entry!**\n"
                    f"   Symbol: {formatted_symbol}\n"
                    f"   New SL: {final_sl_price}\n"
                    f"   (Breakeven Successful)")
        else:
            error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
            return f"  **Failed to Move SL**\n   Error: {error_msg}"

    @property
    def supports_updates(self) -> bool:
        return True
