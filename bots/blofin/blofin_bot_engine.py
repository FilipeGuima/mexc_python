"""
BlofinBotEngine - Shared engine for all Blofin trading strategies.

Handles: API init, message routing, order placement, monitoring loops,
position tracking, and update signals. Delegates strategy-specific logic
to the plugged-in BlofinStrategy instance.
"""

import asyncio
import logging
from datetime import datetime, timezone

from blofincpy.api import BlofinFuturesAPI
from bots.common.listener_interface import ListenerInterface
from bots.blofin.strategies.interface.strategy_interface import BlofinStrategy
from common.parser import parse_signal, UpdateParser
from common.utils import adjust_price_to_step, validate_signal_tp_sl


class BlofinBotEngine:
    def __init__(self, listener: ListenerInterface, strategy: BlofinStrategy,
                 api_key: str, secret_key: str, passphrase: str, testnet: bool):
        self.listener = listener
        self.strategy = strategy
        self.api = BlofinFuturesAPI(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            testnet=testnet
        )
        self.logger = logging.getLogger(strategy.name)

        # Shared state
        self.pending_orders = {}   # {order_id: {symbol, side, size, ...}}
        self.active_positions = {} # {symbol: {side, size, entry_price, ...}}

    def run(self):
        """Wire everything together and start the bot."""
        logging.basicConfig(
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            level=logging.INFO
        )

        # Check credentials
        if not self.api.api_key or not self.api.secret_key or not self.api.passphrase:
            print("CRITICAL ERROR: Blofin credentials (API_KEY, SECRET, PASSPHRASE) are missing")
            return

        # Wire listener callback
        self.listener.register_callback(self._handle_message)

        # Load strategy state
        state = self.strategy.get_state()
        if state:
            self.strategy.load_state(state)

        # Print banner
        start_time = getattr(self.listener, 'start_time', datetime.now(timezone.utc))
        print("=" * 50)
        print(f"   {self.strategy.name}")
        print("=" * 50)
        print(f" Start Time (UTC): {start_time}")
        print("-" * 50)

        # Connect listener (registers handlers + connects)
        self.listener.connect()

        # Run startup in event loop
        loop = asyncio.get_event_loop()
        loop.run_until_complete(self._startup())

        print("Waiting for signals... (Ctrl+C to stop)\n")

        try:
            self.listener.run_forever()
        except KeyboardInterrupt:
            print("\nBot stopped by user.")
        except Exception as e:
            print(f"\nCRITICAL ERROR: {e}")
            import traceback
            traceback.print_exc()

    async def _startup(self):
        """Initialize on startup: load positions, start monitor."""
        await self.load_existing_positions()
        asyncio.create_task(self._monitor_loop())
        self.logger.info("Position monitor started")

    # ===================================================================
    # MESSAGE ROUTING
    # ===================================================================

    async def _handle_message(self, text: str):
        """Route incoming messages to appropriate handlers."""
        text_upper = text.upper()

        # Route 1: New Trade Signals (PAIR + SIDE)
        if "PAIR" in text_upper and "SIDE" in text_upper:
            print(f"\n--- New Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

            signal_data = parse_signal(text)
            if not signal_data:
                print(" Failed to parse signal.")
                return

            symbol = signal_data['symbol']

            if signal_data['type'] == 'BREAKEVEN':
                print(f"  Processing BREAKEVEN for {symbol}...")
                res = await self.strategy.on_breakeven_signal(symbol, self)
                if res:
                    print(res)
                else:
                    print(f"  BREAKEVEN not supported by {self.strategy.name}")

            elif signal_data['type'] == 'TRADE':
                print(f"  Processing TRADE for {symbol}...")
                res = await self.execute_signal_trade(signal_data)
                print(res)

            return

        # Route 2: Update Signals (change TP/SL)
        if self.strategy.supports_updates:
            if any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET"]) and "/" in text:
                print(f"\n--- Update Signal Detected ({datetime.now().strftime('%H:%M:%S')}) ---")

                update_data = UpdateParser.parse(text)
                if update_data:
                    result = await self.execute_update_signal(update_data)
                    print(result)
                else:
                    print(" Update detected but failed to parse details.")

    # ===================================================================
    # TRADE EXECUTION
    # ===================================================================

    async def execute_signal_trade(self, data: dict) -> str:
        """Execute a trade signal. Shared flow for all strategies."""
        symbol_raw = data['symbol']
        formatted_symbol = symbol_raw.replace('_', '-')

        side = data['side']
        leverage = data['leverage']
        equity_perc = data['equity_perc']
        entry_price = data['entry']

        # Standard validation
        validation_error = validate_signal_tp_sl(data)
        if validation_error:
            return validation_error

        # Strategy-specific validation
        strategy_error = self.strategy.validate_signal(data)
        if strategy_error:
            return strategy_error

        # Entry price validation
        if not isinstance(entry_price, (int, float)):
            try:
                entry_price = float(str(entry_price).replace(',', ''))
            except (ValueError, TypeError):
                return (
                    f"\n{'='*50}\n"
                    f"  **ORDER REJECTED** - {formatted_symbol}\n"
                    f"   Reason: INVALID ENTRY PRICE\n"
                    f"   Entry value: '{entry_price}'\n"
                    f"   Expected: A numeric value (e.g., 95000, 0.55)\n"
                    f"{'='*50}"
                )

        blofin_side = "buy" if side == "LONG" else "sell"
        pos_side = "net"

        # Fetch balance
        assets = await self.api.get_user_assets()
        usdt_asset = next((a for a in assets if a.currency == "USDT"), None)
        if not usdt_asset:
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: WALLET ERROR\n"
                f"   USDT balance not found in account.\n"
                f"{'='*50}"
            )
        balance = usdt_asset.availableBalance

        # Get instrument info
        inst_info = await self.api.get_instrument_info(formatted_symbol)
        if not inst_info:
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: INSTRUMENT ERROR\n"
                f"   Could not get contract details for {formatted_symbol}.\n"
                f"{'='*50}"
            )

        self.logger.info(f" Instrument Info: {inst_info}")

        contract_value = float(inst_info.get('contractValue', 1))
        lot_size = float(inst_info.get('lotSize', 1))
        min_size = float(inst_info.get('minSize', lot_size))
        tick_size = float(inst_info.get('tickSize', 0.00001))

        # Round entry price
        entry_price = adjust_price_to_step(entry_price, tick_size)

        # Get TP config from strategy
        tp_config = self.strategy.get_tp_config(data, tick_size)
        is_scaled = tp_config.get('mode') == 'scaled'

        # Calculate volume
        margin_amount = balance * (equity_perc / 100.0)
        notional_value = margin_amount * leverage
        contract_usdt_value = contract_value * entry_price
        calculated_vol = notional_value / contract_usdt_value

        final_vol = round(calculated_vol / lot_size) * lot_size
        if final_vol < min_size:
            final_vol = min_size
        final_vol = round(final_vol, 8)

        self.logger.info(f" Balance: {balance:.2f} USDT | Size: {equity_perc}% | Vol: {final_vol}")

        # Get current price
        current_price = await self.get_current_price(formatted_symbol)
        if current_price == 0:
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: PRICE FETCH ERROR\n"
                f"   Could not fetch current market price.\n"
                f"{'='*50}"
            )

        self.logger.info(f" Current Price: {current_price} | Entry Price: {entry_price}")

        # Smart entry logic
        use_market_order = False
        order_reason = "LIMIT ORDER"

        if blofin_side == "buy":
            if current_price <= entry_price:
                use_market_order = True
                order_reason = f"MARKET (price {current_price} <= entry {entry_price})"
            else:
                order_reason = f"LIMIT @ {entry_price} (waiting for pullback from {current_price})"
        else:
            if current_price >= entry_price:
                use_market_order = True
                order_reason = f"MARKET (price {current_price} >= entry {entry_price})"
            else:
                order_reason = f"LIMIT @ {entry_price} (waiting for bounce from {current_price})"

        self.logger.info(f" Order Decision: {order_reason}")

        # Validate TP/SL direction
        actual_entry = current_price if use_market_order else entry_price

        if is_scaled:
            # Validate each TP level for scaled
            for key in ['tp1', 'tp2', 'tp3']:
                tp_val = tp_config.get(key)
                if tp_val:
                    if blofin_side == "buy" and tp_val <= actual_entry:
                        tp_config[key] = None
                    elif blofin_side == "sell" and tp_val >= actual_entry:
                        tp_config[key] = None
        else:
            tp_val = tp_config.get('tp')
            if tp_val:
                if blofin_side == "buy" and tp_val <= actual_entry:
                    self.logger.warning(f"TP ({tp_val}) should be above entry ({actual_entry}) for LONG - skipping TP")
                    tp_config['tp'] = None
                elif blofin_side == "sell" and tp_val >= actual_entry:
                    self.logger.warning(f"TP ({tp_val}) should be below entry ({actual_entry}) for SHORT - skipping TP")
                    tp_config['tp'] = None

        sl_val = tp_config.get('sl')
        if sl_val:
            if blofin_side == "buy" and sl_val >= actual_entry:
                self.logger.warning(f"SL ({sl_val}) should be below entry ({actual_entry}) for LONG - skipping SL")
                tp_config['sl'] = None
            elif blofin_side == "sell" and sl_val <= actual_entry:
                self.logger.warning(f"SL ({sl_val}) should be above entry ({actual_entry}) for SHORT - skipping SL")
                tp_config['sl'] = None

        # Build order info dict for strategy
        order_info = {
            'symbol': formatted_symbol,
            'side': blofin_side,
            'size': final_vol,
            'entry_price': entry_price,
            'leverage': leverage,
        }
        order_info.update(tp_config)

        # === MARKET ORDER ===
        if use_market_order:
            self.logger.info(f" Placing MARKET {blofin_side.upper()} {formatted_symbol} x{leverage} | Vol: {final_vol}")

            res = await self.api.create_market_order(
                symbol=formatted_symbol,
                side=blofin_side,
                vol=final_vol,
                leverage=leverage,
                position_side=pos_side
            )

            self.logger.info(f"Order Response: {res}")

            if res and res.get('code') == "0":
                order_data = res.get('data', {})
                if isinstance(order_data, list) and order_data:
                    order_id = order_data[0].get('orderId', 'N/A')
                elif isinstance(order_data, dict):
                    order_id = order_data.get('orderId', 'N/A')
                else:
                    order_id = 'N/A'

                # Wait for fill
                await asyncio.sleep(1.5)

                # Let strategy handle the fill
                await self.strategy.on_order_fill(order_id, order_info, final_vol, current_price, self)

                # Build success message
                if is_scaled:
                    order_msg = (
                        f"  **SCALED EXIT ORDER (Blofin)**\n"
                        f"   Symbol: {formatted_symbol}\n"
                        f"   Side: {blofin_side.upper()}\n"
                        f"   Entry: ~{current_price}\n"
                        f"   Size: {final_vol}\n"
                        f"   Leverage: x{leverage}\n"
                        f"   ---\n"
                        f"   Strategy:\n"
                        f"   TP1: {tp_config.get('tp1')} (50% close)\n"
                        f"   TP2: {tp_config.get('tp2')} (25% close + SL->entry)\n"
                        f"   TP3: {tp_config.get('tp3')} (close remaining)\n"
                        f"   SL: {tp_config.get('sl')}\n"
                    )
                else:
                    tp_label = "TP1" if "breakeven" in self.strategy.name.lower() else "TP3" if "tp3" in self.strategy.name.lower() else "TP"
                    tp_price = tp_config.get('tp')
                    sl_price = tp_config.get('sl')

                    order_msg = (
                        f"  **MARKET ORDER EXECUTED (Blofin)**\n"
                        f"   Symbol: {formatted_symbol}\n"
                        f"   Side: {blofin_side.upper()}\n"
                        f"   Entry: Market (~{current_price})\n"
                        f"   Size: {final_vol}\n"
                        f"   Lev: x{leverage}\n"
                    )

                    # Validate TP/SL against latest price
                    latest_price = await self.get_current_price(formatted_symbol) or current_price
                    valid_tp = tp_price
                    valid_sl = sl_price

                    if blofin_side == "buy":
                        if valid_tp and valid_tp <= latest_price:
                            valid_tp = None
                        if valid_sl and valid_sl >= latest_price:
                            valid_sl = None
                    else:
                        if valid_tp and valid_tp >= latest_price:
                            valid_tp = None
                        if valid_sl and valid_sl <= latest_price:
                            valid_sl = None

                    if valid_tp:
                        order_msg += f"   {tp_label}: {valid_tp}\n"
                    if valid_sl:
                        order_msg += f"   SL: {valid_sl}\n"

                return order_msg
            else:
                error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
                error_data = res.get('data', [])
                if error_data and isinstance(error_data, list) and error_data[0].get('msg'):
                    error_msg = error_data[0].get('msg')
                return (
                    f"\n{'='*50}\n"
                    f"  **MARKET ORDER FAILED** - {formatted_symbol}\n"
                    f"   Side: {blofin_side.upper()}\n"
                    f"   API Error: {error_msg}\n"
                    f"   Size: {final_vol} | Leverage: x{leverage}\n"
                    f"{'='*50}"
                )

        # === LIMIT ORDER ===
        self.logger.info(f" Placing LIMIT {blofin_side.upper()} {formatted_symbol} @ {entry_price} x{leverage} | Vol: {final_vol}")

        if is_scaled:
            # Scaled: no TP/SL on limit order, handled on fill
            res = await self.api.create_limit_order(
                symbol=formatted_symbol,
                side=blofin_side,
                vol=final_vol,
                price=entry_price,
                leverage=leverage,
                position_side=pos_side
            )
        else:
            tp_price = tp_config.get('tp')
            sl_price = tp_config.get('sl')
            res = await self.api.create_limit_order(
                symbol=formatted_symbol,
                side=blofin_side,
                vol=final_vol,
                price=entry_price,
                leverage=leverage,
                position_side=pos_side,
                take_profit=tp_price,
                stop_loss=sl_price
            )

        self.logger.info(f"Order Response: {res}")

        if res and res.get('code') == "0":
            order_data = res.get('data', {})
            if isinstance(order_data, list) and order_data:
                order_id = order_data[0].get('orderId', 'N/A')
            elif isinstance(order_data, dict):
                order_id = order_data.get('orderId', 'N/A')
            else:
                order_id = 'N/A'

            # Check if TP/SL were attached (for non-scaled)
            tpsl_attached = False
            if not is_scaled and order_id != 'N/A':
                await asyncio.sleep(0.5)
                pending = await self.api.get_pending_orders(formatted_symbol)
                for p in pending:
                    if str(p.get('orderId')) == str(order_id):
                        if p.get('tpTriggerPrice') or p.get('slTriggerPrice'):
                            tpsl_attached = True
                        break

            # Add to pending orders for monitoring
            if order_id != 'N/A':
                if is_scaled or not tpsl_attached:
                    self.pending_orders[order_id] = order_info
                    self.logger.info(f"Added {order_id} to monitoring queue")

            # Build response message
            if is_scaled:
                order_msg = (
                    f"  **SCALED EXIT LIMIT ORDER (Blofin)**\n"
                    f"   Symbol: {formatted_symbol}\n"
                    f"   Side: {blofin_side.upper()}\n"
                    f"   Entry: {entry_price}\n"
                    f"   Size: {final_vol}\n"
                    f"   Leverage: x{leverage}\n"
                    f"   Order ID: {order_id}\n"
                    f"   ---\n"
                    f"   Strategy (on fill):\n"
                    f"   TP1: {tp_config.get('tp1')} (50% close)\n"
                    f"   TP2: {tp_config.get('tp2')} (25% close + SL->entry)\n"
                    f"   TP3: {tp_config.get('tp3')} (close remaining)\n"
                    f"   SL: {tp_config.get('sl')}\n"
                    f"   Waiting for entry..."
                )
            else:
                tp_label = "TP1" if "breakeven" in self.strategy.name.lower() else "TP3" if "tp3" in self.strategy.name.lower() else "TP"
                tp_price = tp_config.get('tp')
                sl_price = tp_config.get('sl')

                order_msg = (
                    f"  **LIMIT ORDER PLACED (Blofin)**\n"
                    f"   Symbol: {formatted_symbol}\n"
                    f"   Side: {blofin_side.upper()}\n"
                    f"   Entry: {entry_price}\n"
                    f"   Size: {final_vol}\n"
                    f"   Lev: x{leverage}\n"
                    f"   Order ID: {order_id}\n"
                )
                if tp_price:
                    status = "ok" if tpsl_attached else "on fill"
                    order_msg += f"   {tp_label}: {tp_price} ({status})\n"
                if sl_price:
                    status = "ok" if tpsl_attached else "on fill"
                    order_msg += f"   SL: {sl_price} ({status})\n"
                order_msg += "   Waiting for price to reach entry..."

            return order_msg
        else:
            error_msg = res.get('msg', 'Unknown Error') if res else "No Response"
            error_data = res.get('data', [])
            if error_data and isinstance(error_data, list) and len(error_data) > 0 and error_data[0].get('msg'):
                error_msg = error_data[0].get('msg')
            return (
                f"\n{'='*50}\n"
                f"  **LIMIT ORDER FAILED** - {formatted_symbol}\n"
                f"   Side: {blofin_side.upper()}\n"
                f"   API Error: {error_msg}\n"
                f"   Entry: {entry_price} | Size: {final_vol} | Leverage: x{leverage}\n"
                f"{'='*50}"
            )

    # ===================================================================
    # UPDATE SIGNAL EXECUTION
    # ===================================================================

    async def execute_update_signal(self, data: dict) -> str:
        """Execute an UPDATE by modifying existing TPSL orders."""
        symbol_raw = data['symbol']
        formatted_symbol = symbol_raw.replace('_', '-')
        update_type = data['type']
        new_price_raw = data['price']

        print(f"  PROCESSING UPDATE: {formatted_symbol} {update_type} -> {new_price_raw}")

        # Get instrument info for price precision
        inst_info = await self.api.get_instrument_info(formatted_symbol)
        tick_size = float(inst_info.get('tickSize', 0.00001)) if inst_info else 0.00001
        final_price = adjust_price_to_step(new_price_raw, tick_size)

        # Get existing TPSL orders
        tpsl_orders = await self.api.get_tpsl_orders(formatted_symbol)
        print(f" DEBUG: Found {len(tpsl_orders)} active TPSL orders for {formatted_symbol}")

        # Try to get position info
        position_side = None
        hold_vol = None
        margin_mode = "isolated"

        positions = await self.api.get_open_positions(formatted_symbol)
        if positions and len(positions) > 0:
            position = positions[0]
            pos_side = position.positionType
            hold_vol = abs(position.holdVol)
            margin_mode = position.marginMode or "isolated"

            if pos_side == "net":
                position_side = "long" if position.holdVol > 0 else "short"
            else:
                position_side = pos_side

        # Fallback: get position info from TPSL orders
        if not position_side and tpsl_orders:
            first_tpsl = tpsl_orders[0]
            hold_vol = float(first_tpsl.get('size', 0))
            position_side = first_tpsl.get('posSide', 'long')
            margin_mode = first_tpsl.get('marginMode', 'isolated')

        # Fallback: order history
        if not position_side:
            history = await self.api.get_order_history(symbol=formatted_symbol)
            if history:
                for h in history:
                    if h.get('state') == 'filled':
                        hold_vol = float(h.get('filledSize', 0))
                        side = h.get('side', 'buy')
                        position_side = "long" if side == "buy" else "short"
                        break

        if not tpsl_orders and not position_side:
            return f"  Update Ignored: No position or TPSL orders found for {formatted_symbol}"

        # Find target order
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
            # Create new TPSL order
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

            self.logger.info(f" Creating new TPSL: {tpsl_body}")
            res = await self.api._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)
            self.logger.info(f" TPSL Response: {res}")

            if res and res.get('code') == "0":
                return f" SUCCESS: {formatted_symbol} {update_type} set to {final_price}"
            else:
                error_msg = res.get('msg', 'Unknown Error') if res else 'No Response'
                return f"  FAILED to create {update_type}: {error_msg}"

        # Try to amend existing order
        print(f" Amending TPSL Order {target_order['tpsl_id']}...")

        if update_type == 'SL':
            res = await self.api.amend_tpsl_order(
                symbol=formatted_symbol,
                tpsl_id=target_order['tpsl_id'],
                new_sl_trigger_price=final_price
            )
        else:
            res = await self.api.amend_tpsl_order(
                symbol=formatted_symbol,
                tpsl_id=target_order['tpsl_id'],
                new_tp_trigger_price=final_price
            )

        self.logger.info(f" Amend TPSL Response: {res}")

        if res and res.get('code') == "0":
            return f" SUCCESS: {formatted_symbol} {update_type} updated to {final_price}"

        # Amend failed - cancel and recreate
        error_msg = res.get('msg', 'Unknown') if res else 'No Response'
        print(f"    Amend failed ({error_msg}), trying cancel & recreate...")

        cancel_res = await self.api.cancel_tpsl_order(formatted_symbol, target_order['tpsl_id'])
        self.logger.info(f" Cancel TPSL Response: {cancel_res}")

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

        self.logger.info(f" Creating replacement TPSL: {tpsl_body}")
        new_res = await self.api._make_request("POST", "/api/v1/trade/order-tpsl", body=tpsl_body)
        self.logger.info(f" New TPSL Response: {new_res}")

        if new_res and new_res.get('code') == "0":
            return f" SUCCESS: {formatted_symbol} {update_type} updated to {final_price} (via recreate)"
        else:
            new_error = new_res.get('msg', 'Unknown') if new_res else 'No Response'
            return f"  FAILED to recreate {update_type}: {new_error}"

    # ===================================================================
    # MONITORING LOOP
    # ===================================================================

    async def _monitor_loop(self):
        """Background task: monitors pending orders, active positions, and strategy tick."""
        while True:
            try:
                # === PART 1: Monitor Pending Orders ===
                if self.pending_orders:
                    orders_to_remove = []
                    all_pending = await self.api.get_pending_orders()

                    for order_id, order_info in list(self.pending_orders.items()):
                        symbol = order_info['symbol']

                        our_order = None
                        for o in all_pending:
                            if str(o.get('orderId')) == str(order_id):
                                our_order = o
                                break

                        if our_order:
                            state = our_order.get('state', '')
                            if state == 'live':
                                self.logger.debug(f"Order {order_id} still pending for {symbol}")
                            elif state == 'filled':
                                filled_size = float(our_order.get('filledSize', 0))
                                avg_price = float(our_order.get('averagePrice', 0)) or order_info.get('entry_price')
                                await self._handle_order_filled(order_id, order_info, filled_size, avg_price)
                                orders_to_remove.append(order_id)
                        else:
                            history = await self.api.get_order_history(symbol=symbol, order_id=order_id)

                            if history:
                                hist_order = history[0] if isinstance(history, list) else history
                                state = hist_order.get('state', '')
                                filled_size = float(hist_order.get('filledSize', 0))
                                avg_price = float(hist_order.get('averagePrice', 0)) or order_info.get('entry_price')

                                if state == 'filled' and filled_size > 0:
                                    await self._handle_order_filled(order_id, order_info, filled_size, avg_price)
                                    orders_to_remove.append(order_id)
                                elif state in ['cancelled', 'canceled']:
                                    await self._handle_order_cancelled(order_id, order_info)
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
                                    await self._handle_order_filled(
                                        order_id, order_info,
                                        order_info.get('size'),
                                        order_info.get('entry_price')
                                    )
                                    orders_to_remove.append(order_id)

                    for oid in orders_to_remove:
                        if oid in self.pending_orders:
                            del self.pending_orders[oid]

                # === PART 2: Monitor Active Positions ===
                if self.active_positions:
                    positions_to_remove = []

                    for symbol, pos_info in list(self.active_positions.items()):
                        positions = await self.api.get_open_positions(symbol)

                        if positions and len(positions) > 0:
                            live_pos = positions[0]
                            pos_info['unrealized_pnl'] = live_pos.unrealized
                            pos_info['mark_price'] = live_pos.markPrice
                            pos_info['margin_ratio'] = live_pos.marginRatio
                            continue

                        tpsl_orders = await self.api.get_tpsl_orders(symbol)
                        if tpsl_orders and len(tpsl_orders) > 0:
                            continue

                        check_count = pos_info.get('_close_check_count', 0) + 1
                        pos_info['_close_check_count'] = check_count

                        if check_count >= 2:
                            await self._handle_position_closed(symbol, pos_info)
                            positions_to_remove.append(symbol)

                    for sym in positions_to_remove:
                        if sym in self.active_positions:
                            del self.active_positions[sym]

                # === PART 3: Strategy Tick ===
                await self.strategy.on_tick(self)

                await asyncio.sleep(5)

            except Exception as e:
                self.logger.error(f"Monitor error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(10)

    # ===================================================================
    # ORDER/POSITION EVENT HANDLERS
    # ===================================================================

    async def _handle_order_filled(self, order_id: str, order_info: dict,
                                    filled_size: float, fill_price: float):
        """Handle when a limit order is filled. Delegates to strategy."""
        symbol = order_info['symbol']
        side = order_info['side']

        fill_msg = (
            f"\n{'='*40}\n"
            f"  **LIMIT ORDER FILLED!**\n"
            f"   Symbol: {symbol}\n"
            f"   Side: {side.upper()}\n"
            f"   Entry: {fill_price}\n"
            f"   Size: {filled_size}\n"
            f"   Lev: x{order_info.get('leverage', 'N/A')}\n"
        )
        print(fill_msg)

        await self.strategy.on_order_fill(order_id, order_info, filled_size, fill_price, self)

    async def _handle_order_cancelled(self, order_id: str, order_info: dict):
        """Handle when an order is cancelled."""
        symbol = order_info['symbol']
        side = order_info['side']
        entry = order_info.get('entry_price', 'N/A')

        cancel_msg = (
            f"\n{'='*40}\n"
            f"  **ORDER CANCELLED**\n"
            f"   Symbol: {symbol}\n"
            f"   Side: {side.upper()}\n"
            f"   Entry: {entry}\n"
            f"   Order ID: {order_id}\n"
            f"{'='*40}"
        )
        print(cancel_msg)

    async def _handle_position_closed(self, symbol: str, pos_info: dict):
        """Handle when a position is closed. Determine reason and notify strategy."""
        from blofincpy.blofinTypes import CloseReason

        side = pos_info.get('side', 'unknown')
        entry_price = pos_info.get('entry_price', 0)
        tp_price = pos_info.get('tp')
        sl_price = pos_info.get('sl')
        leverage = pos_info.get('leverage', 1)
        size = pos_info.get('size', 0)

        close_reason = await self.api.get_position_close_reason(symbol)

        # Let strategy handle if it wants
        await self.strategy.on_position_closed(symbol, pos_info, close_reason, self)

        # Default close message
        if close_reason == CloseReason.TP:
            reason_str = f"TAKE PROFIT @ {tp_price}" if tp_price else "TAKE PROFIT"
            emoji = "TP"
        elif close_reason == CloseReason.SL:
            reason_str = f"STOP LOSS @ {sl_price}" if sl_price else "STOP LOSS"
            emoji = "SL"
        elif close_reason == CloseReason.LIQUIDATION:
            reason_str = "LIQUIDATED"
            emoji = "LIQ"
        elif close_reason == CloseReason.MANUAL:
            reason_str = "MANUAL CLOSE"
            emoji = "MANUAL"
        else:
            reason_str = "UNKNOWN"
            emoji = "?"

        close_msg = (
            f"\n{'='*40}\n"
            f" [{emoji}] **POSITION CLOSED** - {symbol}\n"
            f"   Side: {side.upper()}\n"
            f"   Entry: {entry_price}\n"
            f"   Size: {size} @ {leverage}x\n"
            f"   Reason: {reason_str}\n"
            f"{'='*40}"
        )
        print(close_msg)
        self.logger.info(f"Position closed: {symbol} - {reason_str}")

    # ===================================================================
    # HELPER METHODS (used by strategies via engine param)
    # ===================================================================

    async def set_tpsl_order(self, symbol: str, position_side: str, close_side: str,
                              size: float, tp_price: float = None, sl_price: float = None) -> dict:
        """Create a TPSL order via API. Returns raw API response."""
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

        self.logger.info(f" Creating TPSL: {body}")
        res = await self.api._make_request("POST", "/api/v1/trade/order-tpsl", body=body)
        self.logger.info(f" TPSL Response: {res}")
        return res

    async def cancel_tpsl_order(self, symbol: str, tpsl_id: str) -> bool:
        """Cancel a specific TPSL order. Returns True on success."""
        if not tpsl_id:
            return False
        res = await self.api.cancel_tpsl_order(symbol, tpsl_id)
        return res and res.get('code') == "0"

    async def get_current_price(self, symbol: str) -> float:
        """Fetch current market price for a symbol. Returns 0 on failure."""
        ticker_res = await self.api._make_request(
            "GET", "/api/v1/market/tickers",
            params={"instId": symbol}
        )
        if ticker_res and ticker_res.get('data'):
            return float(ticker_res['data'][0]['last'])
        return 0

    async def load_existing_positions(self):
        """Load existing open positions on startup for monitoring."""
        print("\nChecking for existing positions...")

        try:
            positions = await self.api.get_open_positions()

            if not positions:
                print("  No existing positions found.\n")
                return

            for pos in positions:
                symbol = pos.symbol

                tpsl_orders = await self.api.get_tpsl_orders(symbol)
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

                self.active_positions[symbol] = {
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
                print(f"  Loaded: {symbol} | {side.upper()} | Entry: {pos.openAvgPrice} | PnL: {pnl_str}")
                if tp_price or sl_price:
                    print(f"    TP: {tp_price or 'None'} | SL: {sl_price or 'None'}")

            print(f"\n  Total: {len(self.active_positions)} position(s) loaded for monitoring.\n")

        except Exception as e:
            self.logger.error(f"Error loading existing positions: {e}")
            print(f"  Warning: Could not load existing positions: {e}\n")
