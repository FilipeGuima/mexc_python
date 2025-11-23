import uvicorn
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, CreateOrderRequest, OpenType, OrderType

# --- CONFIGURATION ---
MEXC_TOKEN = "REDACTED"
DEFAULT_PAIR = "BTC_USDT"

# --- DEFAULT PARAMETERS ---
DEFAULT_EQUITY_PERC = 0.5
DEFAULT_LEVERAGE = 10
DEFAULT_CLOSE_PERC = 100.0

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

API = MexcFuturesAPI(token=MEXC_TOKEN, testnet=False)
app = FastAPI(title="Dynamic MEXC Trading Webhook")


async def open_trade_position(pair: str, side: OrderSide, equity_perc: float, leverage: int):
    try:
        margin_currency = pair.split('_')[1]

        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}

        target_asset = next((asset for asset in assets_response.data if asset.currency == margin_currency), None)
        if not target_asset:
            return {"success": False, "error": f"{margin_currency} asset not found in wallet."}

        balance = getattr(target_asset, 'availableBalance', target_asset.equity)

        contract_details_response = await API.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}

        contract_size = contract_details_response.data.get('contractSize')

        raw_fee_rate = contract_details_response.data.get('takerFeeRate', 0.0002)

        if margin_currency == "USDT":
            fee_reserve_perc = 0.0
        else:
            slippage_buffer = 0.0004
            total_safety_rate = raw_fee_rate + slippage_buffer

            fee_reserve_perc = total_safety_rate * leverage * 100

        max_usable_perc = 100.0 - fee_reserve_perc

        effective_perc = equity_perc
        if effective_perc > max_usable_perc:
            effective_perc = max_usable_perc

        if effective_perc < 0:
            effective_perc = 0

        margin_amount = balance * (effective_perc / 100.0)

        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}
        current_price = ticker_response.data.get('lastPrice')

        position_size_value = margin_amount * leverage
        value_of_one_contract = contract_size * current_price

        vol = int(position_size_value / value_of_one_contract)

        if vol == 0:
            return {"success": False,
                    "error": f"Calculated volume is zero. Available: {balance:.2f} {margin_currency}. Need more funds or higher leverage."}

        order_request = CreateOrderRequest(
            symbol=pair,
            side=side,
            vol=vol,
            leverage=leverage,
            openType=OpenType.Cross,
            type=OrderType.MarketOrder
        )
        order_response = await API.create_order(order_request)

        if order_response.success:
            return {
                "success": True,
                "message": (f"Opened {side.name}: {vol} contracts. "
                            f"Used {effective_perc:.2f}% of Available {margin_currency} "
                            f"(Reserved {fee_reserve_perc:.2f}% for Fees/Slippage).")
            }
        else:
            return {"success": False, "error": order_response.message}

    except Exception as e:
        logger.error(f"Exception in open_trade_position: {e}")
        return {"success": False, "error": str(e)}


async def partially_close_trade(symbol: str, percentage: float):
    """Closes a position using Cross Margin."""
    try:
        if not 0 < percentage <= 100:
            return {"success": False, "error": "Percentage must be between 1 and 100."}

        positions_response = await API.get_open_positions(symbol=symbol)
        if not positions_response.success or not positions_response.data:
            return {"success": True, "message": f"No open positions to close for {symbol}."}

        closed_messages, error_messages = [], []

        for position in positions_response.data:
            vol_to_close = int(position.holdVol * (percentage / 100.0))
            if vol_to_close == 0: continue

            position_type_enum = PositionType(position.positionType)
            close_side = OrderSide.CloseLong if position_type_enum == PositionType.Long else OrderSide.CloseShort

            order_request = CreateOrderRequest(
                symbol=position.symbol,
                side=close_side,
                vol=vol_to_close,
                leverage=position.leverage,
                openType=OpenType.Cross,
                type=OrderType.MarketOrder
            )
            close_response = await API.create_order(order_request)

            if close_response.success:
                pos_type_str = "LONG" if position_type_enum == PositionType.Long else "SHORT"
                closed_messages.append(
                    f"Successfully placed order to close {percentage}% of {pos_type_str} ({vol_to_close} contracts).")
            else:
                error_messages.append(f"Failed to close {pos_type_str} position: {close_response.message}")

        final_message = "\n".join(closed_messages) + "\n" + "\n".join(error_messages)
        return {"success": True, "message": final_message.strip()}
    except Exception as e:
        logger.error(f"Exception in partially_close_trade: {e}")
        return {"success": False, "error": str(e)}


# --- WEBHOOK LISTENER ---
@app.post("/")
async def webhook(request: Request):
    msg_bytes = await request.body()
    msg = msg_bytes.decode('utf-8').strip()

    msg = msg.replace('"', '').replace("'", "")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n>>> {ts} - RECEIVED COMMAND: '{msg}'")

    try:
        parts = msg.split(',')
        command = parts[0].upper()
        args = parts[1:]
        result = {}

        if command in ["LONG", "SHORT"]:
            equity_perc = float(args[0]) if len(args) > 0 else DEFAULT_EQUITY_PERC
            leverage = int(args[1]) if len(args) > 1 else DEFAULT_LEVERAGE
            side = OrderSide.OpenLong if command == "LONG" else OrderSide.OpenShort
            result = await open_trade_position(DEFAULT_PAIR, side, equity_perc, leverage)

        elif command == "CLOSE":
            percentage = float(args[0]) if len(args) > 0 else DEFAULT_CLOSE_PERC
            result = await partially_close_trade(DEFAULT_PAIR, percentage)

        else:
            print(f"IGNORED UNKNOWN COMMAND: {command}")
            return {"status": "ignored", "reason": "unknown command"}

        print(f"Execution Result: {result}")
        if result.get("success"):
            return result
        else:
            raise HTTPException(status_code=500, detail=result.get("error", "Unknown error."))

    except (ValueError, IndexError) as e:
        print(f"Error parsing arguments: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid arguments: {e}")
    except Exception as e:
        print(f"Server Error: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")


if __name__ == "__main__":
    print("Starting Dynamic MEXC Trading Webhook server...")
    print("-----------------------------------------------")
    uvicorn.run(app, host="0.0.0.0", port=80, log_config=None)