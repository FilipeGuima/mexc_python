"""
Exchange adapter layer providing a unified interface for stats bot.
Supports MEXC and Blofin exchanges via the adapter pattern.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Any

logger = logging.getLogger(__name__)


# --- Unified Dataclasses ---

@dataclass
class StatsAssetInfo:
    currency: str
    equity: float
    availableBalance: float
    unrealized: float


@dataclass
class StatsPositionInfo:
    positionId: str
    symbol: str
    holdVol: float
    positionType: str  # "LONG" or "SHORT"
    openAvgPrice: float
    closeAvgPrice: float
    margin: float
    unrealized: float
    realised: float
    leverage: int
    createTime: float  # unix timestamp in seconds
    updateTime: float  # unix timestamp in seconds


@dataclass
class StatsOrderInfo:
    orderId: str
    positionId: str
    symbol: str
    orderType: str
    price: float


@dataclass
class StatsTickerInfo:
    lastPrice: float
    volume24: str


@dataclass
class StatsContractInfo:
    contractSize: float
    priceUnit: str


# --- Abstract Base Class ---

class ExchangeAdapter(ABC):

    @property
    @abstractmethod
    def exchange_name(self) -> str:
        ...

    @abstractmethod
    async def get_assets(self) -> Optional[List[StatsAssetInfo]]:
        ...

    @abstractmethod
    async def get_open_positions(self, symbol: Optional[str] = None) -> Optional[List[StatsPositionInfo]]:
        ...

    @abstractmethod
    async def get_historical_positions(self, symbol: Optional[str] = None, page_num: int = 1, page_size: int = 20) -> Optional[List[StatsPositionInfo]]:
        ...

    @abstractmethod
    async def get_pending_tp_orders(self) -> Optional[List[StatsOrderInfo]]:
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Optional[StatsTickerInfo]:
        ...

    @abstractmethod
    async def get_contract_details(self, symbol: str) -> Optional[StatsContractInfo]:
        ...

    @abstractmethod
    def to_exchange_symbol(self, display_symbol: str) -> str:
        """Convert a display symbol like BTC_USDT to exchange-native format."""
        ...

    @abstractmethod
    def to_display_symbol(self, exchange_symbol: str) -> str:
        """Convert an exchange-native symbol to display format BTC_USDT."""
        ...


# --- MEXC Adapter ---

class MexcAdapter(ExchangeAdapter):
    def __init__(self, api):
        from mexcpy.api import MexcFuturesAPI
        self._api: MexcFuturesAPI = api

    @property
    def exchange_name(self) -> str:
        return "MEXC"

    async def _safe_call(self, method_name: str, *args, **kwargs) -> Any:
        try:
            method = getattr(self._api, method_name, None)
            if not method:
                return None
            response = await method(*args, **kwargs)
            if response.success:
                if isinstance(response.data, (list, dict)) and not response.data:
                    return None
                return response.data
            else:
                logger.warning(f"MEXC API call failed for {method_name}: {response.message}")
                return None
        except Exception as e:
            logger.error(f"MEXC API exception in {method_name}: {e}")
            return None

    async def get_assets(self) -> Optional[List[StatsAssetInfo]]:
        data = await self._safe_call("get_user_assets")
        if not data:
            return None
        return [
            StatsAssetInfo(
                currency=a.currency,
                equity=a.equity,
                availableBalance=a.availableBalance,
                unrealized=a.unrealized,
            )
            for a in data
        ]

    async def get_open_positions(self, symbol: Optional[str] = None) -> Optional[List[StatsPositionInfo]]:
        data = await self._safe_call("get_open_positions", symbol=symbol)
        if not data:
            return None
        from mexcpy.mexcTypes import PositionType
        return [
            StatsPositionInfo(
                positionId=str(p.positionId),
                symbol=p.symbol,
                holdVol=p.holdVol,
                positionType="LONG" if p.positionType == PositionType.Long.value else "SHORT",
                openAvgPrice=p.openAvgPrice,
                closeAvgPrice=p.closeAvgPrice,
                margin=p.im,
                unrealized=0.0,  # MEXC needs ticker to compute this
                realised=p.realised,
                leverage=p.leverage,
                createTime=p.createTime / 1000,
                updateTime=p.updateTime / 1000,
            )
            for p in data
        ]

    async def get_historical_positions(self, symbol: Optional[str] = None, page_num: int = 1, page_size: int = 20) -> Optional[List[StatsPositionInfo]]:
        data = await self._safe_call("get_historical_positions", symbol=symbol, page_num=page_num, page_size=page_size)
        if not data:
            return None
        from mexcpy.mexcTypes import PositionType
        return [
            StatsPositionInfo(
                positionId=str(p.positionId),
                symbol=p.symbol,
                holdVol=p.holdVol,
                positionType="LONG" if p.positionType == PositionType.Long.value else "SHORT",
                openAvgPrice=p.openAvgPrice,
                closeAvgPrice=p.closeAvgPrice,
                margin=p.im,
                unrealized=0.0,
                realised=p.realised,
                leverage=p.leverage,
                createTime=p.createTime / 1000,
                updateTime=p.updateTime / 1000,
            )
            for p in data
        ]

    async def get_pending_tp_orders(self) -> Optional[List[StatsOrderInfo]]:
        data = await self._safe_call("get_current_pending_orders")
        if not data:
            return None
        from mexcpy.mexcTypes import OrderType
        return [
            StatsOrderInfo(
                orderId=str(o.orderId),
                positionId=str(o.positionId),
                symbol=o.symbol,
                orderType=str(o.orderType),
                price=o.price,
            )
            for o in data
            if o.orderType == OrderType.PriceLimited
        ]

    async def get_ticker(self, symbol: str) -> Optional[StatsTickerInfo]:
        data = await self._safe_call("get_ticker", symbol)
        if not data:
            return None
        return StatsTickerInfo(
            lastPrice=float(data.get('lastPrice', 0)),
            volume24=str(data.get('volume24', 'N/A')),
        )

    async def get_contract_details(self, symbol: str) -> Optional[StatsContractInfo]:
        data = await self._safe_call("get_contract_details", symbol)
        if not data:
            return None
        return StatsContractInfo(
            contractSize=float(data.get('contractSize', 1)),
            priceUnit=str(data.get('priceUnit', '')),
        )

    def to_exchange_symbol(self, display_symbol: str) -> str:
        # MEXC uses BTC_USDT format natively
        return display_symbol

    def to_display_symbol(self, exchange_symbol: str) -> str:
        return exchange_symbol


# --- Blofin Adapter ---

class BlofinAdapter(ExchangeAdapter):
    def __init__(self, api):
        from blofincpy.api import BlofinFuturesAPI
        self._api: BlofinFuturesAPI = api

    @property
    def exchange_name(self) -> str:
        return "Blofin"

    async def get_assets(self) -> Optional[List[StatsAssetInfo]]:
        try:
            assets = await self._api.get_user_assets()
            if not assets:
                return None
            return [
                StatsAssetInfo(
                    currency=a.currency,
                    equity=a.equity,
                    availableBalance=a.availableBalance,
                    unrealized=a.unrealized,
                )
                for a in assets
            ]
        except Exception as e:
            logger.error(f"Blofin get_assets error: {e}")
            return None

    async def get_open_positions(self, symbol: Optional[str] = None) -> Optional[List[StatsPositionInfo]]:
        try:
            blofin_symbol = self.to_exchange_symbol(symbol) if symbol else None
            positions = await self._api.get_open_positions(symbol=blofin_symbol)
            if not positions:
                return None
            return [
                StatsPositionInfo(
                    positionId=p.positionId,
                    symbol=self.to_display_symbol(p.symbol),
                    holdVol=p.holdVol,
                    positionType=p.positionType.upper() if p.positionType in ("long", "short") else p.positionType.upper(),
                    openAvgPrice=p.openAvgPrice,
                    closeAvgPrice=0.0,
                    margin=p.margin,
                    unrealized=p.unrealized,
                    realised=0.0,
                    leverage=p.leverage,
                    createTime=int(p.createTime) / 1000 if p.createTime else 0.0,
                    updateTime=int(p.updateTime) / 1000 if p.updateTime else 0.0,
                )
                for p in positions
            ]
        except Exception as e:
            logger.error(f"Blofin get_open_positions error: {e}")
            return None

    async def get_historical_positions(self, symbol: Optional[str] = None, page_num: int = 1, page_size: int = 20) -> Optional[List[StatsPositionInfo]]:
        """
        Blofin doesn't have a direct historical positions endpoint.
        We use order history and filter for filled closing orders that have a PNL.
        """
        try:
            blofin_symbol = self.to_exchange_symbol(symbol) if symbol else None
            orders = await self._api.get_order_history(symbol=blofin_symbol)
            if not orders:
                return None

            results = []
            for o in orders:
                pnl = float(o.get("pnl", 0))
                state = o.get("state", "")
                reduce_only = o.get("reduceOnly", "false")

                # Only include filled closing orders (those with PNL)
                if state == "filled" and (pnl != 0 or reduce_only == "true"):
                    pos_side = o.get("positionSide", o.get("side", ""))
                    if pos_side in ("long", "buy"):
                        pos_type = "LONG"
                    elif pos_side in ("short", "sell"):
                        pos_type = "SHORT"
                    else:
                        pos_type = pos_side.upper() if pos_side else "UNKNOWN"

                    create_ts = int(o.get("createTime", 0)) / 1000
                    update_ts = int(o.get("updateTime", 0)) / 1000

                    results.append(StatsPositionInfo(
                        positionId=o.get("orderId", ""),
                        symbol=self.to_display_symbol(o.get("instId", "")),
                        holdVol=float(o.get("size", 0)),
                        positionType=pos_type,
                        openAvgPrice=float(o.get("price", 0)),
                        closeAvgPrice=float(o.get("averagePrice", 0)),
                        margin=0.0,
                        unrealized=0.0,
                        realised=pnl,
                        leverage=int(o.get("leverage", 1)),
                        createTime=create_ts,
                        updateTime=update_ts,
                    ))

            if not results:
                return None
            return results
        except Exception as e:
            logger.error(f"Blofin get_historical_positions error: {e}")
            return None

    async def get_pending_tp_orders(self) -> Optional[List[StatsOrderInfo]]:
        try:
            tpsl_orders = await self._api.get_tpsl_orders()
            if not tpsl_orders:
                return None

            results = []
            for o in tpsl_orders:
                tp_price = o.get("tpTriggerPrice")
                if tp_price and tp_price != "0" and tp_price != "":
                    results.append(StatsOrderInfo(
                        orderId=o.get("tpslId", o.get("orderId", "")),
                        positionId=o.get("positionId", ""),
                        symbol=self.to_display_symbol(o.get("instId", "")),
                        orderType="TP",
                        price=float(tp_price),
                    ))

            return results if results else None
        except Exception as e:
            logger.error(f"Blofin get_pending_tp_orders error: {e}")
            return None

    async def get_ticker(self, symbol: str) -> Optional[StatsTickerInfo]:
        try:
            blofin_symbol = self.to_exchange_symbol(symbol)
            resp = await self._api._make_request("GET", "/api/v1/market/tickers", params={"instId": blofin_symbol})
            if resp.get("code") == "0" and resp.get("data"):
                data = resp["data"]
                if isinstance(data, list) and len(data) > 0:
                    ticker = data[0]
                    return StatsTickerInfo(
                        lastPrice=float(ticker.get("last", 0)),
                        volume24=str(ticker.get("vol24h", "N/A")),
                    )
            return None
        except Exception as e:
            logger.error(f"Blofin get_ticker error: {e}")
            return None

    async def get_contract_details(self, symbol: str) -> Optional[StatsContractInfo]:
        try:
            blofin_symbol = self.to_exchange_symbol(symbol)
            info = await self._api.get_instrument_info(blofin_symbol)
            if not info:
                return None
            return StatsContractInfo(
                contractSize=float(info.get("contractValue", info.get("ctVal", 1))),
                priceUnit=str(info.get("tickSize", "")),
            )
        except Exception as e:
            logger.error(f"Blofin get_contract_details error: {e}")
            return None

    def to_exchange_symbol(self, display_symbol: str) -> str:
        # Convert BTC_USDT -> BTC-USDT
        return display_symbol.replace("_", "-")

    def to_display_symbol(self, exchange_symbol: str) -> str:
        # Convert BTC-USDT -> BTC_USDT
        return exchange_symbol.replace("-", "_")


# --- Factory ---

def create_adapter(account_config: dict) -> ExchangeAdapter:
    exchange = account_config.get("exchange", "mexc").lower()

    if exchange == "mexc":
        from mexcpy.api import MexcFuturesAPI
        api = MexcFuturesAPI(token=account_config["token"], testnet=account_config.get("testnet", False))
        return MexcAdapter(api)
    elif exchange == "blofin":
        from blofincpy.api import BlofinFuturesAPI
        api = BlofinFuturesAPI(
            api_key=account_config["api_key"],
            secret_key=account_config["secret_key"],
            passphrase=account_config["passphrase"],
            testnet=account_config.get("testnet", True),
        )
        return BlofinAdapter(api)
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")
