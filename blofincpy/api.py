import aiohttp
import json
from typing import Optional, List, Dict, Any
from .sign import get_auth_headers
from .blofinTypes import (
    BlofinOrderRequest, OrderSide, OrderType, MarginMode,
    PositionSide, PositionInfo, AssetInfo
)


class BlofinFuturesAPI:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, testnet: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase

        if testnet:
            self.base_url = "https://demo-trading-openapi.blofin.com"
        else:
            self.base_url = "https://openapi.blofin.com"

    async def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                            body: Optional[Dict] = None):
        url = f"{self.base_url}{endpoint}"
        request_path = endpoint

        if method == "GET" and params:
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            request_path = f"{endpoint}?{query_string}"
            url = f"{self.base_url}{request_path}"

        headers = get_auth_headers(
            request_path,
            method,
            body,
            self.api_key,
            self.secret_key,
            self.passphrase
        )

        data_payload = None
        if body is not None:
            data_payload = json.dumps(body, separators=(',', ':'))

        async with aiohttp.ClientSession() as session:
            async with session.request(
                    method,
                    url,
                    headers=headers,
                    data=data_payload
            ) as response:

                try:
                    return await response.json(content_type=None)
                except Exception:
                    text = await response.text()
                    return {"code": "error", "msg": f"Raw Response (Not JSON): {text}"}

    # --- Account ---
    async def get_user_assets(self) -> List[AssetInfo]:
        resp = await self._make_request("GET", "/api/v1/account/balance", params={"accountType": "futures"})

        assets = []
        if resp.get("code") == "0" and "data" in resp:
            data_obj = resp["data"]
            source_list = []

            if isinstance(data_obj, dict) and "details" in data_obj:
                source_list = data_obj["details"]
            elif isinstance(data_obj, list):
                source_list = data_obj

            for item in source_list:
                assets.append(AssetInfo(
                    currency=item.get("currency", item.get("asset", "")),
                    equity=float(item.get("equity", 0)),
                    availableBalance=float(item.get("available", 0)),
                    unrealized=float(item.get("unrealizedPl", item.get("isolatedUnrealizedPnl", 0)))
                ))
        return assets

    # --- Instruments ---
    async def get_instrument_info(self, symbol: str) -> Optional[Dict]:
        """Get instrument/contract details including contract size and tick size."""
        params = {"instType": "SWAP", "instId": symbol}
        resp = await self._make_request("GET", "/api/v1/market/instruments", params=params)

        if resp.get("code") == "0" and resp.get("data"):
            data = resp["data"]
            if isinstance(data, list) and len(data) > 0:
                return data[0]
        return None

    # --- Positions ---
    async def get_open_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        # Don't pass symbol to API - demo might not support it
        # We'll filter locally instead
        params = {"instType": "SWAP"}

        resp = await self._make_request("GET", "/api/v1/trade/positions", params=params)

        import logging
        logger = logging.getLogger("BlofinAPI")
        logger.info(f"Positions response: {resp}")

        positions = []
        if resp.get("code") == "0" and "data" in resp:
            data_obj = resp["data"]
            source_list = data_obj if isinstance(data_obj, list) else []

            for item in source_list:
                pos = PositionInfo(
                    positionId=item.get("posId", ""),
                    symbol=item.get("instId", ""),
                    holdVol=float(item.get("positions", 0)),
                    positionType=item.get("positionSide", ""),
                    openAvgPrice=float(item.get("avgPx", 0)),
                    liquidatePrice=float(item.get("liqPx", 0) or 0),
                    unrealized=float(item.get("unrealizedPl", 0)),
                    leverage=int(item.get("leverage", 1)),
                    marginMode=item.get("marginMode", "cross")
                )
                # Filter by symbol locally if specified
                if symbol is None or pos.symbol == symbol:
                    positions.append(pos)

        return positions

    # --- Leverage ---
    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "isolated", pos_side: str = "net"):
        """Set leverage for a symbol before placing orders."""
        body = {
            "instId": symbol,
            "leverage": str(leverage),
            "marginMode": margin_mode,
            "posSide": pos_side
        }
        result = await self._make_request("POST", "/api/v1/account/set-leverage", body=body)
        # Log the result for debugging
        import logging
        logger = logging.getLogger("BlofinAPI")
        logger.info(f"Set Leverage Response: {result}")
        return result

    # --- Orders ---
    async def create_market_order(
            self,
            symbol: str,
            side: str,
            vol: float,
            leverage: int,
            position_side: str = "net",
            reduce_only: bool = False,
            take_profit: Optional[float] = None,
            stop_loss: Optional[float] = None
    ):
        # Set leverage before placing order
        await self.set_leverage(symbol, leverage, "isolated", position_side)

        blofin_side = OrderSide.Buy if "long" in side.lower() or "buy" in side.lower() else OrderSide.Sell

        order_req = {
            "instId": symbol,
            "marginMode": "isolated",
            "side": blofin_side.value,
            "orderType": OrderType.Market.value,
            "size": str(vol),
            "reduceOnly": "true" if reduce_only else "false"
        }

        if position_side != "net":
            order_req["positionSide"] = position_side

        if take_profit:
            order_req["tpTriggerPrice"] = str(take_profit)
            order_req["tpOrderPrice"] = "-1"  # -1 means Market Price

        if stop_loss:
            order_req["slTriggerPrice"] = str(stop_loss)
            order_req["slOrderPrice"] = "-1"  # -1 means Market Price

        import logging
        logger = logging.getLogger("BlofinAPI")
        logger.info(f"Market Order Request: {order_req}")

        return await self._make_request("POST", "/api/v1/trade/order", body=order_req)

    async def create_limit_order(
            self,
            symbol: str,
            side: str,
            vol: float,
            price: float,
            leverage: int,
            position_side: str = "net",
            reduce_only: bool = False,
            take_profit: Optional[float] = None,
            stop_loss: Optional[float] = None
    ):
        """Place a limit order at a specific price."""
        # Set leverage before placing order
        await self.set_leverage(symbol, leverage, "isolated", position_side)

        blofin_side = OrderSide.Buy if "long" in side.lower() or "buy" in side.lower() else OrderSide.Sell

        order_req = {
            "instId": symbol,
            "marginMode": "isolated",
            "side": blofin_side.value,
            "orderType": OrderType.Limit.value,
            "price": str(price),
            "size": str(vol),
            "reduceOnly": "true" if reduce_only else "false"
        }

        if position_side != "net":
            order_req["positionSide"] = position_side

        # Attach TP/SL to the limit order
        if take_profit:
            order_req["tpTriggerPrice"] = str(take_profit)
            order_req["tpOrderPrice"] = "-1"

        if stop_loss:
            order_req["slTriggerPrice"] = str(stop_loss)
            order_req["slOrderPrice"] = "-1"

        import logging
        logger = logging.getLogger("BlofinAPI")
        logger.info(f"Limit Order Request: {order_req}")

        return await self._make_request("POST", "/api/v1/trade/order", body=order_req)

    async def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get pending/open orders."""
        params = {"instType": "SWAP"}
        if symbol:
            params["instId"] = symbol

        resp = await self._make_request("GET", "/api/v1/trade/orders-pending", params=params)

        import logging
        logger = logging.getLogger("BlofinAPI")
        logger.info(f"Pending orders API response: {resp}")

        if resp.get("code") == "0" and "data" in resp:
            data = resp["data"] if isinstance(resp["data"], list) else []
            return data
        return []

    async def cancel_order(self, symbol: str, order_id: str):
        """Cancel a specific order."""
        body = {
            "instId": symbol,
            "orderId": order_id
        }
        return await self._make_request("POST", "/api/v1/trade/cancel-order", body=body)

    async def get_tpsl_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get pending TPSL orders. Use this to verify TP/SL orders were created."""
        params = {"instType": "SWAP"}
        if symbol:
            params["instId"] = symbol

        resp = await self._make_request("GET", "/api/v1/trade/orders-tpsl-pending", params=params)

        if resp.get("code") == "0" and "data" in resp:
            return resp["data"] if isinstance(resp["data"], list) else []
        return []

    async def cancel_all_orders(self, symbol: Optional[str] = None):
        pass