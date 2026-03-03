"""
Binance TP1 Strategy
- Places TP at TP1 and SL as separate TAKE_PROFIT_MARKET / STOP_MARKET orders
- No breakeven or update support
"""
import logging
from typing import Optional
from bots.binance.strategies.interface.strategy_interface import BinanceStrategy
from common.utils import adjust_price_to_step

logger = logging.getLogger("BinanceTP1Strategy")


class BinanceTP1Strategy(BinanceStrategy):
    name = "BINANCE TP1 BOT"

    def validate_signal(self, signal_data: dict) -> Optional[str]:
        return None

    def get_tp_config(self, signal_data: dict, tick_size: float) -> dict:
        tps = signal_data['tps']
        if not tps:
            raise ValueError("Signal has no TP levels")
        tp1_price = adjust_price_to_step(tps[0], tick_size)

        sl_price = signal_data['sl']
        if sl_price:
            sl_price = adjust_price_to_step(sl_price, tick_size)
        return {'tp': tp1_price, 'sl': sl_price}

    async def on_order_fill(self, order_id, order_info, filled_size, fill_price, engine):
        symbol = order_info['symbol']
        side = order_info['side']
        tp_price = order_info.get('tp')
        sl_price = order_info.get('sl')

        # Binance: TP and SL are separate orders — API raises on failure
        close_side = "SELL" if side == "BUY" else "BUY"

        tp_order_id = None
        sl_order_id = None

        if tp_price:
            res = await engine.set_tp_order(symbol, close_side, filled_size, tp_price)
            tp_order_id = res['algoId']
            logger.info(f"   TP set @ {tp_price} (algoId {tp_order_id})")

        if sl_price:
            res = await engine.set_sl_order(symbol, close_side, filled_size, sl_price)
            sl_order_id = res['algoId']
            logger.info(f"   SL set @ {sl_price} (algoId {sl_order_id})")

        logger.info(f"{'='*40}")

        engine.active_positions[symbol] = {
            'side': side, 'size': filled_size, 'entry_price': fill_price,
            'tp': tp_price, 'sl': sl_price, 'leverage': order_info['leverage'],
            'tp_order_id': tp_order_id, 'sl_order_id': sl_order_id,
        }
        engine.logger.info(f"Added {symbol} to active positions monitoring")

    async def on_tick(self, engine):
        pass

    @property
    def supports_updates(self) -> bool:
        return False
