"""
MexcBotEngine - Shared engine for all MEXC trading strategies.

Handles: API init, message routing, shared helpers for balance/contract info,
volume calculation, and trade monitoring. Delegates strategy-specific logic
to the plugged-in MexcStrategy instance.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from mexcpy.api import MexcFuturesAPI
from bots.common.listener_interface import ListenerInterface
from bots.mexc.strategies.strategy_interface import MexcStrategy
from common.logger import setup_logging


class MexcBotEngine:
    def __init__(self, listener: ListenerInterface, strategy: MexcStrategy,
                 token: str, testnet: bool = False):
        self.listener = listener
        self.strategy = strategy
        self.api = MexcFuturesAPI(token=token, testnet=testnet)
        self.testnet = testnet
        self.logger = logging.getLogger(strategy.name)

    def run(self):
        """Wire everything together and start the bot."""
        setup_logging(self.strategy.name)

        # Wire listener callback
        self.listener.register_callback(self._handle_message)

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

        testnet_status = "TRUE" if self.testnet else "FALSE"
        self.logger.info(f"TESTNET STATUS: {testnet_status}")
        self.logger.info("Waiting for signals... (Ctrl+C to stop)")

        try:
            self.listener.run_forever()
        except KeyboardInterrupt:
            self.logger.info("Bot stopped by user.")
        except Exception as e:
            self.logger.critical(f"CRITICAL ERROR: {e}", exc_info=True)

    async def _startup(self):
        """Initialize on startup: check API, run strategy startup."""
        self.logger.info("Checking MEXC API connection...")

        res = await self.api.get_user_assets()
        if res.success and res.data:
            # Handle both single asset and list of assets
            data = res.data if isinstance(res.data, list) else [res.data]
            usdt = next((a for a in data if getattr(a, 'currency', None) == "USDT"), None)

            if usdt:
                bal = f"{usdt.availableBalance:.2f} USDT"
            else:
                available = [getattr(a, 'currency', 'unknown') for a in data]
                bal = f"No USDT found (available: {available})"
            self.logger.info(f"API OK | Balance: {bal}")
        else:
            self.logger.error(f"API FAILED: {res.message}")
            self.logger.warning("Bot will start but trades may fail!")

        # Run strategy-specific startup
        await self.strategy.on_startup(self)

    # ===================================================================
    # MESSAGE ROUTING
    # ===================================================================

    async def _handle_message(self, text: str):
        """Route incoming messages to strategy handler."""
        result = await self.strategy.handle_signal(text, self)
        if result:
            self.logger.info(result)

    # ===================================================================
    # SHARED HELPERS (used by strategies)
    # ===================================================================

    async def get_balance(self, quote_currency: str = "USDT") -> float:
        """Fetch available balance for a currency. Returns 0 on failure."""
        assets_response = await self.api.get_user_assets()

        # Debug: show raw response
        self.logger.info(f"Assets API response - success: {assets_response.success}, data type: {type(assets_response.data)}")

        if not assets_response.success:
            self.logger.warning(f"Failed to fetch assets: {assets_response.message}")
            return 0

        # Handle both single asset and list of assets
        data = assets_response.data
        if data is None:
            self.logger.warning("No asset data returned from API")
            return 0

        # If it's a single AssetInfo, wrap in list for iteration
        if not isinstance(data, list):
            data = [data]

        # Debug: show what we have
        self.logger.info(f"Assets found: {len(data)} items")
        for a in data:
            curr = getattr(a, 'currency', 'N/A')
            bal = getattr(a, 'availableBalance', 'N/A')
            self.logger.info(f"  - {curr}: {bal}")

        target_asset = next(
            (a for a in data if getattr(a, 'currency', None) == quote_currency),
            None
        )

        if target_asset:
            return target_asset.availableBalance
        else:
            # Log available currencies for debugging
            available = [getattr(a, 'currency', 'unknown') for a in data]
            self.logger.warning(f"Currency {quote_currency} not found. Available: {available}")
            return 0

    async def get_contract_info(self, symbol: str) -> dict:
        """
        Get contract info for a symbol.
        Returns dict with 'contract_size' and 'price_step', or empty dict on failure.
        """
        contract_res = await self.api.get_contract_details(symbol)
        if not contract_res.success:
            return {}

        return {
            'contract_size': contract_res.data.get('contractSize'),
            'price_step': contract_res.data.get('priceUnit'),
        }

    async def get_current_price(self, symbol: str) -> float:
        """Fetch current market price for a symbol. Returns 0 on failure."""
        ticker_res = await self.api.get_ticker(symbol)
        if not ticker_res.success or not ticker_res.data:
            return 0
        return ticker_res.data.get('lastPrice', 0)

    def calc_volume(self, balance: float, equity_perc: float, leverage: int,
                    contract_size: float, current_price: float) -> int:
        """Calculate order volume in contracts."""
        margin_amount = balance * (equity_perc / 100.0)
        position_value = margin_amount * leverage
        vol = int(position_value / (contract_size * current_price))
        return vol

    async def monitor_trade(self, symbol: str, start_vol: int, targets: list = None,
                            is_limit_order: bool = False):
        """
        Background task that monitors an open position for TP/SL hits.
        If is_limit_order=True, first waits for the limit order to fill.
        """
        if is_limit_order:
            self.logger.info(f"Waiting for limit order to fill for {symbol}...")
            fill_wait_count = 0
            max_wait_cycles = 720  # ~1 hour at 5s intervals

            while fill_wait_count < max_wait_cycles:
                await asyncio.sleep(5)
                fill_wait_count += 1

                try:
                    pos_res = await self.api.get_open_positions(symbol)
                    if pos_res.success and pos_res.data:
                        self.logger.info(f"Limit order FILLED for {symbol}! Position now open.")
                        start_vol = pos_res.data[0].holdVol
                        break

                    # Check if order was cancelled
                    orders_res = await self.api.get_current_pending_orders(symbol=symbol)
                    if orders_res.success:
                        has_pending = len(orders_res.data or []) > 0
                        if not has_pending:
                            self.logger.info(f"Limit order for {symbol} was CANCELLED or EXPIRED. Stopping monitor.")
                            return

                except Exception as e:
                    self.logger.error(f"Error checking limit order status: {e}")

            else:
                self.logger.info(f"Limit order for {symbol} did not fill within timeout. Stopping monitor.")
                return

        self.logger.info(f"Auto-monitoring started for {symbol}...")

        last_vol = start_vol
        first_run = True
        tp1_target = targets[0] if targets else None

        while True:
            await asyncio.sleep(5)

            try:
                pos_res = await self.api.get_open_positions(symbol)

                if not pos_res.success:
                    await asyncio.sleep(5)
                    continue

                if not pos_res.data:
                    # Position closed - determine reason
                    await asyncio.sleep(2)
                    reason = await self.detect_close_reason(symbol, tp1_target)

                    msg = f"**{symbol} Closed!** Reason: {reason}"
                    self.logger.info(msg)

                    # Clean up any remaining orders
                    await self.api.cancel_all_orders(symbol=symbol)
                    break

                position = pos_res.data[0]
                current_vol = position.holdVol

                if first_run:
                    last_vol = current_vol
                    first_run = False
                    continue

                if current_vol != last_vol:
                    last_vol = current_vol

            except Exception as e:
                self.logger.error(f"Monitor Exception for {symbol}: {e}")
                await asyncio.sleep(5)

    async def detect_close_reason(self, symbol: str, tp1_target: float = None) -> str:
        """Analyze stop order history to determine why position closed."""
        stop_res = await self.api.get_stop_limit_orders(
            symbol=symbol,
            is_finished=1,
            page_size=5
        )

        reason = "Manual Close / Unknown"

        if stop_res.success and stop_res.data:
            def get_time(item):
                return item.get('updateTime') if isinstance(item, dict) else item.updateTime

            sorted_stops = sorted(stop_res.data, key=get_time, reverse=True)

            if sorted_stops:
                last_stop = sorted_stops[0]

                if isinstance(last_stop, dict):
                    up_time = last_stop.get('updateTime', 0)
                    state_val = last_stop.get('state')
                    trig_price = float(last_stop.get('triggerPrice', 0))
                    trig_side = last_stop.get('triggerSide')
                else:
                    up_time = last_stop.updateTime
                    state_val = last_stop.state.value if hasattr(last_stop.state, 'value') else last_stop.state
                    trig_price = float(last_stop.triggerPrice)
                    trig_side = last_stop.triggerSide.value if hasattr(last_stop.triggerSide, 'value') else last_stop.triggerSide

                time_diff = (time.time() * 1000) - up_time

                if state_val == 3 and time_diff < 120000:
                    # triggerSide: 2 = TakeProfit, 1 = StopLoss
                    if trig_side == 2:
                        reason = "**TAKE PROFIT HIT**"
                    elif trig_side == 1:
                        reason = "**STOP LOSS HIT**"
                    elif tp1_target and abs(trig_price - tp1_target) / tp1_target < 0.005:
                        reason = f"**TAKE PROFIT HIT** (Target: {tp1_target})"
                    else:
                        reason = f"**STOP HIT** (Trigger: {trig_price})"

        return reason
