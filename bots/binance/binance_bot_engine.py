"""
BinanceBotEngine - Shared engine for all Binance Futures trading strategies.

Handles: API init, message routing, order placement, monitoring loops,
position tracking. Delegates strategy-specific logic to the plugged-in
BinanceStrategy instance.

Key difference from Blofin: TP and SL are placed as separate orders
(TAKE_PROFIT_MARKET and STOP_MARKET), not a combined TPSL endpoint.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from binancecpy.api import BinanceFuturesAPI, BinanceAPIError
from bots.common.listener_interface import ListenerInterface
from bots.binance.strategies.interface.strategy_interface import BinanceStrategy
from common.parser import UpdateParser
from common.utils import adjust_price_to_step, validate_signal_tp_sl
from common.logger import setup_logging

STATE_DIR = Path(__file__).resolve().parent.parent.parent / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


class BinanceBotEngine:
    def __init__(self, listener: ListenerInterface, strategy: BinanceStrategy,
                 api_key: str, secret_key: str, testnet: bool):
        if not api_key:
            raise ValueError("Binance API_KEY is missing")
        if not secret_key:
            raise ValueError("Binance SECRET_KEY is missing")

        self.listener = listener
        self.strategy = strategy
        self.api = BinanceFuturesAPI(
            api_key=api_key,
            secret_key=secret_key,
            testnet=testnet
        )
        self.logger = logging.getLogger(strategy.name)

        # State file for pending orders (survives restarts)
        safe_name = strategy.name.lower().replace(' ', '_')
        self._state_file = STATE_DIR / f"{safe_name}_pending.json"

        # Shared state
        self.pending_orders = {}   # {order_id: {symbol, side, size, ...}}
        self.active_positions = {} # {symbol: {side, size, entry_price, tp_order_id, sl_order_id, ...}}

    # ===================================================================
    # PENDING ORDER PERSISTENCE
    # ===================================================================

    def _save_pending_orders(self):
        """Persist pending orders to disk so they survive restarts."""
        try:
            # JSON keys must be strings
            serializable = {str(k): v for k, v in self.pending_orders.items()}
            self._state_file.write_text(json.dumps(serializable, indent=2))
        except Exception as e:
            self.logger.error(f"Failed to save pending orders: {e}")

    def _load_pending_orders(self) -> dict:
        """Load pending orders from disk. Returns empty dict on failure."""
        if not self._state_file.exists():
            return {}
        try:
            data = json.loads(self._state_file.read_text())
            # Convert string keys back to int order IDs
            return {int(k): v for k, v in data.items()}
        except Exception as e:
            self.logger.error(f"Failed to load pending orders from {self._state_file}: {e}")
            return {}

    def _clear_pending_state(self):
        """Remove state file when no pending orders remain."""
        try:
            if self._state_file.exists():
                self._state_file.unlink()
        except Exception:
            pass

    def run(self):
        """Wire everything together and start the bot."""
        setup_logging(self.strategy.name)

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

        self.logger.info("Waiting for signals... (Ctrl+C to stop)")

        try:
            self.listener.run_forever()
        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user.")
        except Exception as e:
            self.logger.critical(f"FATAL: {type(e).__name__}: {e}", exc_info=True)
            raise

    async def _startup(self):
        """Initialize on startup: load positions, recover pending orders, start monitor."""
        await self.load_existing_positions()
        await self._recover_pending_orders()
        asyncio.create_task(self._monitor_loop())
        self.logger.info("Position monitor started")

    # ===================================================================
    # MESSAGE ROUTING
    # ===================================================================

    async def _handle_message(self, text: str):
        """Route incoming messages to appropriate handlers."""
        text_upper = text.upper()

        # Route 1: New Trade Signals (delegated to strategy's parser)
        if self.strategy.parser.can_handle(text):
            self.logger.info(f"New Signal Detected ({datetime.now().strftime('%H:%M:%S')})")

            signal_data = self.strategy.parser.parse(text)
            if not signal_data:
                self.logger.error("SIGNAL PARSE FAILED — raw text did not match expected format. Ignoring.")
                return

            symbol = signal_data['symbol']

            if signal_data['type'] == 'BREAKEVEN':
                self.logger.info(f"Processing BREAKEVEN for {symbol}...")
                res = await self.strategy.on_breakeven_signal(symbol, self)
                if res:
                    self.logger.info(res)
                else:
                    self.logger.info(f"BREAKEVEN not supported by {self.strategy.name}")

            elif signal_data['type'] == 'TRADE':
                self.logger.info(f"Processing TRADE for {symbol}...")
                res = await self.execute_signal_trade(signal_data)
                self.logger.info(res)

            return

        # Route 2: Update Signals (change TP/SL)
        if self.strategy.supports_updates:
            if any(k in text_upper for k in ["CHANGE", "ADJUST", "MOVE", "SET"]) and "/" in text:
                self.logger.info(f"Update Signal Detected ({datetime.now().strftime('%H:%M:%S')})")

                update_data = UpdateParser.parse(text)
                if update_data:
                    result = await self.execute_update_signal(update_data)
                    self.logger.info(result)
                else:
                    self.logger.error("UPDATE PARSE FAILED — detected keywords but could not extract data.")

    # ===================================================================
    # TRADE EXECUTION
    # ===================================================================

    async def execute_signal_trade(self, data: dict) -> str:
        """Execute a trade signal. Shared flow for all strategies."""
        symbol_raw = data['symbol']
        # Binance format: BTC_USDT -> BTCUSDT
        formatted_symbol = symbol_raw.replace('_', '')

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

        binance_side = "BUY" if side == "LONG" else "SELL"

        # Fetch balance
        assets = await self.api.get_user_assets()
        usdt_asset = next((a for a in assets if a.asset == "USDT"), None)
        if not usdt_asset:
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: WALLET ERROR\n"
                f"   USDT balance not found in account.\n"
                f"{'='*50}"
            )
        balance = usdt_asset.availableBalance

        # Get instrument info — raises BinanceAPIError if symbol not found
        inst_info = await self.api.get_instrument_info(formatted_symbol)

        self.logger.info(f" Instrument Info: {inst_info}")

        tick_size = float(inst_info['tickSize'])
        step_size = float(inst_info['stepSize'])
        min_qty = float(inst_info['minQty'])

        # Round entry price
        entry_price = adjust_price_to_step(entry_price, tick_size)

        # Get TP config from strategy
        tp_config = self.strategy.get_tp_config(data, tick_size)

        # Calculate volume
        # Binance USDT-M: quantity is in base asset (e.g., BTC), not contracts
        margin_amount = balance * (equity_perc / 100.0)
        notional_value = margin_amount * leverage
        calculated_qty = notional_value / entry_price

        final_qty = round(calculated_qty / step_size) * step_size
        if final_qty < min_qty:
            final_qty = min_qty
        # Round to step_size precision
        step_str = f"{step_size:.16f}".rstrip('0')
        qty_precision = len(step_str.split('.')[1]) if '.' in step_str else 0
        final_qty = round(final_qty, qty_precision)

        self.logger.info(f" Balance: {balance:.2f} USDT | Size: {equity_perc}% | Qty: {final_qty}")

        # Get current price — raises BinanceAPIError if ticker not found
        ticker = await self.api.get_ticker(formatted_symbol)
        current_price = float(ticker['price'])

        self.logger.info(f" Current Price: {current_price} | Entry Price: {entry_price}")

        # Sanity check: reject if entry is wildly off from current price
        deviation = abs(current_price - entry_price) / current_price
        if deviation > 0.90:
            return (
                f"\n{'='*50}\n"
                f"  **ORDER REJECTED** - {formatted_symbol}\n"
                f"   Reason: ENTRY PRICE SANITY CHECK FAILED\n"
                f"   Entry: {entry_price} vs Market: {current_price}\n"
                f"   Deviation: {deviation:.1%} (max allowed: 90%)\n"
                f"   The signal likely has the wrong pair.\n"
                f"{'='*50}"
            )

        # Smart entry logic
        use_market_order = False
        order_reason = "LIMIT ORDER"

        if binance_side == "BUY":
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

        tp_val = tp_config.get('tp')
        if tp_val:
            if binance_side == "BUY" and tp_val <= actual_entry:
                self.logger.warning(f"TP ({tp_val}) should be above entry ({actual_entry}) for LONG - skipping TP")
                tp_config['tp'] = None
            elif binance_side == "SELL" and tp_val >= actual_entry:
                self.logger.warning(f"TP ({tp_val}) should be below entry ({actual_entry}) for SHORT - skipping TP")
                tp_config['tp'] = None

        sl_val = tp_config.get('sl')
        if sl_val:
            if binance_side == "BUY" and sl_val >= actual_entry:
                self.logger.warning(f"SL ({sl_val}) should be below entry ({actual_entry}) for LONG - skipping SL")
                tp_config['sl'] = None
            elif binance_side == "SELL" and sl_val <= actual_entry:
                self.logger.warning(f"SL ({sl_val}) should be above entry ({actual_entry}) for SHORT - skipping SL")
                tp_config['sl'] = None

        # Set leverage and margin type
        await self.api.set_margin_type(formatted_symbol, "ISOLATED")
        await self.api.set_leverage(formatted_symbol, leverage)

        # Build order info dict for strategy
        order_info = {
            'symbol': formatted_symbol,
            'side': binance_side,
            'size': final_qty,
            'entry_price': entry_price,
            'leverage': leverage,
        }
        order_info.update(tp_config)

        # === MARKET ORDER ===
        if use_market_order:
            self.logger.info(f" Placing MARKET {binance_side} {formatted_symbol} x{leverage} | Qty: {final_qty}")

            res = await self.api.create_market_order(
                symbol=formatted_symbol,
                side=binance_side,
                quantity=final_qty,
            )

            self.logger.info(f"Order Response: {res}")

            order_id = res['orderId']

            # Wait for fill
            await asyncio.sleep(1.5)

            # Let strategy handle the fill
            await self.strategy.on_order_fill(order_id, order_info, final_qty, current_price, self)

            tp_price = tp_config.get('tp')
            sl_price = tp_config.get('sl')

            order_msg = (
                f"  **MARKET ORDER EXECUTED (Binance)**\n"
                f"   Symbol: {formatted_symbol}\n"
                f"   Side: {binance_side}\n"
                f"   Entry: Market (~{current_price})\n"
                f"   Qty: {final_qty}\n"
                f"   Lev: x{leverage}\n"
            )
            if tp_price:
                order_msg += f"   TP1: {tp_price}\n"
            if sl_price:
                order_msg += f"   SL: {sl_price}\n"

            return order_msg

        # === LIMIT ORDER ===
        self.logger.info(f" Placing LIMIT {binance_side} {formatted_symbol} @ {entry_price} x{leverage} | Qty: {final_qty}")

        res = await self.api.create_limit_order(
            symbol=formatted_symbol,
            side=binance_side,
            quantity=final_qty,
            price=entry_price,
        )

        self.logger.info(f"Order Response: {res}")

        order_id = res['orderId']

        # Add to pending orders for monitoring (TP/SL set on fill)
        self.pending_orders[order_id] = order_info
        self._save_pending_orders()
        self.logger.info(f"Added {order_id} to monitoring queue")

        tp_price = tp_config.get('tp')
        sl_price = tp_config.get('sl')

        order_msg = (
            f"  **LIMIT ORDER PLACED (Binance)**\n"
            f"   Symbol: {formatted_symbol}\n"
            f"   Side: {binance_side}\n"
            f"   Entry: {entry_price}\n"
            f"   Qty: {final_qty}\n"
            f"   Lev: x{leverage}\n"
            f"   Order ID: {order_id}\n"
        )
        if tp_price:
            order_msg += f"   TP1: {tp_price} (on fill)\n"
        if sl_price:
            order_msg += f"   SL: {sl_price} (on fill)\n"
        order_msg += "   Waiting for price to reach entry..."

        return order_msg

    # ===================================================================
    # UPDATE SIGNAL EXECUTION
    # ===================================================================

    async def execute_update_signal(self, data: dict) -> str:
        """Execute an UPDATE by cancelling and replacing TP or SL orders."""
        symbol_raw = data['symbol']
        formatted_symbol = symbol_raw.replace('_', '')
        update_type = data['type']
        new_price_raw = data['price']

        self.logger.info(f"PROCESSING UPDATE: {formatted_symbol} {update_type} -> {new_price_raw}")

        # Get instrument info for price precision — raises if symbol not found
        inst_info = await self.api.get_instrument_info(formatted_symbol)
        tick_size = float(inst_info['tickSize'])
        final_price = adjust_price_to_step(new_price_raw, tick_size)

        # Check if we track this position
        if formatted_symbol not in self.active_positions:
            return f"  Update Ignored: No tracked position for {formatted_symbol}"

        pos_data = self.active_positions[formatted_symbol]
        side = pos_data['side']
        size = pos_data['size']
        close_side = "SELL" if side == "BUY" else "BUY"

        if update_type == 'SL':
            # Cancel existing SL algo order
            old_sl_id = pos_data.get('sl_order_id')
            if old_sl_id:
                await self.api.cancel_algo_order(old_sl_id)
                self.logger.info(f"Cancelled old SL algo order {old_sl_id}")

            # Place new SL
            res = await self.api.create_stop_market_order(
                formatted_symbol, close_side, size, final_price
            )
            pos_data['sl'] = final_price
            pos_data['sl_order_id'] = res['algoId']
            return f" SUCCESS: {formatted_symbol} SL updated to {final_price}"

        elif 'TP' in update_type:
            # Cancel existing TP algo order
            old_tp_id = pos_data.get('tp_order_id')
            if old_tp_id:
                await self.api.cancel_algo_order(old_tp_id)
                self.logger.info(f"Cancelled old TP algo order {old_tp_id}")

            # Place new TP
            res = await self.api.create_take_profit_market_order(
                formatted_symbol, close_side, size, final_price
            )
            pos_data['tp'] = final_price
            pos_data['tp_order_id'] = res['algoId']
            return f" SUCCESS: {formatted_symbol} TP updated to {final_price}"

        raise ValueError(f"Unknown update type: {update_type}")

    # ===================================================================
    # MONITORING LOOP
    # ===================================================================

    async def _monitor_loop(self):
        """
        Background task: monitors pending orders, active positions, and strategy tick.

        Includes:
        - Exponential backoff on repeated errors
        - Circuit breaker pattern for API outages
        """
        consecutive_errors = 0
        max_consecutive_errors = 10
        base_sleep = 5
        max_error_sleep = 120

        while True:
            try:
                # === PART 1: Monitor Pending Orders ===
                if self.pending_orders:
                    orders_to_remove = []

                    for order_id, order_info in list(self.pending_orders.items()):
                        symbol = order_info['symbol']

                        order_status = await self.api.get_order(symbol, order_id)
                        status = order_status['status']

                        if status == 'NEW':
                            self.logger.debug(f"Order {order_id} still pending for {symbol}")
                        elif status == 'FILLED':
                            filled_qty = float(order_status['executedQty'])
                            avg_price = float(order_status['avgPrice'])
                            if avg_price == 0:
                                self.logger.warning(
                                    f"Order {order_id} avgPrice is 0 (API settlement delay), "
                                    f"using signal entry_price {order_info['entry_price']} instead"
                                )
                                avg_price = order_info['entry_price']
                            await self._handle_order_filled(order_id, order_info, filled_qty, avg_price)
                            orders_to_remove.append(order_id)
                        elif status in ('CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'):
                            await self._handle_order_cancelled(order_id, order_info)
                            orders_to_remove.append(order_id)
                        elif status == 'PARTIALLY_FILLED':
                            self.logger.debug(f"Order {order_id} partially filled for {symbol}")
                        else:
                            self.logger.error(f"Order {order_id} has unexpected status: '{status}'")

                    for oid in orders_to_remove:
                        if oid in self.pending_orders:
                            del self.pending_orders[oid]
                    if orders_to_remove:
                        if self.pending_orders:
                            self._save_pending_orders()
                        else:
                            self._clear_pending_state()

                # === PART 2: Monitor Active Positions ===
                if self.active_positions:
                    positions_to_remove = []

                    for symbol, pos_info in list(self.active_positions.items()):
                        positions = await self.api.get_open_positions(symbol)

                        if positions and len(positions) > 0:
                            live_pos = positions[0]
                            pos_info['unrealized_pnl'] = live_pos.unRealizedProfit
                            pos_info['mark_price'] = live_pos.markPrice
                            continue

                        # Position not found — might be closed
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

                # Success - reset error counter
                consecutive_errors = 0
                await asyncio.sleep(base_sleep)

            except asyncio.CancelledError:
                self.logger.info("Monitor loop cancelled, shutting down...")
                raise

            except (asyncio.TimeoutError, TimeoutError) as e:
                consecutive_errors += 1
                backoff = min(base_sleep * (2 ** consecutive_errors), max_error_sleep)
                self.logger.warning(
                    f"Monitor timeout (error {consecutive_errors}/{max_consecutive_errors}): {e}. "
                    f"Retrying in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)

            except BinanceAPIError as e:
                consecutive_errors += 1
                backoff = min(base_sleep * (2 ** consecutive_errors), max_error_sleep)
                self.logger.error(
                    f"Monitor API error (error {consecutive_errors}/{max_consecutive_errors}): "
                    f"code={e.code}, msg={e.msg}, endpoint={e.endpoint}. Retrying in {backoff:.0f}s..."
                )
                await asyncio.sleep(backoff)

                if consecutive_errors >= max_consecutive_errors:
                    self.logger.critical(
                        f"Circuit breaker triggered: {consecutive_errors} consecutive API errors. "
                        f"Backing off for {max_error_sleep}s before resuming..."
                    )
                    await asyncio.sleep(max_error_sleep)
                    consecutive_errors = max_consecutive_errors // 2

            except Exception as e:
                consecutive_errors += 1
                self.logger.error(
                    f"Monitor UNEXPECTED error (error {consecutive_errors}/{max_consecutive_errors}): "
                    f"{type(e).__name__}: {e}",
                    exc_info=True
                )

                if consecutive_errors >= max_consecutive_errors:
                    self.logger.critical(
                        f"Circuit breaker triggered: {consecutive_errors} consecutive errors. "
                        f"Backing off for {max_error_sleep}s before resuming..."
                    )
                    await asyncio.sleep(max_error_sleep)
                    consecutive_errors = max_consecutive_errors // 2
                else:
                    backoff = min(10 * consecutive_errors, max_error_sleep)
                    await asyncio.sleep(backoff)

    # ===================================================================
    # ORDER/POSITION EVENT HANDLERS
    # ===================================================================

    async def _handle_order_filled(self, order_id, order_info: dict,
                                    filled_size: float, fill_price: float):
        """Handle when a limit order is filled. Delegates to strategy."""
        symbol = order_info['symbol']
        side = order_info['side']
        leverage = order_info['leverage']

        fill_msg = (
            f"LIMIT ORDER FILLED! | Symbol: {symbol} | Side: {side} | "
            f"Entry: {fill_price} | Qty: {filled_size} | Lev: x{leverage}"
        )
        self.logger.info(fill_msg)

        await self.strategy.on_order_fill(order_id, order_info, filled_size, fill_price, self)

    async def _handle_order_cancelled(self, order_id, order_info: dict):
        """Handle when an order is cancelled."""
        symbol = order_info['symbol']
        side = order_info['side']
        entry = order_info['entry_price']

        cancel_msg = (
            f"ORDER CANCELLED | Symbol: {symbol} | Side: {side} | "
            f"Entry: {entry} | Order ID: {order_id}"
        )
        self.logger.info(cancel_msg)

    async def _handle_position_closed(self, symbol: str, pos_info: dict):
        """Handle when a position is closed. Determine reason and notify strategy."""
        from binancecpy.binanceTypes import CloseReason

        side = pos_info['side']
        entry_price = pos_info['entry_price']
        tp_price = pos_info.get('tp')
        sl_price = pos_info.get('sl')
        leverage = pos_info['leverage']
        size = pos_info['size']

        close_reason = await self._determine_close_reason(symbol, pos_info)

        # Let strategy handle if it wants
        await self.strategy.on_position_closed(symbol, pos_info, close_reason, self)

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
            f"POSITION CLOSED [{emoji}] | Symbol: {symbol} | Side: {side} | "
            f"Entry: {entry_price} | Qty: {size} @ {leverage}x | Reason: {reason_str}"
        )
        self.logger.info(close_msg)

    async def _determine_close_reason(self, symbol: str, pos_info: dict):
        """Determine why a position was closed by checking tracked algo orders."""
        from binancecpy.binanceTypes import CloseReason

        tp_algo_id = pos_info.get('tp_order_id')
        sl_algo_id = pos_info.get('sl_order_id')

        # Check tracked algo orders to see which one triggered
        for algo_id, reason in [(tp_algo_id, CloseReason.TP), (sl_algo_id, CloseReason.SL)]:
            if not algo_id:
                continue
            try:
                algo_order = await self.api.get_algo_order(algo_id)
                # actualOrderId is non-empty when the algo order triggered and placed a market order
                if algo_order.get('actualOrderId') and str(algo_order['actualOrderId']) != '0':
                    return reason
            except Exception as e:
                self.logger.debug(f"Could not query algo order {algo_id}: {e}")

        # Fallback: check regular order history for liquidation or manual close
        return await self.api.get_position_close_reason(symbol)

    # ===================================================================
    # HELPER METHODS (used by strategies via engine param)
    # ===================================================================

    async def set_tp_order(self, symbol: str, close_side: str, quantity: float, tp_price: float) -> dict:
        """Place a TAKE_PROFIT_MARKET order. Returns raw API response."""
        self.logger.info(f" Setting TP: {symbol} {close_side} qty={quantity} @ {tp_price}")
        return await self.api.create_take_profit_market_order(symbol, close_side, quantity, tp_price)

    async def set_sl_order(self, symbol: str, close_side: str, quantity: float, sl_price: float) -> dict:
        """Place a STOP_MARKET order. Returns raw API response."""
        self.logger.info(f" Setting SL: {symbol} {close_side} qty={quantity} @ {sl_price}")
        return await self.api.create_stop_market_order(symbol, close_side, quantity, sl_price)

    async def cancel_tp_sl_orders(self, symbol: str) -> int:
        """Cancel all open algo orders (TP/SL) for a symbol. Returns count cancelled."""
        algo_orders = await self.api.get_open_algo_orders(symbol)
        cancelled = 0
        for order in algo_orders:
            order_type = order.get('orderType', '')
            if order_type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
                algo_id = order['algoId']
                await self.api.cancel_algo_order(algo_id)
                cancelled += 1
        self.logger.info(f"Cancelled {cancelled} TP/SL algo orders for {symbol}")
        return cancelled

    async def get_current_price(self, symbol: str) -> float:
        """Fetch current market price for a symbol. Raises on failure."""
        ticker = await self.api.get_ticker(symbol)
        return float(ticker['price'])

    async def load_existing_positions(self):
        """Load existing open positions on startup for monitoring.
        On failure: logs error and continues — bot runs but without pre-loaded positions."""
        self.logger.info("Checking for existing positions...")

        try:
            positions = await self.api.get_open_positions()
        except Exception as e:
            self.logger.error(
                f"Failed to load existing positions on startup: {type(e).__name__}: {e}. "
                f"Bot will continue without pre-loaded position monitoring.",
                exc_info=True
            )
            return

        if not positions:
            self.logger.info("No existing positions found.")
            return

        for pos in positions:
            symbol = pos.symbol

            # Check for existing TP/SL algo orders
            algo_orders = await self.api.get_open_algo_orders(symbol)
            tp_price = None
            sl_price = None
            tp_order_id = None
            sl_order_id = None

            for order in algo_orders:
                order_type = order.get('orderType', '')
                if order_type == 'TAKE_PROFIT_MARKET':
                    tp_price = float(order['triggerPrice'])
                    tp_order_id = order['algoId']
                elif order_type == 'STOP_MARKET':
                    sl_price = float(order['triggerPrice'])
                    sl_order_id = order['algoId']

            side = "BUY" if pos.positionAmt > 0 else "SELL"

            self.active_positions[symbol] = {
                'side': side,
                'size': abs(pos.positionAmt),
                'entry_price': pos.entryPrice,
                'tp': tp_price,
                'sl': sl_price,
                'leverage': pos.leverage,
                'tp_order_id': tp_order_id,
                'sl_order_id': sl_order_id,
                'unrealized_pnl': pos.unRealizedProfit,
                'mark_price': pos.markPrice
            }

            pnl_str = f"+{pos.unRealizedProfit:.2f}" if pos.unRealizedProfit >= 0 else f"{pos.unRealizedProfit:.2f}"
            self.logger.info(f"Loaded: {symbol} | {side} | Entry: {pos.entryPrice} | PnL: {pnl_str} | TP: {tp_price} | SL: {sl_price}")

        self.logger.info(f"Total: {len(self.active_positions)} position(s) loaded for monitoring.")

    async def _recover_pending_orders(self):
        """Recover pending orders from disk state file.

        Cross-references saved orders against Binance open orders:
        - If order is still open on Binance → re-add to monitoring
        - If order was filled while bot was down → trigger fill handler
        - If order was cancelled/expired → clean up
        """
        saved = self._load_pending_orders()
        if not saved:
            return

        self.logger.info(f"Recovering {len(saved)} pending order(s) from previous session...")
        recovered = 0
        filled_while_down = 0

        for order_id, order_info in saved.items():
            symbol = order_info['symbol']
            try:
                order_status = await self.api.get_order(symbol, order_id)
                status = order_status['status']

                if status == 'NEW':
                    # Still pending — re-add to monitoring
                    self.pending_orders[order_id] = order_info
                    recovered += 1
                    self.logger.info(
                        f"  Recovered: {symbol} {order_info['side']} @ {order_info['entry_price']} "
                        f"(order {order_id}) — still pending"
                    )

                elif status == 'FILLED':
                    # Filled while bot was down — trigger fill handler
                    filled_qty = float(order_status['executedQty'])
                    avg_price = float(order_status['avgPrice'])
                    if avg_price == 0:
                        avg_price = order_info['entry_price']

                    self.logger.info(
                        f"  Order {order_id} for {symbol} FILLED while bot was down! "
                        f"Qty: {filled_qty} @ {avg_price} — setting up TP/SL now"
                    )
                    await self._handle_order_filled(order_id, order_info, filled_qty, avg_price)
                    filled_while_down += 1

                elif status in ('CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'):
                    self.logger.info(
                        f"  Order {order_id} for {symbol} was {status} — removing from state"
                    )

                else:
                    # Partially filled or unexpected — add to monitoring
                    self.pending_orders[order_id] = order_info
                    recovered += 1
                    self.logger.warning(
                        f"  Order {order_id} for {symbol} has status '{status}' — adding to monitor"
                    )

            except Exception as e:
                self.logger.error(
                    f"  Failed to recover order {order_id} for {symbol}: "
                    f"{type(e).__name__}: {e}"
                )

        # Save updated state (only orders still pending)
        if self.pending_orders:
            self._save_pending_orders()
        else:
            self._clear_pending_state()

        self.logger.info(
            f"Recovery complete: {recovered} pending, {filled_while_down} filled while down, "
            f"{len(saved) - recovered - filled_while_down} removed"
        )
