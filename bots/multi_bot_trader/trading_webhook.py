import uvicorn
import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, Any
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType, CreateOrderRequest, OpenType, OrderType

load_dotenv()

ACCOUNTS = {}
bot_index = 1

while True:
    token_key = f"BOT{bot_index}_TOKEN"
    pair_key = f"BOT{bot_index}_PAIR"

    token = os.getenv(token_key)
    pair = os.getenv(pair_key)

    if not token:
        break

    ACCOUNTS[f"BOT{bot_index}"] = {
        "token": token,
        "pair": pair
    }
    bot_index += 1

print(f"Loaded {len(ACCOUNTS)} bots from configuration.")

IS_TESTNET = True

# --- DEFAULT TRADING PARAMETERS ---
DEFAULT_EQUITY_PERC = 0.5
DEFAULT_LEVERAGE = 10
DEFAULT_CLOSE_PERC = 100.0

# --- SETUP ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Multi-Account MEXC Trading Webhook")


# --- TRADING LOGIC ---
async def open_trade_position(api: MexcFuturesAPI, pair: str, side: OrderSide, equity_perc: float, leverage: int):
    try:
        margin_currency = pair.split('_')[1]

        assets_response = await api.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}

        target_asset = next((asset for asset in assets_response.data if asset.currency == margin_currency), None)
        if not target_asset:
            return {"success": False, "error": f"{margin_currency} asset not found in wallet."}

        balance = getattr(target_asset, 'availableBalance', target_asset.equity)

        contract_details_response = await api.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}

        contract_size = contract_details_response.data.get('contractSize')

        if equity_perc < 0: equity_perc = 0

        margin_amount = balance * (equity_perc / 100.0)

        ticker_response = await api.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}
        current_price = ticker_response.data.get('lastPrice')

        position_size_value = margin_amount * leverage
        value_of_one_contract = contract_size * current_price

        if value_of_one_contract == 0:
            return {"success": False, "error": "Contract value is zero (Price or Size error)."}

        vol = int(position_size_value / value_of_one_contract)

        if vol == 0:
            return {"success": False,
                    "error": f"Calculated volume is zero. Available: {balance:.2f} {margin_currency}."}

        order_request = CreateOrderRequest(
            symbol=pair,
            side=side,
            vol=vol,
            leverage=leverage,
            openType=OpenType.Cross,
            type=OrderType.MarketOrder
        )
        order_response = await api.create_order(order_request)

        if order_response.success:
            return {
                "success": True,
                "message": (f"Opened {side.name}: {vol} contracts on {pair}.")
            }
        else:
            return {"success": False, "error": order_response.message}

    except Exception as e:
        logger.error(f"Exception in open_trade_position: {e}")
        return {"success": False, "error": str(e)}


async def partially_close_trade(api: MexcFuturesAPI, symbol: str, percentage: float):
    try:
        if not 0 < percentage <= 100:
            return {"success": False, "error": "Percentage must be between 1 and 100."}

        positions_response = await api.get_open_positions(symbol=symbol)
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
            close_response = await api.create_order(order_request)

            if close_response.success:
                pos_type_str = "LONG" if position_type_enum == PositionType.Long else "SHORT"
                closed_messages.append(
                    f"Closed {percentage}% of {pos_type_str} ({vol_to_close} contracts).")
            else:
                error_messages.append(f"Failed to close {pos_type_str}: {close_response.message}")

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
    print(f"\n>>> {ts} - RECEIVED: '{msg}'")

    try:
        parts = msg.split(',')

        if len(parts) < 2:
            raise ValueError("Message too short. Must be: ACCOUNT,COMMAND,ARGS...")

        account_id = parts[0].strip().upper()
        command = parts[1].strip().upper()
        args = parts[2:]

        account_config = ACCOUNTS.get(account_id)
        if not account_config:
            raise ValueError(f"Account ID '{account_id}' not found in configuration.")

        token = account_config.get("token")
        default_pair = account_config.get("pair")

        if not token or not default_pair:
            raise ValueError(f"Configuration error for '{account_id}': Missing token or pair.")

        current_api = MexcFuturesAPI(token=token, testnet=IS_TESTNET)

        result = {}

        if command in ["LONG", "SHORT"]:
            equity_perc = float(args[0]) if len(args) > 0 else DEFAULT_EQUITY_PERC
            leverage = int(args[1]) if len(args) > 1 else DEFAULT_LEVERAGE
            side = OrderSide.OpenLong if command == "LONG" else OrderSide.OpenShort

            result = await open_trade_position(current_api, default_pair, side, equity_perc, leverage)

        elif command == "CLOSE":
            percentage = float(args[0]) if len(args) > 0 else DEFAULT_CLOSE_PERC
            result = await partially_close_trade(current_api, default_pair, percentage)

        else:
            print(f"IGNORED UNKNOWN COMMAND: {command}")
            return {"status": "ignored", "reason": "unknown command"}

        print(f"Execution Result ({account_id} on {default_pair}): {result}")

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
    print("Starting Multi-Account MEXC Trading Webhook...")
    uvicorn.run(app, host="0.0.0.0", port=80, log_config=None)