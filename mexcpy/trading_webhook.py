import uvicorn
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, CreateOrderRequest, OpenType, OrderType

# --- CONFIGURATION ---
MEXC_TOKEN = "WEBff813a3694aebc6ba23dc64d0881510d8660e2a96226fd3e2c12d2943459c256"
DEFAULT_PAIR = "SUI_USDT"

# --- NEW: DEFAULT PARAMETERS CONFIGURATION ---
# These values are used when a webhook is received WITHOUT parameters.
DEFAULT_EQUITY_PERC = 0.5  # 0.5% of total equity
DEFAULT_LEVERAGE = 10  # 10x leverage
DEFAULT_CLOSE_PERC = 100.0  # 100% of the position

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

API = MexcFuturesAPI(token=MEXC_TOKEN, testnet=True)
app = FastAPI(title="Dynamic MEXC Trading Webhook")


# --- TRADING LOGIC (Unchanged) ---
# All your powerful, async trading functions are the same as before.
async def open_trade_position(pair: str, side: OrderSide, equity_perc: float, leverage: int):
    # ... (This function is exactly the same)
    try:
        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}
        usdt_asset = next((asset for asset in assets_response.data if asset.currency == "USDT"), None)
        if not usdt_asset: return {"success": False, "error": "USDT asset not found."}
        equity = usdt_asset.equity
        margin_in_usdt = equity * (equity_perc / 100.0)
        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}
        current_price = ticker_response.data.get('lastPrice')
        contract_details_response = await API.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}
        contract_size = contract_details_response.data.get('contractSize')
        position_size_usdt = margin_in_usdt * leverage
        value_of_one_contract_usdt = contract_size * current_price
        vol = int(position_size_usdt / value_of_one_contract_usdt)
        if vol == 0:
            return {"success": False, "error": "Calculated volume is zero. Increase equity percentage or leverage."}
        order_response = await API.create_market_order(pair, side, vol, leverage)
        if order_response.success:
            return {
                "success": True,
                "message": f"Opened {side.name}: {vol} contracts of {pair} using {margin_in_usdt:.2f} USDT as margin ({equity_perc}% equity @ {leverage}x leverage).",
            }
        else:
            return {"success": False, "error": order_response.message}
    except Exception as e:
        logger.error(f"Exception in open_trade_position: {e}")
        return {"success": False, "error": str(e)}


async def partially_close_trade(symbol: str, percentage: float):
    # ... (This function is exactly the same)
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
            close_response = await API.create_market_order(position.symbol, close_side, vol_to_close, position.leverage)
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


# --- NEW: DYNAMIC WEBHOOK LISTENER ---

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

# On Mac/Linux
#curl -X POST -d "LONG" http://127.0.0.1:80

# On Windows PowerShell
#curl http://127.0.0.1:80 -Method POST -Body "LONG"