"""
MEXC Breakeven Strategy Implementation

- Uses market orders only (no smart entry)
- Supports BREAKEVEN signals (move SL to entry)
- Uses its own ASCII-based parser (different from common parser)
- Resumes monitoring on startup
- Matches original telegram_listener_breakeven.py behavior exactly
"""

import asyncio
import logging
import re
from typing import Optional

from mexcpy.mexcTypes import (
    OrderSide, CreateOrderRequest, OpenType, OrderType,
    TriggerOrderRequest, TriggerType, TriggerPriceType, ExecuteCycle
)
from bots.mexc.strategies.strategy_interface import MexcStrategy
from common.utils import adjust_price_to_step, validate_signal_tp_sl

logger = logging.getLogger("MexcBreakevenStrategy")


class MexcBreakevenStrategy(MexcStrategy):
    name = "MEXC BREAKEVEN BOT (Market Entry)"

    def parse_signal(self, text: str) -> Optional[dict]:
        """
        Parse signal using ASCII-based parser (specific to this strategy).
        Matches original telegram_listener_breakeven.py parse_signal() exactly.
        """
        try:
            # Strip non-ASCII characters
            text = text.encode('ascii', 'ignore').decode('ascii')
            text_clean = re.sub(r'[^a-zA-Z0-9\s.:,/%-]', '', text)
            text_clean = re.sub(r'\s+', ' ', text_clean).strip()

            print(f" DEBUG CLEANED: {text_clean}")

            # Ignore status messages
            if "TARGET HIT" in text_clean.upper() or "PROFIT:" in text_clean.upper():
                return {'valid': False, 'error': 'Ignored: Status/Profit update message'}

            # Parse PAIR
            pair_match = re.search(r"PAIR:\s*([A-Z0-9]+)[/_]([A-Z0-9]+)", text_clean, re.IGNORECASE)
            if not pair_match:
                return {'valid': False, 'error': 'Parsing Failed: No PAIR found in message'}

            symbol = f"{pair_match.group(1)}_{pair_match.group(2)}".upper()

            # Check for BREAKEVEN signal
            if "MOVE SL TO ENTRY" in text_clean.upper():
                return {
                    'valid': True,
                    'type': 'BREAKEVEN',
                    'symbol': symbol
                }

            # Parse SIDE
            side_match = re.search(r"SIDE:\s*(LONG|SHORT)", text_clean, re.IGNORECASE)
            if not side_match:
                return {'valid': False, 'error': f"Parsing Failed: Pair {symbol} found, but missing SIDE (LONG/SHORT)"}

            direction = side_match.group(1).upper()
            side = OrderSide.OpenLong if direction == "LONG" else OrderSide.OpenShort

            # Parse SIZE
            def parse_price(value_str):
                if not value_str:
                    return None
                clean_str = value_str.replace(',', '')
                return float(clean_str)

            size_match = re.search(r"SIZE:\s*(\d+)(?:-\s*(\d+))?%", text_clean, re.IGNORECASE)
            equity_perc = 1.0
            if size_match:
                val1 = float(size_match.group(1))
                val2 = float(size_match.group(2)) if size_match.group(2) else val1
                equity_perc = (val1 + val2) / 2

            # Parse ENTRY
            entry_match = re.search(r"ENTRY:\s*([\,\d.]+)", text_clean, re.IGNORECASE)
            entry_label = parse_price(entry_match.group(1)) if entry_match else "Market"

            # Parse SL
            sl_match = re.search(r"SL:\s*([\,\d.]+)", text_clean, re.IGNORECASE)
            sl_price = parse_price(sl_match.group(1)) if sl_match else None

            # Parse LEVERAGE
            lev_match = re.search(r"LEVERAGE:\s*(\d+)", text_clean, re.IGNORECASE)
            leverage = int(lev_match.group(1)) if lev_match else 20

            # Parse TPs
            all_tps = re.findall(r"TP\d:\s*([\,\d.]+)", text_clean, re.IGNORECASE)
            real_tps = []
            if all_tps:
                limit = 3 if len(all_tps) >= 3 else len(all_tps)
                real_tps = [parse_price(tp) for tp in all_tps[:limit]]

            return {
                'valid': True,
                'type': 'TRADE',
                'symbol': symbol,
                'side': side,
                'equity_perc': equity_perc,
                'leverage': leverage,
                'entry': entry_label,
                'sl': sl_price,
                'tps': real_tps
            }

        except Exception as e:
            logger.error(f"Parse Exception: {e}")
            return {'valid': False, 'error': f"Exception during parsing: {str(e)}"}

    async def handle_signal(self, text: str, engine) -> Optional[str]:
        """Route signal to appropriate handler."""
        if "PAIR:" not in text.upper():
            return None

        print(f"\n--- Signal Detected ---")

        result = self.parse_signal(text)

        if not result.get('valid'):
            error = result.get('error', 'Unknown error')
            if "Ignored" in error:
                print(f" -> {error}")
            else:
                print(f"  {error}")
            return None

        symbol = result['symbol']

        if result['type'] == 'BREAKEVEN':
            print(f"  Processing BREAKEVEN for {symbol}...")
            return await self._move_sl_to_entry(symbol, engine)

        elif result['type'] == 'TRADE':
            # Validate TP/SL
            validation_error = validate_signal_tp_sl(result)
            if validation_error:
                return validation_error

            print(f"  Processing TRADE for {symbol}...")
            return await self.execute_trade(result, engine)

        return None

    async def execute_trade(self, data: dict, engine) -> str:
        """
        Execute trade with market order (no smart entry).
        Matches original telegram_listener_breakeven.py execute_signal_trade() exactly.
        """
        symbol = data['symbol']
        leverage = data['leverage']
        side = data['side']
        equity_perc = data['equity_perc']
        entry_label = data['entry']

        sl_price_raw = data.get('sl')
        tp1_price_raw = data['tps'][0] if data['tps'] else None

        quote_currency = symbol.split('_')[1]

        # === STEP 1: Get balance (matching original error handling) ===
        assets_response = await engine.api.get_user_assets()
        if not assets_response.success:
            return (
                f"\n{'='*50}\n"
                f"❌ **ORDER REJECTED** - {symbol}\n"
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
                f"❌ **ORDER REJECTED** - {symbol}\n"
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
                f"❌ **ORDER REJECTED** - {symbol}\n"
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
                f"❌ **ORDER REJECTED** - {symbol}\n"
                f"   Reason: CONTRACT ERROR\n"
                f"   \n"
                f"   Could not fetch contract details for {symbol}.\n"
                f"   Error: {contract_res.message}\n"
                f"{'='*50}"
            )

        contract_size = contract_res.data.get('contractSize')
        price_step = contract_res.data.get('priceUnit')

        # === STEP 4: Calculate volume ===
        margin_amount = balance * (equity_perc / 100.0)
        position_value = margin_amount * leverage
        vol = int(position_value / (contract_size * current_price))

        if vol == 0:
            return (
                f"\n{'='*50}\n"
                f"❌ **ORDER REJECTED** - {symbol}\n"
                f"   Reason: INSUFFICIENT BALANCE\n"
                f"   \n"
                f"   Calculated volume is 0 contracts.\n"
                f"   \n"
                f"   Account Details:\n"
                f"   • Balance: {balance:.2f} {quote_currency}\n"
                f"   • Size: {equity_perc}%\n"
                f"   • Leverage: x{leverage}\n"
                f"   \n"
                f"   Increase balance or position size percentage.\n"
                f"{'='*50}"
            )

        logger.info(f" Executing {side.name} {symbol} x{leverage} | Vol: {vol}")

        # === STEP 5: Adjust prices ===
        final_sl_price = adjust_price_to_step(sl_price_raw, price_step)
        final_tp_price = adjust_price_to_step(tp1_price_raw, price_step)

        # === STEP 6: Always use market order for this strategy ===
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

        order_res = await engine.api.create_order(open_req)

        if not order_res.success:
            return (
                f"\n{'='*50}\n"
                f"❌ **ORDER FAILED** - {symbol}\n"
                f"   Side: {side.name}\n"
                f"   \n"
                f"   API Error: {order_res.message}\n"
                f"   \n"
                f"   Order Details:\n"
                f"   • Type: MARKET\n"
                f"   • Volume: {vol}\n"
                f"   • Leverage: x{leverage}\n"
                f"   • TP1: {final_tp_price or 'None'}\n"
                f"   • SL: {final_sl_price or 'None'}\n"
                f"{'='*50}"
            )

        # === STEP 7: Start monitoring ===
        asyncio.create_task(engine.monitor_trade(symbol, vol, data['tps']))

        return (
            f"  **SUCCESS: {symbol} {side.name} x{leverage}**\n"
            f"---------------------------------------\n"
            f" Pos Size: {equity_perc}% (Midpoint) | Vol: {vol}\n"
            f" Entry: {entry_label}\n"
            f" SL: {final_sl_price} (Set on Position )\n"
            f" TP1: {final_tp_price} (Set on Position )\n"
        )

    async def _move_sl_to_entry(self, symbol: str, engine) -> str:
        """
        Move SL to entry price for breakeven.
        Matches original telegram_listener_breakeven.py move_sl_to_entry() exactly.
        """
        logger.info(f"Processing 'Move SL to Entry' for {symbol}...")

        pos_res = await engine.api.get_open_positions(symbol)
        if not pos_res.success:
            return f" API Error checking positions: {pos_res.message}"

        if not pos_res.data:
            return f"  Cannot Move SL: No open position found for {symbol}"

        position = pos_res.data[0]
        entry_price = float(position.openAvgPrice)
        vol = position.holdVol
        leverage = position.leverage
        pos_type = position.positionType

        contract_res = await engine.api.get_contract_details(symbol)
        price_step = contract_res.data.get('priceUnit') if contract_res.success else 0.01
        final_sl_price = adjust_price_to_step(entry_price, price_step)

        existing_sl_order_id = None
        existing_tp_price = None

        print(f"    Scanning for SL on {symbol} (Entry: {entry_price})...")

        for attempt in range(3):
            stops_res = await engine.api.get_stop_limit_orders(symbol=symbol, is_finished=0)

            if stops_res.success and stops_res.data:
                for order in stops_res.data:
                    is_dict = isinstance(order, dict)
                    o_id = order.get('id') if is_dict else order.id

                    t_price = (
                        order.get('triggerPrice') if is_dict else getattr(order, 'triggerPrice', None)
                    ) or (
                        order.get('stopLossPrice') if is_dict else getattr(order, 'stopLossPrice', None)
                    ) or (
                        order.get('price') if is_dict else getattr(order, 'price', None)
                    )

                    tp_val = order.get('takeProfitPrice') if is_dict else getattr(order, 'takeProfitPrice', None)

                    if t_price is None:
                        continue
                    t_price = float(t_price)

                    is_sl = False
                    if pos_type == 1:  # Long
                        if t_price < entry_price:
                            is_sl = True
                    elif pos_type == 2:  # Short
                        if t_price > entry_price:
                            is_sl = True

                    if is_sl:
                        existing_sl_order_id = o_id
                        existing_tp_price = tp_val
                        break

            if existing_sl_order_id:
                break
            await asyncio.sleep(1.5)

        if existing_sl_order_id:
            print(f"   Identified SL (ID: {existing_sl_order_id}). Editing...")

            res = await engine.api.update_stop_limit_trigger_plan_price(
                stop_plan_order_id=existing_sl_order_id,
                stop_loss_price=final_sl_price,
                take_profit_price=existing_tp_price
            )

            if res.success:
                tp_msg = f" (TP kept at {existing_tp_price})" if existing_tp_price else " (No TP found)"
                return f"  **SL Updated to Entry!**\n   Symbol: {symbol}\n   New SL: {final_sl_price}\n  {tp_msg}"

            print(f"    Edit Failed ({res.message}). executing CANCEL & REPLACE strategy...")
            await engine.api.cancel_stop_limit_order(stop_plan_order_id=existing_sl_order_id)

        else:
            print("    No existing SL identified. Creating NEW SL...")

        # Create new SL order
        if pos_type == 1:  # Long
            sl_side = OrderSide.CloseLong
            trigger_type = TriggerType.LessThanOrEqual
        else:
            sl_side = OrderSide.CloseShort
            trigger_type = TriggerType.GreaterThanOrEqual

        sl_req = TriggerOrderRequest(
            symbol=symbol,
            side=sl_side,
            vol=vol,
            openType=OpenType.Cross,
            triggerType=trigger_type,
            triggerPrice=final_sl_price,
            leverage=leverage,
            orderType=OrderType.MarketOrder,
            executeCycle=ExecuteCycle.UntilCanceled,
            trend=TriggerPriceType.LatestPrice
        )
        res = await engine.api.create_trigger_order(sl_req)

        if res.success:
            return f"  **SL Created at Entry!** (New Order)\n   Symbol: {symbol}\n   New SL: {final_sl_price}"
        else:
            return f"   **Failed to Create SL**\n   Error: {res.message}"

    async def on_startup(self, engine):
        """
        Resume monitoring for existing positions on startup.
        Matches original telegram_listener_breakeven.py resume_monitoring() exactly.
        """
        print("\n  RESUME: Checking for existing open positions...")

        pos_res = await engine.api.get_open_positions()

        if pos_res.success and pos_res.data:
            count = 0
            for pos in pos_res.data:
                symbol = pos.symbol
                vol = pos.holdVol
                print(f"   Resuming monitor for {symbol} (Vol: {vol})")

                asyncio.create_task(engine.monitor_trade(symbol, vol, targets=[]))
                count += 1
            print(f"  Resumed monitoring for {count} positions.\n")
        else:
            print("  No existing positions found to resume.\n")

    @property
    def supports_updates(self) -> bool:
        return False
