import aiohttp
import asyncio
import json
import logging
import random
import time
from typing import Optional, List, Dict, Any
from .sign import get_signature, get_auth_headers, get_timestamp
from .binanceTypes import PositionInfo, AssetInfo, CloseReason

logger = logging.getLogger("BinanceAPI")

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
BASE_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 30.0


class BinanceAPIError(Exception):
    """Raised when Binance API returns an error response."""
    def __init__(self, code: int, msg: str, endpoint: str = ""):
        self.code = code
        self.msg = msg
        self.endpoint = endpoint
        super().__init__(f"Binance API error on {endpoint}: code={code}, msg={msg}")


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
            self.requests = [t for t in self.requests if now - t < self.per_seconds]
            if len(self.requests) >= self.max_requests:
                sleep_time = self.per_seconds - (now - self.requests[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                self.requests = self.requests[1:]
            self.requests.append(time.time())


_global_rate_limiter = RateLimiter(max_requests=8, per_seconds=1.0)


class BinanceFuturesAPI:
    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.rate_limiter = _global_rate_limiter

        if testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = "https://fapi.binance.com"

    async def _make_request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                            body: Optional[Dict] = None, retries: int = MAX_RETRIES):
        """
        Make a signed request to Binance Futures API.

        For GET/DELETE: params are sent as query string.
        For POST/PUT: body is sent as URL-encoded form data (not JSON).
        Both include timestamp + signature in the query string.

        Raises BinanceAPIError on API-level errors.
        Raises aiohttp exceptions on network errors after retries exhausted.
        """
        last_error = None
        for attempt in range(retries):
            try:
                await self.rate_limiter.acquire()

                # Build query string from params or body
                all_params = {}
                if params:
                    all_params.update(params)
                if body:
                    all_params.update(body)

                # Add timestamp
                all_params["timestamp"] = str(get_timestamp())

                # Build sorted query string
                sorted_params = sorted(all_params.items(), key=lambda x: x[0])
                query_string = "&".join([f"{k}={v}" for k, v in sorted_params])

                # Sign
                signature = get_signature(query_string, self.secret_key)
                signed_query = f"{query_string}&signature={signature}"

                # Build URL and headers
                headers = get_auth_headers(self.api_key)
                timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT)

                if method in ("GET", "DELETE"):
                    url = f"{self.base_url}{endpoint}?{signed_query}"
                    data_payload = None
                else:
                    url = f"{self.base_url}{endpoint}"
                    data_payload = signed_query

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers, data=data_payload) as response:
                        rate_remaining = response.headers.get('X-MBX-USED-WEIGHT-1m')
                        if rate_remaining:
                            used = int(rate_remaining)
                            if used > 1000:
                                logger.warning(f"Rate limit weight high: {used}/1200 used")

                        if response.status >= 500:
                            text = await response.text()
                            raise aiohttp.ServerConnectionError(
                                f"Server error {response.status}: {text[:200]}"
                            )

                        try:
                            result = await response.json(content_type=None)
                        except json.JSONDecodeError as e:
                            text = await response.text()
                            raise BinanceAPIError(
                                code=-1,
                                msg=f"Non-JSON response (status {response.status}): {text[:200]}",
                                endpoint=endpoint
                            ) from e

                        # Binance error responses have a "code" field (negative int)
                        if isinstance(result, dict) and "code" in result and result["code"] < 0:
                            raise BinanceAPIError(
                                code=result["code"],
                                msg=result.get("msg", "Unknown error"),
                                endpoint=endpoint
                            )

                        return result

            except (asyncio.TimeoutError, aiohttp.ServerConnectionError,
                    aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as e:
                last_error = e
                if attempt < retries - 1:
                    delay = min(BASE_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1), MAX_RETRY_DELAY)
                    logger.warning(f"Request failed (attempt {attempt + 1}/{retries}): {type(e).__name__}: {e}. Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"Request failed after {retries} attempts: {type(e).__name__}: {e}")
                    raise

            except BinanceAPIError:
                raise

            except aiohttp.ClientError as e:
                logger.error(f"Client error (not retrying): {type(e).__name__}: {e}")
                raise

        raise last_error

    # --- Account ---

    async def get_user_assets(self) -> List[AssetInfo]:
        """GET /fapi/v3/balance — raises on API error."""
        resp = await self._make_request("GET", "/fapi/v3/balance")
        if not isinstance(resp, list):
            raise BinanceAPIError(
                code=-1,
                msg=f"Expected list from /fapi/v3/balance, got {type(resp).__name__}: {resp}",
                endpoint="/fapi/v3/balance"
            )
        assets = []
        for item in resp:
            assets.append(AssetInfo(
                asset=item["asset"],
                availableBalance=float(item["availableBalance"]),
                balance=float(item["balance"]),
                crossUnPnl=float(item["crossUnPnl"]),
            ))
        return assets

    # --- Positions ---

    async def get_open_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        """GET /fapi/v3/positionRisk — filter out zero-size positions. Raises on API error."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        resp = await self._make_request("GET", "/fapi/v3/positionRisk", params=params)
        if not isinstance(resp, list):
            raise BinanceAPIError(
                code=-1,
                msg=f"Expected list from /fapi/v3/positionRisk, got {type(resp).__name__}: {resp}",
                endpoint="/fapi/v3/positionRisk"
            )
        positions = []
        for item in resp:
            amt = float(item["positionAmt"])
            if amt == 0:
                continue
            positions.append(PositionInfo(
                symbol=item["symbol"],
                positionAmt=amt,
                entryPrice=float(item["entryPrice"]),
                markPrice=float(item["markPrice"]),
                unRealizedProfit=float(item["unRealizedProfit"]),
                liquidationPrice=float(item.get("liquidationPrice") or 0),
                leverage=int(item.get("leverage", 1)),
                marginType=item.get("marginType", "UNKNOWN"),
                positionSide=item.get("positionSide", "BOTH"),
                updateTime=int(item.get("updateTime", 0)),
            ))
        return positions

    # --- Market ---

    async def get_instrument_info(self, symbol: str) -> Dict:
        """GET /fapi/v1/exchangeInfo — extract filters for a symbol. Raises if symbol not found."""
        resp = await self._make_request("GET", "/fapi/v1/exchangeInfo")
        if not isinstance(resp, dict) or "symbols" not in resp:
            raise BinanceAPIError(
                code=-1,
                msg=f"Malformed exchangeInfo response: missing 'symbols' key",
                endpoint="/fapi/v1/exchangeInfo"
            )
        for sym_info in resp["symbols"]:
            if sym_info["symbol"] == symbol:
                filters = {f["filterType"]: f for f in sym_info["filters"]}

                price_filter = filters["PRICE_FILTER"]
                lot_filter = filters["LOT_SIZE"]
                min_notional = filters["MIN_NOTIONAL"]

                return {
                    "symbol": symbol,
                    "status": sym_info["status"],
                    "contractType": sym_info["contractType"],
                    "pricePrecision": sym_info["pricePrecision"],
                    "quantityPrecision": sym_info["quantityPrecision"],
                    "tickSize": price_filter["tickSize"],
                    "stepSize": lot_filter["stepSize"],
                    "minQty": lot_filter["minQty"],
                    "minNotional": min_notional["notional"],
                }
        raise BinanceAPIError(
            code=-1,
            msg=f"Symbol '{symbol}' not found in exchangeInfo",
            endpoint="/fapi/v1/exchangeInfo"
        )

    async def get_ticker(self, symbol: str) -> Dict:
        """GET /fapi/v2/ticker/price — raises if symbol not found."""
        resp = await self._make_request("GET", "/fapi/v2/ticker/price", params={"symbol": symbol})
        if isinstance(resp, dict) and "symbol" in resp:
            return resp
        if isinstance(resp, list):
            for item in resp:
                if item["symbol"] == symbol:
                    return item
        raise BinanceAPIError(
            code=-1,
            msg=f"Ticker not found for symbol '{symbol}'",
            endpoint="/fapi/v2/ticker/price"
        )

    # --- Leverage ---

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """POST /fapi/v1/leverage"""
        body = {"symbol": symbol, "leverage": str(leverage)}
        result = await self._make_request("POST", "/fapi/v1/leverage", body=body)
        logger.info(f"Set Leverage Response ({symbol}): {result}")
        return result

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        """POST /fapi/v1/marginType — ISOLATED or CROSSED.
        Silently succeeds if margin type is already set (code -4046)."""
        body = {"symbol": symbol, "marginType": margin_type}
        try:
            result = await self._make_request("POST", "/fapi/v1/marginType", body=body)
            logger.info(f"Set Margin Type Response ({symbol}): {result}")
            return result
        except BinanceAPIError as e:
            if e.code in (-4046, -4067):
                # -4046: already set to this margin type
                # -4067: can't change while open orders/positions exist (already in use)
                logger.info(f"Margin type already {margin_type} for {symbol}, no change needed")
                return {"code": 200, "msg": "No need to change margin type."}
            raise

    # --- Orders ---

    async def create_market_order(self, symbol: str, side: str, quantity: float,
                                   position_side: str = "BOTH", reduce_only: bool = False) -> dict:
        """POST /fapi/v1/order — MARKET order."""
        body = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "quantity": str(quantity),
        }
        if position_side != "BOTH":
            body["positionSide"] = position_side
        if reduce_only:
            body["reduceOnly"] = "true"
        logger.info(f"Market Order Request: {body}")
        return await self._make_request("POST", "/fapi/v1/order", body=body)

    async def create_limit_order(self, symbol: str, side: str, quantity: float, price: float,
                                  position_side: str = "BOTH", reduce_only: bool = False) -> dict:
        """POST /fapi/v1/order — LIMIT order."""
        body = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "quantity": str(quantity),
            "price": str(price),
            "timeInForce": "GTC",
        }
        if position_side != "BOTH":
            body["positionSide"] = position_side
        if reduce_only:
            body["reduceOnly"] = "true"
        logger.info(f"Limit Order Request: {body}")
        return await self._make_request("POST", "/fapi/v1/order", body=body)

    async def create_stop_market_order(self, symbol: str, side: str, quantity: float,
                                        stop_price: float, position_side: str = "BOTH") -> dict:
        """POST /fapi/v1/algoOrder — STOP_MARKET (used for SL).
        Returns dict with 'algoId' (not 'orderId')."""
        body = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side.upper(),
            "type": "STOP_MARKET",
            "quantity": str(quantity),
            "triggerPrice": str(stop_price),
        }
        if position_side != "BOTH":
            body["positionSide"] = position_side
        logger.info(f"Algo Stop Market Order Request (SL): {body}")
        return await self._make_request("POST", "/fapi/v1/algoOrder", body=body)

    async def create_take_profit_market_order(self, symbol: str, side: str, quantity: float,
                                               stop_price: float, position_side: str = "BOTH") -> dict:
        """POST /fapi/v1/algoOrder — TAKE_PROFIT_MARKET (used for TP).
        Returns dict with 'algoId' (not 'orderId')."""
        body = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side.upper(),
            "type": "TAKE_PROFIT_MARKET",
            "quantity": str(quantity),
            "triggerPrice": str(stop_price),
        }
        if position_side != "BOTH":
            body["positionSide"] = position_side
        logger.info(f"Algo Take Profit Market Order Request (TP): {body}")
        return await self._make_request("POST", "/fapi/v1/algoOrder", body=body)

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        """DELETE /fapi/v1/order — for regular orders (LIMIT, MARKET)."""
        return await self._make_request("DELETE", "/fapi/v1/order",
                                         params={"symbol": symbol, "orderId": str(order_id)})

    async def cancel_algo_order(self, algo_id: int) -> dict:
        """DELETE /fapi/v1/algoOrder — for conditional orders (TP/SL)."""
        return await self._make_request("DELETE", "/fapi/v1/algoOrder",
                                         params={"algoId": str(algo_id)})

    async def get_open_orders(self, symbol: Optional[str] = None) -> list:
        """GET /fapi/v1/openOrders — regular orders only. Raises on API error."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        resp = await self._make_request("GET", "/fapi/v1/openOrders", params=params)
        if not isinstance(resp, list):
            raise BinanceAPIError(
                code=-1,
                msg=f"Expected list from /fapi/v1/openOrders, got {type(resp).__name__}: {resp}",
                endpoint="/fapi/v1/openOrders"
            )
        return resp

    async def get_open_algo_orders(self, symbol: Optional[str] = None) -> list:
        """GET /fapi/v1/openAlgoOrders — conditional orders (TP/SL). Raises on API error."""
        params = {}
        if symbol:
            params["symbol"] = symbol
        resp = await self._make_request("GET", "/fapi/v1/openAlgoOrders", params=params)
        if not isinstance(resp, list):
            raise BinanceAPIError(
                code=-1,
                msg=f"Expected list from /fapi/v1/openAlgoOrders, got {type(resp).__name__}: {resp}",
                endpoint="/fapi/v1/openAlgoOrders"
            )
        return resp

    async def get_order(self, symbol: str, order_id: int) -> dict:
        """GET /fapi/v1/order — regular orders."""
        return await self._make_request("GET", "/fapi/v1/order",
                                         params={"symbol": symbol, "orderId": str(order_id)})

    async def get_algo_order(self, algo_id: int) -> dict:
        """GET /fapi/v1/algoOrder — query a single algo order by ID."""
        return await self._make_request("GET", "/fapi/v1/algoOrder",
                                         params={"algoId": str(algo_id)})

    async def get_all_orders(self, symbol: str, limit: int = 50) -> list:
        """GET /fapi/v1/allOrders — raises on API error."""
        resp = await self._make_request("GET", "/fapi/v1/allOrders",
                                         params={"symbol": symbol, "limit": str(limit)})
        if not isinstance(resp, list):
            raise BinanceAPIError(
                code=-1,
                msg=f"Expected list from /fapi/v1/allOrders, got {type(resp).__name__}: {resp}",
                endpoint="/fapi/v1/allOrders"
            )
        return resp

    async def get_trades(self, symbol: str, limit: int = 50) -> list:
        """GET /fapi/v1/userTrades — raises on API error."""
        resp = await self._make_request("GET", "/fapi/v1/userTrades",
                                         params={"symbol": symbol, "limit": str(limit)})
        if not isinstance(resp, list):
            raise BinanceAPIError(
                code=-1,
                msg=f"Expected list from /fapi/v1/userTrades, got {type(resp).__name__}: {resp}",
                endpoint="/fapi/v1/userTrades"
            )
        return resp

    # --- Close reason detection ---

    async def get_position_close_reason(self, symbol: str) -> CloseReason:
        """Determine why a position was closed by checking recent order history."""
        orders = await self.get_all_orders(symbol, limit=10)
        if not orders:
            return CloseReason.UNKNOWN

        # Sort by updateTime descending
        orders.sort(key=lambda o: int(o["updateTime"]), reverse=True)

        for order in orders:
            status = order["status"]
            order_type = order["type"]
            if status != "FILLED":
                continue
            if order_type == "TAKE_PROFIT_MARKET":
                return CloseReason.TP
            elif order_type == "STOP_MARKET":
                return CloseReason.SL
            elif order_type == "LIQUIDATION":
                return CloseReason.LIQUIDATION
            elif order_type in ("MARKET", "LIMIT") and order.get("reduceOnly"):
                return CloseReason.MANUAL

        return CloseReason.UNKNOWN
