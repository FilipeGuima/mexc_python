import aiohttp
import asyncio
import json
import time
import logging
from typing import Optional, List, Dict, Any
from .sign import get_auth_headers
from .blofinTypes import (
    BlofinOrderRequest, OrderSide, OrderType, MarginMode,
    PositionSide, PositionInfo, AssetInfo, CloseReason
)

logger = logging.getLogger("BlofinAPI")


class RateLimiter:
    """Simple rate limiter to prevent API throttling."""
    def __init__(self, max_requests: int = 8, per_seconds: float = 1.0):
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self.requests = []
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.time()
            # Remove old requests outside the window
            self.requests = [t for t in self.requests if now - t < self.per_seconds]

            if len(self.requests) >= self.max_requests:
                # Wait until oldest request expires
                sleep_time = self.per_seconds - (now - self.requests[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                self.requests = self.requests[1:]

            self.requests.append(time.time())


# Shared rate limiter across all API instances
_global_rate_limiter = RateLimiter(max_requests=8, per_seconds=1.0)


class BlofinFuturesAPI:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, testnet: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.rate_limiter = _global_rate_limiter  # Share across instances

        if testnet:
            self.base_url = "https://demo-trading-openapi.blofin.com"
        else:
            self.base_url = "https://openapi.blofin.com"

    async def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                            body: Optional[Dict] = None):
        # Rate limit before making request
        await self.rate_limiter.acquire()
        url = f"{self.base_url}{endpoint}"
        request_path = endpoint

        if method == "GET" and params:
            # CRITICAL: Sort params alphabetically for correct signature generation
            sorted_params = sorted(params.items(), key=lambda x: x[0])
            query_string = "&".join([f"{k}={v}" for k, v in sorted_params])
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
                # Log rate limit headers if present (helps debug limits)
                rate_limit = response.headers.get('X-RateLimit-Limit')
                rate_remaining = response.headers.get('X-RateLimit-Remaining')
                if rate_remaining and int(rate_remaining) < 10:
                    logger.warning(f"Rate limit low: {rate_remaining}/{rate_limit} remaining")

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
        """
        Get open positions with full details.
        Pass symbol (instId) for faster, more accurate results.
        Returns empty list on API error.
        """
        params = {}
        if symbol:
            params["instId"] = symbol

        resp = await self._make_request("GET", "/api/v1/account/positions", params=params)

        code = resp.get("code", "error")
        if code != "0":
            logger.warning(f"get_open_positions API error: code={code}, msg={resp.get('msg', 'Unknown')}")
            return []

        positions = []
        data_obj = resp.get("data", [])
        source_list = data_obj if isinstance(data_obj, list) else []

        for item in source_list:
            hold_vol = float(item.get("positions", 0))
            if hold_vol == 0:
                continue

            pos = PositionInfo(
                positionId=item.get("positionId", ""),
                symbol=item.get("instId", ""),
                holdVol=abs(hold_vol),  # Use absolute value
                positionType=item.get("positionSide", "net"),
                openAvgPrice=float(item.get("averagePrice", 0)),
                liquidatePrice=float(item.get("liquidationPrice", 0) or 0),
                unrealized=float(item.get("unrealizedPnl", 0)),
                unrealizedPnlRatio=float(item.get("unrealizedPnlRatio", 0)),
                leverage=int(item.get("leverage", 1)),
                marginMode=item.get("marginMode", "cross"),
                marginRatio=float(item.get("marginRatio", 0)),
                margin=float(item.get("margin", 0) or item.get("initialMargin", 0) or 0),
                markPrice=float(item.get("markPrice", 0)),
                createTime=item.get("createTime", ""),
                updateTime=item.get("updateTime", "")
            )
            positions.append(pos)

        return positions

    async def get_position_close_reason(self, symbol: str) -> CloseReason:
        """
        Determine why a position was closed by checking TPSL and order history.
        Returns CloseReason enum.
        """
        # Check TPSL history first
        tpsl_resp = await self._make_request(
            "GET", "/api/v1/trade/orders-tpsl-history",
            params={"instId": symbol, "limit": "5"}
        )

        if tpsl_resp.get("code") == "0":
            data = tpsl_resp.get("data", [])
            if data:
                recent = data[0]
                state = recent.get("state", "")
                order_cat = recent.get("orderCategory", "")

                if state in ["filled", "effective", "triggered"]:
                    # Check which trigger was hit
                    tp_price = recent.get("tpTriggerPrice")
                    sl_price = recent.get("slTriggerPrice")

                    if order_cat == "tp" or (tp_price and tp_price != "0"):
                        return CloseReason.TP
                    elif order_cat == "sl" or (sl_price and sl_price != "0"):
                        return CloseReason.SL

        # Check regular order history for manual close or liquidation
        order_resp = await self._make_request(
            "GET", "/api/v1/trade/orders-history",
            params={"instId": symbol, "limit": "5"}
        )

        if order_resp.get("code") == "0":
            data = order_resp.get("data", [])
            for order in data:
                order_cat = order.get("orderCategory", "")
                if order_cat in ["full_liquidation", "partial_liquidation"]:
                    return CloseReason.LIQUIDATION
                elif order_cat == "tp":
                    return CloseReason.TP
                elif order_cat == "sl":
                    return CloseReason.SL
                elif order.get("reduceOnly") == "true" and order.get("state") == "filled":
                    return CloseReason.MANUAL

        return CloseReason.UNKNOWN

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

    async def get_order_history(self, symbol: Optional[str] = None, order_id: Optional[str] = None) -> List[Dict]:
        """Get order history (filled/cancelled orders)."""
        params = {"instType": "SWAP"}
        if symbol:
            params["instId"] = symbol
        if order_id:
            params["orderId"] = order_id

        resp = await self._make_request("GET", "/api/v1/trade/orders-history", params=params)

        if resp.get("code") == "0" and "data" in resp:
            return resp["data"] if isinstance(resp["data"], list) else []
        return []

    async def get_fills(self, symbol: Optional[str] = None, order_id: Optional[str] = None) -> List[Dict]:
        """Get trade fills/executions."""
        params = {"instType": "SWAP"}
        if symbol:
            params["instId"] = symbol
        if order_id:
            params["orderId"] = order_id

        resp = await self._make_request("GET", "/api/v1/trade/fills", params=params)

        if resp.get("code") == "0" and "data" in resp:
            return resp["data"] if isinstance(resp["data"], list) else []
        return []

    async def cancel_all_orders(self, symbol: Optional[str] = None):
        pass

    async def cancel_tpsl_order(self, symbol: str, tpsl_id: str):
        """Cancel a specific TPSL order."""
        body = {
            "instId": symbol,
            "tpslId": tpsl_id
        }
        return await self._make_request("POST", "/api/v1/trade/cancel-tpsl", body=body)

    async def amend_tpsl_order(
            self,
            symbol: str,
            tpsl_id: str,
            new_size: Optional[str] = None,
            new_tp_trigger_price: Optional[float] = None,
            new_sl_trigger_price: Optional[float] = None
    ):
        """Amend an existing TPSL order."""
        body = {
            "instId": symbol,
            "tpslId": tpsl_id
        }
        if new_size:
            body["newSize"] = str(new_size)
        if new_tp_trigger_price:
            body["newTpTriggerPrice"] = str(new_tp_trigger_price)
            body["newTpOrderPrice"] = "-1"
        if new_sl_trigger_price:
            body["newSlTriggerPrice"] = str(new_sl_trigger_price)
            body["newSlOrderPrice"] = "-1"

        return await self._make_request("POST", "/api/v1/trade/amend-tpsl", body=body)