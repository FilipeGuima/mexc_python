import asyncio
from aiohttp import web

from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType

TOKEN = "WEB4fe4b377534c557ed822621b521dbcced6d03f469ae0610af920e5a726f77ec0"

DEFAULT_PAIR = "BTC_USDT"
DEFAULT_MARGIN_PERC = 1.0
DEFAULT_LEVERAGE = 20

SERVER_PORT = 80

API = MexcFuturesAPI(TOKEN, testnet=True)


async def open_trade_position(pair: str, side: OrderSide, margin_perc: float, leverage: int):

    try:
        assets_response = await API.get_user_assets()
        if not assets_response.success or not assets_response.data:
            return {"success": False, "error": "Could not fetch user assets."}

        usdt_asset = next((asset for asset in assets_response.data if asset.currency == "USDT"), None)
        if not usdt_asset:
            return {"success": False, "error": "USDT asset not found."}

        equity = usdt_asset.equity
        print(f"Total equity: {equity:.2f} USDT")

        ticker_response = await API.get_ticker(pair)
        if not ticker_response.success or not ticker_response.data:
            return {"success": False, "error": f"Could not fetch ticker for {pair}."}

        current_price = ticker_response.data.get('lastPrice')
        if not current_price:
            return {"success": False, "error": f"Invalid ticker data for {pair}."}
        print(f"Current price for {pair}: {current_price}")

        contract_details_response = await API.get_contract_details(pair)
        if not contract_details_response.success or not contract_details_response.data:
            return {"success": False, "error": f"Could not fetch contract details for {pair}."}

        contract_size = contract_details_response.data.get('contractSize')
        if not contract_size:
            return {"success": False, "error": f"Could not find contract size for {pair}."}
        print(f"Contract size: {contract_size}")

        margin_in_usdt = equity * (margin_perc / 100.0)
        position_size_usdt = margin_in_usdt * leverage

        value_of_one_contract_usdt = contract_size * current_price

        vol = int(position_size_usdt / value_of_one_contract_usdt)

        if vol == 0:
            return {"success": False, "error": f"Calculated volume is zero. Increase margin % or leverage."}

        print(
            f"Calculated Margin: {margin_in_usdt:.2f} USDT, Position Size: {position_size_usdt:.2f} USDT, Volume (Contracts): {vol}")

        order_response = await API.create_market_order(pair, side, vol, leverage)

        if order_response.success:
            print(f"Successfully placed {side.name} order for {vol} contracts of {pair}.")
            return {"success": True, "orderId": order_response.data.orderId, "volume": vol}
        else:
            print(f"Failed to place order: {order_response.message}")
            return {"success": False, "error": order_response.message}

    except Exception as e:
        print(f"An exception occurred in open_trade_position: {e}")
        return {"success": False, "error": str(e)}


async def close_all_trades():
    print("Attempting to close all positions...")
    positions_response = await API.get_open_positions()

    if not positions_response.success or not positions_response.data:
        print(" No open positions found to close.")
        return {"success": True, "message": "No open positions to close."}

    print(f"Found {len(positions_response.data)} open position(s) to close.")
    closed_orders = []
    for position in positions_response.data:
        position_type_enum = PositionType(position.positionType)
        close_side = OrderSide.CloseLong if position_type_enum == PositionType.Long else OrderSide.CloseShort

        print(f"--> Closing {position.holdVol} contracts of {position.symbol} ({position_type_enum.name})...")

        close_response = await API.create_market_order(
            position.symbol, close_side, position.holdVol, position.leverage
        )
        if close_response.success:
            order_id = close_response.data.orderId
            print(f" created closing order. Order ID: {order_id}")
            closed_orders.append({"symbol": position.symbol, "orderId": order_id})
        else:
            print(f" Failed to close position for {position.symbol}: {close_response.message}")

    return {"success": True, "closed_positions": closed_orders}

async def handle_request(request: web.Request):

    if request.method != "POST":
        return web.Response(text="Method not allowed", status=405)

    try:
        data = await request.json()
        command = data.get('command', '').upper()

        print(f"\n--- Received command: {command} ---")
        print(f"Request data: {data}")

        if command == "LONG" or command == "SHORT":
            side = OrderSide.OpenLong if command == "LONG" else OrderSide.OpenShort
            pair = data.get('pair', DEFAULT_PAIR)
            margin_perc = float(data.get('margin_perc', DEFAULT_MARGIN_PERC))
            leverage = int(data.get('leverage', DEFAULT_LEVERAGE))

            result = await open_trade_position(pair, side, margin_perc, leverage)
            return web.json_response(result)

        elif command == "CLOSE":
            result = await close_all_trades()
            return web.json_response(result)

        else:
            return web.json_response({"success": False, "error": "Invalid command"}, status=400)

    except Exception as e:
        print(f"Error processing request: {e}")
        return web.json_response({"success": False, "error": str(e)}, status=500)


app = web.Application()
app.router.add_post('/', handle_request)

print(f"Starting server on http://localhost:{SERVER_PORT}")

web.run_app(app, port=SERVER_PORT)