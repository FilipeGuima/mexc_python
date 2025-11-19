import uvicorn
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, CreateOrderRequest, OpenType, OrderType

# --- CONFIGURATION ---
MEXC_TOKEN = "REDACTED"
DEFAULT_PAIR = "BTC_USDC"

# --- DEFAULT PARAMETERS CONFIGURATION ---
DEFAULT_EQUITY_PERC = 0.5
DEFAULT_LEVERAGE = 10
DEFAULT_CLOSE_PERC = 100.0

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

API = MexcFuturesAPI(token=MEXC_TOKEN, testnet=True)
app = FastAPI(title="Dynamic MEXC Trading Webhook")


# --- TRADING LOGIC (Hardcoded for Cross Margin) ---
async def open_trade_position(pair: str, side: OrderSide, equity_perc: float, leverage: int):
    """Opens a trade using Cross Margin."""
    try:
        margin_currency = pair.split('_')[1]

        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}

        target_asset = next((asset for asset in assets_response.data if asset.currency == margin_currency), None)

        if not target_asset:
            return {"success": False, "error": f"{margin_currency} asset not found in wallet."}

        equity = target_asset.equity

        margin_amount = equity * (equity_perc / 100.0)

        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}
        current_price = ticker_response.data.get('lastPrice')

        contract_details_response = await API.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}
        contract_size = contract_details_response.data.get('contractSize')

        position_size_value = margin_amount * leverage
        value_of_one_contract = contract_size * current_price

        vol = int(position_size_value / value_of_one_contract)

        if vol == 0:
            return {"success": False,
                    "error": f"Calculated volume is zero. Equity: {equity} {margin_currency}. Value required for 1 contract: {value_of_one_contract:.2f}"}

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
                "message": f"Opened {side.name}: {vol} contracts of {pair} using {margin_amount:.2f} {margin_currency} as margin ({equity_perc}% equity @ {leverage}x leverage) [Cross Margin].",
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
                    f"Successfully placed order to close {percentage}% of {pos_type_str} ({vol_to_close} contracts) [Cross Margin].")
            else:
                error_messages.append(f"Failed to close {pos_type_str} position: {close_response.message}")

        final_message = "\n".join(closed_messages) + "\n" + "\n".join(error_messages)
        return {"success": True, "message": final_message.strip()}
    except Exception as e:
        logger.error(f"Exception in partially_close_trade: {e}")
        return {"success": False, "error": str(e)}


# --- DYNAMIC WEBHOOK LISTENER ---
@app.post("/")
async def webhook(request: Request):
    """Receives a webhook and processes it like a terminal command."""
    msg_bytes = await request.body()
    msg = msg_bytes.decode('utf-8').strip()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info(f"{ts} - Received webhook command: '{msg}'")

    try:
        # Parse the incoming message for command and arguments
        parts = msg.split(',')
        command = parts[0].upper()
        args = parts[1:]
        result = {}

        if command in ["LONG", "SHORT"]:
            # Use provided parameters or fall back to defaults
            equity_perc = float(args[0]) if len(args) > 0 else DEFAULT_EQUITY_PERC
            leverage = int(args[1]) if len(args) > 1 else DEFAULT_LEVERAGE
            side = OrderSide.OpenLong if command == "LONG" else OrderSide.OpenShort
            result = await open_trade_position(DEFAULT_PAIR, side, equity_perc, leverage)

        elif command == "CLOSE":
            # Use provided percentage or fall back to default
            percentage = float(args[0]) if len(args) > 0 else DEFAULT_CLOSE_PERC
            result = await partially_close_trade(DEFAULT_PAIR, percentage)

        else:
            logger.warning(f"IGNORED UNKNOWN COMMAND: {command}")
            return {"status": "ignored", "reason": "unknown command"}

        logger.info(f"Execution result: {result}")
        if result.get("success"):
            return result
        else:
            raise HTTPException(status_code=500, detail=result.get("error", "Unknown error during execution."))

    except (ValueError, IndexError) as e:
        logger.error(f"Invalid arguments in command '{msg}': {e}")
        raise HTTPException(status_code=400, detail=f"Invalid arguments in command: {e}")
    except Exception as e:
        logger.error(f"Error executing command '{msg}': {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e}")


if __name__ == "__main__":
    print("Starting Dynamic MEXC Trading Webhook server...")
    print("To run, use the command: uvicorn trading_webhook:app --host 0.0.0.0 --port 80")
    uvicorn.run(app, host="0.0.0.0", port=80)