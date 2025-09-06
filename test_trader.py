import asyncio
from mexcpy.api import MexcFuturesAPI
from mexcpy.mexcTypes import OrderSide, PositionType

token = "REDACTED"
api = MexcFuturesAPI(token, testnet=True)


async def open_position(symbol: str, side: OrderSide, vol: float, leverage: int):
    order_response = await api.create_market_order(symbol, side, vol, leverage)

    if order_response.success:
        print(f" opened position. Order ID: {order_response.data.orderId}")
    else:
        print(f" Failed: {order_response.message}")


async def close_all_positions():
    print("--> Attempting to close all open positions...")
    open_positions_response = await api.get_open_positions()

    if not open_positions_response.success or not open_positions_response.data:
        print(" No open positions found or failed to fetch positions.")
        return

    positions_to_close = open_positions_response.data
    print(f"Found {len(positions_to_close)} open position(s) to close.")

    for position in positions_to_close:
        position_type_enum = PositionType(position.positionType)
        close_side = (
            OrderSide.CloseLong if position_type_enum == PositionType.Long
            else OrderSide.CloseShort
        )

        symbol = position.symbol
        vol_to_close = position.holdVol
        leverage = position.leverage

        print(f"Closing {position_type_enum.name} position for {vol_to_close} {symbol}...")  # MODIFIED

        close_order_response = await api.create_market_order(
            symbol=symbol,
            side=close_side,
            vol=vol_to_close,
            leverage=leverage
        )

        if close_order_response.success:
            print(f" Successfully created closing order for {symbol}.")
        else:
            print(f" Failed to create closing order for {symbol}: {close_order_response.message}")


async def main():
    #  Open a LONG
    await open_position("BTC_USDT", OrderSide.OpenLong, 1, 20)
    await asyncio.sleep(3)

    # Open a SHORT
    await open_position("BTC_USDT", OrderSide.OpenShort, 1, 10)
    await asyncio.sleep(3)

    await close_all_positions()

if __name__ == "__main__":
    asyncio.run(main())