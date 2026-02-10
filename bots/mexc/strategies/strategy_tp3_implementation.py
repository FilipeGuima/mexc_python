"""
MEXC TP3 Strategy Implementation

- Uses smart entry logic (market if price favorable, limit otherwise)
- Supports UPDATE signals (change TP/SL)
- Uses TP3 (third take profit target) instead of TP1
- Uses common.parser for signal parsing
"""

import asyncio
import logging
from typing import Optional

from mexcpy.mexcTypes import OrderSide, CreateOrderRequest, OpenType, OrderType
from bots.mexc.strategies.strategy_interface import MexcStrategy
from common.parser import parse_signal, UpdateParser
from common.utils import adjust_price_to_step, validate_signal_tp_sl

logger = logging.getLogger("MexcTP3Strategy")


class MexcTP3Strategy(MexcStrategy):
    name = "MEXC TP3 BOT "

    def parse_signal(self, text: str) -> Optional[dict]:
        """Parse signal using common parser."""
        return parse_signal(text)

    async def handle_signal(self, text: str, engine) -> Optional[str]:
        """Route signal to appropriate handler."""
        text_upper = text.upper()

        # Trade signals
        if "PAIR" in text_upper and "SIDE" in text_upper:
            logger.info("\n  New Signal Detected!")
            signal_data = self.parse_signal(text)

            if not signal_data:
                return " Failed to parse signal."

            # Convert string side to MEXC OrderSide enum
            if signal_data.get('side') == "LONG":
                signal_data['side'] = OrderSide.OpenLong
            elif signal_data.get('side') == "SHORT":
                signal_data['side'] = OrderSide.OpenShort

            # Validate TP/SL
            validation_error = validate_signal_tp_sl(signal_data)
            if validation_error:
                return validation_error

            # Validate that TP3 exists
            if len(signal_data.get('tps', [])) < 3:
                symbol = signal_data.get('symbol', 'UNKNOWN')
                tp_count = len(signal_data.get('tps', []))
                return (
                    f"\n{'='*50}\n"
                    f" **ORDER REJECTED** - {symbol}\n"
                    f"   Reason: Signal missing TP3\n"
                    f"   \n"
                    f"   This bot requires TP3 but signal only has {tp_count} TP level(s).\n"
                    f"   TPs found: {signal_data.get('tps', [])}\n"
                    f"{'='*50}"
                )

            return await self.execute_trade(signal_data, engine)

        # Update signals
        if any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET"]) and "/" in text:
            logger.info("\n  Update Signal Detected!")
            update_data = UpdateParser.parse(text)

            if update_data:
                return await self._execute_update_signal(update_data, engine)
            else:
                return " Update detected but failed to parse details."

        return None

    async def execute_trade(self, data: dict, engine) -> str:
        """
        Execute trade with smart entry logic, using TP3 as the take profit target.
        """
        symbol = data['symbol']
        leverage = data['leverage']
        side = data['side']
        equity_perc = data['equity_perc']
        entry_value = data['entry']

        sl_price_raw = data.get('sl')
        tp3_price_raw = data['tps'][2]

        quote_currency = symbol.split('_')[1]

        # === STEP 1: Get balance ===
        assets_response = await engine.api.get_user_assets()
        if not assets_response.success:
            return (
                f"\n{'='*50}\n"
                f" **ORDER REJECTED** - {symbol}\n"
                f"   Reason: API ERROR\n"
                f"   \n"
                f"   Could not fetch account assets.\n"
                f"   Error: {assets_response.message}\n"
                f"{'='*50}"
            )

        target_asset = next((a for a in assets_response.data if a.currency == quote_currency), None)
        if not target_asset:
            return (
                f"\n{'='*50}\n"
                f" **ORDER REJECTED** - {symbol}\n"
                f"   Reason: WALLET ERROR\n"
                f"   \n"
                f"   {quote_currency} balance not found in account.\n"
                f"   Please ensure you have {quote_currency} available for trading.\n"
                f"{'='*50}"
            )

        balance = target_asset.availableBalance

        # === STEP 2: Get current price ===
        ticker_res = await engine.api.get_ticker(symbol)
        if not ticker_res.success or not ticker_res.data:
            return (
                f"\n{'='*50}\n"
                f" **ORDER REJECTED** - {symbol}\n"
                f"   Reason: PRICE FETCH ERROR\n"
                f"   \n"
                f"   Could not fetch ticker data for {symbol}.\n"
                f"   Error: {ticker_res.message or 'No data returned'}\n"
                f"   \n"
                f"   This symbol may not exist or market may be closed.\n"
                f"{'='*50}"
            )
        current_price = ticker_res.data.get('lastPrice')

        # === STEP 3: Get contract info ===
        contract_res = await engine.api.get_contract_details(symbol)
        if not contract_res.success:
            return (
                f"\n{'='*50}\n"
                f" **ORDER REJECTED** - {symbol}\n"
                f"   Reason: CONTRACT ERROR\n"
                f"   \n"
                f"   Could not fetch contract details for {symbol}.\n"
                f"   Error: {contract_res.message}\n"
                f"{'='*50}"
            )

        contract_size = contract_res.data.get('contractSize')
        price_step = contract_res.data.get('priceUnit')

        logger.debug(f"{symbol} Tick Size: {price_step} | Current Price: {current_price}")

        # === STEP 4: Calculate volume ===
        margin_amount = balance * (equity_perc / 100.0)
        position_value = margin_amount * leverage
        vol = int(position_value / (contract_size * current_price))

        if vol == 0:
            return (
                f"\n{'='*50}\n"
                f" **ORDER REJECTED** - {symbol}\n"
                f"   Reason: INSUFFICIENT BALANCE\n"
                f"   \n"
                f"   Calculated volume is 0 contracts.\n"
                f"   \n"
                f"   Account Details:\n"
                f"   * Balance: {balance:.2f} {quote_currency}\n"
                f"   * Size: {equity_perc}%\n"
                f"   * Leverage: x{leverage}\n"
                f"   \n"
                f"   Increase balance or position size percentage.\n"
                f"{'='*50}"
            )

        # === STEP 5: Adjust prices ===
        final_sl_price = adjust_price_to_step(sl_price_raw, price_step)
        final_tp_price = adjust_price_to_step(tp3_price_raw, price_step)

        # === STEP 6: Smart entry logic ===
        use_market_order = True
        entry_price = None
        order_reason = "Market Entry"

        if entry_value != "Market" and isinstance(entry_value, (int, float)):
            entry_price = adjust_price_to_step(entry_value, price_step)
            is_long = side == OrderSide.OpenLong

            if is_long:
                if current_price <= entry_price:
                    use_market_order = True
                    order_reason = f"MARKET (price {current_price} <= entry {entry_price})"
                else:
                    use_market_order = False
                    order_reason = f"LIMIT @ {entry_price} (waiting for pullback from {current_price})"
            else:  # SHORT
                if current_price >= entry_price:
                    use_market_order = True
                    order_reason = f"MARKET (price {current_price} >= entry {entry_price})"
                else:
                    use_market_order = False
                    order_reason = f"LIMIT @ {entry_price} (waiting for bounce from {current_price})"

        logger.info(f" Executing {side.name} {symbol} x{leverage} | Vol: {vol} | {order_reason}")

        # === STEP 7: Build and send order ===
        if use_market_order:
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
        else:
            open_req = CreateOrderRequest(
                symbol=symbol,
                side=side,
                vol=vol,
                leverage=leverage,
                openType=OpenType.Cross,
                type=OrderType.PriceLimited,
                price=entry_price,
                stopLossPrice=final_sl_price,
                takeProfitPrice=final_tp_price
            )

        order_res = await engine.api.create_order(open_req)

        if not order_res.success:
            order_type_str = "MARKET" if use_market_order else f"LIMIT @ {entry_price}"
            return (
                f"\n{'='*50}\n"
                f" **ORDER FAILED** - {symbol}\n"
                f"   Side: {side.name}\n"
                f"   \n"
                f"   API Error: {order_res.message}\n"
                f"   \n"
                f"   Order Details:\n"
                f"   * Type: {order_type_str}\n"
                f"   * Volume: {vol}\n"
                f"   * Leverage: x{leverage}\n"
                f"   * TP3: {final_tp_price or 'None'}\n"
                f"   * SL: {final_sl_price or 'None'}\n"
                f"{'='*50}"
            )

        # === STEP 8: Start monitoring ===
        asyncio.create_task(
            engine.monitor_trade(symbol, vol, data['tps'], is_limit_order=not use_market_order)
        )

        order_type_str = "MARKET" if use_market_order else f"LIMIT @ {entry_price}"
        return (
            f" SUCCESS: {symbol} {side.name} x{leverage}\n"
            f"---------------------------------------\n"
            f" Vol: {vol} | Order: {order_type_str}\n"
            f" SL: {final_sl_price} | TP3: {final_tp_price}\n"
            f" Reason: {order_reason}\n"
        )

    async def _execute_update_signal(self, data: dict, engine) -> str:
        """
        Execute an UPDATE by modifying existing Position TP/SL order.
        """
        symbol = data['symbol']
        update_type = data['type']
        new_price_raw = data['price']

        logger.info(f"  PROCESSING UPDATE: {symbol} {update_type} -> {new_price_raw}")

        pos_res = await engine.api.get_open_positions(symbol)
        if not pos_res.success or not pos_res.data:
            return f"  Update Ignored: No open position found for {symbol}"

        contract_res = await engine.api.get_contract_details(symbol)
        price_step = contract_res.data.get('priceUnit') if contract_res.success else 0.01
        final_price = adjust_price_to_step(new_price_raw, price_step)

        orders_res = await engine.api.get_stop_limit_orders(symbol=symbol, is_finished=0)

        target_order = None

        if orders_res.success and orders_res.data:
            logger.debug(f"Found {len(orders_res.data)} active stop-limit orders.")

            for order in orders_res.data:
                is_dict = isinstance(order, dict)

                order_id = order.get('orderId') if is_dict else getattr(order, 'orderId', None)
                plan_id = order.get('id') if is_dict else getattr(order, 'id', None)
                t_side = order.get('triggerSide') if is_dict else getattr(order, 'triggerSide', None)

                side_val = t_side.value if hasattr(t_side, 'value') else (t_side or 0)

                curr_sl = order.get('stopLossPrice') if is_dict else getattr(order, 'stopLossPrice', None)
                curr_tp = order.get('takeProfitPrice') if is_dict else getattr(order, 'takeProfitPrice', None)

                logger.debug(f"  Order ID={plan_id or order_id} | Side={side_val} | SL={curr_sl} | TP={curr_tp}")

                match_found = False

                if side_val == 0:
                    match_found = True
                    logger.debug("Selected (Position TP/SL Plan)")
                elif update_type == 'SL' and side_val == 2:
                    match_found = True
                elif 'TP' in update_type and side_val == 1:
                    match_found = True

                if match_found:
                    target_order = {
                        'order_id': order_id,
                        'plan_id': plan_id,
                        'side': side_val,
                        'curr_sl': curr_sl,
                        'curr_tp': curr_tp,
                    }
                    break
        else:
            logger.debug("No active stop-limit orders found.")

        if not target_order:
            return f"  Update Skipped: No existing {update_type} order found to modify."

        use_plan_api = target_order['side'] == 0 or target_order['plan_id']
        order_id_to_use = target_order['plan_id'] or target_order['order_id']

        if update_type == 'SL':
            new_sl = final_price
            new_tp = target_order['curr_tp']
        else:
            new_sl = target_order['curr_sl']
            new_tp = final_price

        logger.info(f" Modifying Order {order_id_to_use}: SL={new_sl}, TP={new_tp}")

        try:
            if use_plan_api:
                logger.info(f"    Using: update_stop_limit_trigger_plan_price (Plan Order)")
                res = await engine.api.update_stop_limit_trigger_plan_price(
                    stop_plan_order_id=order_id_to_use,
                    stop_loss_price=new_sl,
                    take_profit_price=new_tp
                )
            else:
                logger.info(f"    Using: change_stop_limit_trigger_price (Regular Order)")
                res = await engine.api.change_stop_limit_trigger_price(
                    order_id=order_id_to_use,
                    stop_loss_price=new_sl,
                    take_profit_price=new_tp
                )

            if res.success:
                return f" SUCCESS: {symbol} {update_type} updated to {final_price}"
            else:
                if use_plan_api:
                    logger.warning(f"Plan API failed ({res.message}), trying regular API...")
                    res = await engine.api.change_stop_limit_trigger_price(
                        order_id=order_id_to_use,
                        stop_loss_price=new_sl,
                        take_profit_price=new_tp
                    )
                    if res.success:
                        return f"  SUCCESS: {symbol} {update_type} updated to {final_price}"

                return f"  MODIFICATION FAILED: {res.message}"

        except Exception as e:
            logger.error(f"Error calling modification API: {e}", exc_info=True)
            return f"  Error calling modification API: {e}"

    @property
    def supports_updates(self) -> bool:
        return True
