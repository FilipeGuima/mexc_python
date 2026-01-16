from dataclasses import dataclass
from enum import Enum
from typing import Optional, List, Any

class OrderSide(Enum):
    Buy = "buy"
    Sell = "sell"

class PositionSide(Enum):
    Long = "long"
    Short = "short"
    Net = "net"

class OrderType(Enum):
    Market = "market"
    Limit = "limit"
    PostOnly = "post_only"
    Fok = "fok"
    Ioc = "ioc"

class MarginMode(Enum):
    Cross = "cross"
    Isolated = "isolated"

@dataclass
class BlofinOrderRequest:
    instId: str
    side: OrderSide
    orderType: OrderType
    marginMode: MarginMode
    size: str
    positionSide: Optional[PositionSide] = None
    price: Optional[str] = None
    reduceOnly: Optional[str] = "false"
    clientOid: Optional[str] = None

@dataclass
class PositionInfo:
    """Standardized Position Info for the bot"""
    positionId: str
    symbol: str
    holdVol: float
    positionType: str
    openAvgPrice: float
    liquidatePrice: float
    unrealized: float
    leverage: int
    marginMode: str

@dataclass
class AssetInfo:
    currency: str
    equity: float
    availableBalance: float
    unrealized: float