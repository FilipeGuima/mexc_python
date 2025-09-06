import dataclasses
import time
from enum import Enum
from dataclasses import asdict, dataclass
import types
from typing import Any, Dict, List, Optional, Type, TypeVar, Generic, Union
import aiohttp
import json

from .sign import generate_signature
from .mexcTypes import (
    AssetInfo, OrderId, TransferRecords, PositionInfo, FundingRecords, Order, Transaction,
    TriggerOrder, StopLimitOrder, RiskLimit, TradingFeeInfo, Leverage, PositionMode,
    CreateOrderRequest, TriggerOrderRequest, ExecuteCycle, PositionType, OpenType,
    OrderSide, OrderType, OrderCategory, TriggerType, TriggerPriceType,
    PositionSide
)


def asdict_factory_with_enum_support(data):
    def convert_value(obj):
        return obj.value if isinstance(obj, Enum) else obj

    return dict((k, convert_value(v)) for k, v in data)


T = TypeVar('T')


@dataclass
class ApiResponse(Generic[T]):
    success: bool
    code: int
    data: T
    message: Optional[str] = None

    @classmethod
    def from_dict(cls, data_dict: Dict[str, Any], data_type: Type[T]) -> 'ApiResponse[T]':
        processed_data: Any = None
        raw_data = data_dict.get('data')

        if raw_data is None:
            processed_data = None
        elif isinstance(raw_data, dict):
            if dataclasses.is_dataclass(data_type):
                expected_keys = {f.name for f in dataclasses.fields(data_type)}
                filtered_data = {k: v for k, v in raw_data.items() if k in expected_keys}
                processed_data = data_type(**filtered_data)
            else:
                processed_data = raw_data
        elif isinstance(raw_data, list):
            processed_data = []
            for item in raw_data:
                if isinstance(item, dict) and dataclasses.is_dataclass(data_type):
                    expected_keys = {f.name for f in dataclasses.fields(data_type)}
                    filtered_item = {k: v for k, v in item.items() if k in expected_keys}
                    processed_data.append(data_type(**filtered_item))
                else:
                    processed_data.append(item)
        else:
            processed_data = raw_data

        return cls(
            success=data_dict.get('success', False),
            code=data_dict.get('code', 0),
            data=processed_data,
            message=data_dict.get('message')
        )


class MexcFuturesAPI:
    def __init__(self, api_key: str, secret_key: str, testnet: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = (
            "https://futures.testnet.mexc.com/api/v1"
            if testnet
            else "https://futures.mexc.com/api/v1"
        )

    def _dict_to_url_params(self, params: Dict[str, Any]) -> str:
        return "&".join(f"{k}={v}" for k, v in params.items() if v is not None)

    async def _make_request(
            self,
            method: str,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            response_type: Optional[Type[T]] = None
    ) -> ApiResponse[T]:

        timestamp = str(int(time.time() * 1000))
        query_string = ""
        request_body = ""

        if params:
            if method == "GET":
                query_string = self._dict_to_url_params(params)
            else:  # POST
                request_body = json.dumps(params)

        # Correctly create the string to sign
        string_to_sign = timestamp + self.api_key + (query_string or request_body)
        signature = generate_signature(self.api_key, self.secret_key, timestamp, (query_string or request_body))

        headers = {
            'Content-Type': 'application/json',
            'X-MEXC-APIKEY': self.api_key,
            'X-MEXC-TIMESTAMP': timestamp,
            'X-MEXC-SIGN': signature
        }

        url = f"{self.base_url}{endpoint}"
        if query_string:
            url += f"?{query_string}"

        async with aiohttp.ClientSession() as session:
            async with session.request(
                    method,
                    url,
                    headers=headers,
                    data=request_body if method == "POST" else None,
            ) as response:
                response_data = await response.json()
                if response_type:
                    return ApiResponse.from_dict(response_data, response_type)
                return ApiResponse(
                    success=response_data.get("success", False),
                    code=response_data.get("code", 0),
                    data=response_data.get("data"),
                    message=response_data.get("message"),
                )

    # --- Public Endpoints ---
    async def get_ticker(self, symbol: str) -> 'ApiResponse':
        endpoint = f"/contract/ticker"
        params = {"symbol": symbol}
        url_params = f"?{self._dict_to_url_params(params)}"
        async with aiohttp.ClientSession() as session:
            async with session.request("GET", f"{self.base_url}{endpoint}{url_params}") as response:
                response_data = await response.json()
                return ApiResponse(
                    success=response_data.get("success", False),
                    code=response_data.get("code", 0),
                    data=response_data.get("data"),
                    message=response_data.get("message"),
                )

    async def get_contract_details(self, symbol: str) -> 'ApiResponse':
        endpoint = f"/contract/detail"
        params = {"symbol": symbol}
        url_params = f"?{self._dict_to_url_params(params)}"
        async with aiohttp.ClientSession() as session:
            async with session.request("GET", f"{self.base_url}{endpoint}{url_params}") as response:
                response_data = await response.json()
                return ApiResponse(
                    success=response_data.get("success", False),
                    code=response_data.get("code", 0),
                    data=response_data.get("data"),
                    message=response_data.get("message"),
                )

    # --- Private Endpoints ---
    async def get_user_assets(self) -> ApiResponse[List[AssetInfo]]:
        return await self._make_request("GET", "/private/account/assets", response_type=AssetInfo)

    async def get_open_positions(self) -> ApiResponse[List[PositionInfo]]:
        return await self._make_request("GET", "/private/position/open_positions", response_type=PositionInfo)

    async def get_history_positions(self) -> ApiResponse[List[PositionInfo]]:
        return await self._make_request("GET", "/private/position/list/history_positions", {"page_size": 50},
                                        response_type=PositionInfo)

    async def create_order(self, order_request: CreateOrderRequest) -> ApiResponse[OrderId]:
        params = asdict(order_request, dict_factory=asdict_factory_with_enum_support)
        # The correct endpoint for placing an order is /private/order/submit
        return await self._make_request("POST", "/private/order/submit", params, response_type=OrderId)

    # --- Convenience Methods ---
    async def create_market_order(self, symbol: str, side: OrderSide, vol: float, leverage: int) -> ApiResponse[
        OrderId]:
        order_request = CreateOrderRequest(
            symbol=symbol,
            side=side,
            vol=vol,
            leverage=leverage,
            type=OrderType.MarketOrder,
            openType=OpenType.Isolated
        )
        return await self.create_order(order_request)